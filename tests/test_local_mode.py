from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.feishu import FeishuAPIError
from app.local import initialize
from app.long_connection import LongConnectionReceiver, persist_event
from app.models import OAuthTokens, OAuthUserInfo, User
from app.repository import MemoryRepository
from app.security import TokenCipher
from app.sqlite_repository import SQLiteRepository
from app.workers import BackgroundSupervisor
from tests.test_api import FakeBitable, FakeFeishu
from tests.test_processor import TENANT, message_event


def local_settings() -> Settings:
    return Settings(
        app_env="test",
        local_only=True,
        host="127.0.0.1",
        public_base_url="http://127.0.0.1:8090",
        feishu_event_transport="long_connection",
        run_background_workers=False,
        admin_api_token="a" * 48,
        oauth_state_secret="b" * 48,
        token_encryption_secret="c" * 48,
        feishu_tenant_key=TENANT,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"host": "0.0.0.0"},
        {"host": "192.168.1.2"},
        {"public_base_url": "https://example.test"},
        {"feishu_event_transport": "webhook"},
        {"admin_api_token": ""},
        {"database_url": "postgresql://db/app"},
        {"database_url": "sqlite:///:memory:"},
        {"run_background_workers": True},
    ],
)
def test_local_rejects_unsafe_settings(changes: dict) -> None:
    assert replace(local_settings(), **changes).validate_runtime()


def test_local_defaults_are_valid_and_production_webhook_secrets_not_required() -> None:
    settings = local_settings()
    assert settings.validate_runtime() == []
    issues = settings.validate_production()
    assert "BITABLE_CALLBACK_TOKEN" not in issues
    assert "FEISHU_ENCRYPT_KEY" not in issues
    assert "FEISHU_VERIFICATION_TOKEN" not in issues
    assert "FEISHU_APP_SECRET" in issues


def test_local_single_user_pilot_relaxes_only_the_bitable_role() -> None:
    configured = replace(
        local_settings(),
        local_single_user_pilot=True,
        bitable_grant_document_access=False,
    )
    assert configured.validate_runtime() == []
    assert "BITABLE_USER_ROLE_ID" not in configured.validate_production()
    unsafe = replace(configured, local_only=False)
    assert "LOCAL_SINGLE_USER_PILOT requires LOCAL_ONLY=true" in unsafe.validate_runtime()
    role_unsafe = replace(configured, bitable_user_role_id="role-must-not-be-used")
    assert (
        "LOCAL_SINGLE_USER_PILOT requires BITABLE_USER_ROLE_ID to be empty"
        in role_unsafe.validate_runtime()
    )


def test_local_http_boundary_and_offline_health() -> None:
    app = create_app(
        local_settings(), repository=MemoryRepository(), feishu=FakeFeishu(), bitable=FakeBitable()
    )
    with TestClient(app, base_url="http://127.0.0.1:8090", client=("127.0.0.1", 1234)) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["receiver_process_running"] is False
        assert health.json()["background_workers"] is False
        assert health.headers["referrer-policy"] == "no-referrer"
        assert client.get("/admin/users").status_code == 401
        for path in ["feishu/events", "bitable/status", "bitable/settings", "bitable/users"]:
            assert client.post(f"/integrations/{path}", json={}).status_code == 404
        assert client.get("/healthz", headers={"Host": "evil.test:8090"}).status_code == 403
        assert client.get("/healthz", headers={"Origin": "https://evil.test"}).status_code == 403
        assert (
            client.get(
                "/auth/feishu/callback", params={"code": "code", "state": "state"}
            ).status_code
            == 400
        )
    with TestClient(app, base_url="http://127.0.0.1:8090", client=("192.168.1.2", 1234)) as client:
        assert client.get("/healthz").status_code == 403


def test_local_init_is_private_unique_and_non_destructive(tmp_path: Path) -> None:
    from dotenv import dotenv_values

    path = tmp_path / ".env.local"
    initialize(path)
    values = dotenv_values(path)
    keys = [
        values[name]
        for name in ("ADMIN_API_TOKEN", "OAUTH_STATE_SECRET", "TOKEN_ENCRYPTION_SECRET")
    ]
    assert len(set(keys)) == 3
    assert all(value and len(value) >= 48 for value in keys)
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        initialize(path)
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_long_connection_queue_survives_reopen_and_deduplicates(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'test.db'}"
    repo = SQLiteRepository(url, TokenCipher("test-secret"))
    await repo.open()
    event = message_event()
    event["header"]["token"] = "must-not-be-stored"
    raw = json.dumps(event).encode()
    await asyncio.gather(*(persist_event(repo, raw, TENANT) for _ in range(10)))
    await repo.close()
    repo = SQLiteRepository(url, TokenCipher("test-secret"))
    await repo.open()
    try:
        jobs = await repo.claim_events()
        assert len(jobs) == 1
        assert "token" not in jobs[0].payload["header"]
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_receiver_rejects_wrong_tenant_and_bad_envelopes() -> None:
    repo = MemoryRepository()
    for raw in [
        b"[]",
        b"invalid",
        json.dumps(message_event()).encode(),
        b"x" * (2 * 1024 * 1024 + 1),
    ]:
        with pytest.raises(ValueError):
            await persist_event(repo, raw, "another-tenant")
    assert not repo.events


@pytest.mark.asyncio
@pytest.mark.parametrize("storage_fails", [False, True])
async def test_receiver_acknowledges_only_after_commit_and_stops_child(
    monkeypatch, storage_fails: bool
) -> None:
    repo = MemoryRepository()
    drained = asyncio.Event()

    class Input:
        def write(self, data: bytes) -> None:
            assert data == (b"retry\n" if storage_fails else b"ok\n")
            assert bool(repo.events) is not storage_fails

        async def drain(self) -> None:
            drained.set()

    class Process:
        returncode = None
        stdin = Input()
        stdout = asyncio.StreamReader()

        def terminate(self) -> None:
            self.returncode = 0

        async def wait(self) -> int:
            return self.returncode

    process = Process()
    process.stdout.feed_data(json.dumps(message_event()).encode() + b"\n")

    async def spawn(*args, **kwargs):
        assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
        return process

    async def fail(*args, **kwargs):
        raise RuntimeError("synthetic storage failure")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    if storage_fails:
        monkeypatch.setattr(repo, "enqueue_event", fail)
    receiver = LongConnectionReceiver(local_settings(), repo)
    receiver.task = asyncio.create_task(receiver._run())
    await asyncio.wait_for(drained.wait(), timeout=2)
    await receiver.stop()
    assert process.returncode == 0
    assert not receiver.running


def test_official_sdk_dispatch_preserves_envelope_without_network(monkeypatch) -> None:
    import io
    import logging
    from types import SimpleNamespace

    from app.long_connection import main

    lark = pytest.importorskip("lark_oapi")
    output = io.BytesIO()

    class Client:
        def __init__(self, *args, event_handler, **kwargs):
            self.handler = event_handler

        def start(self):
            payload = message_event()
            payload["schema"] = "2.0"
            with pytest.warns(DeprecationWarning):
                self.handler.do_without_validation(json.dumps(payload).encode())

    monkeypatch.setattr(lark.ws, "Client", Client)
    monkeypatch.setattr("sys.stdout", SimpleNamespace(buffer=output))
    monkeypatch.setattr("sys.stdin", SimpleNamespace(buffer=io.BytesIO(b"ok\n")))
    monkeypatch.setenv("FEISHU_APP_ID", "test-app")
    monkeypatch.setenv("FEISHU_APP_SECRET", "test-secret")
    previous = logging.root.manager.disable
    try:
        main()
    finally:
        logging.disable(previous)
    event = json.loads(output.getvalue())
    assert event["header"]["tenant_key"] == TENANT
    assert (
        event["event"]["message"]["message_id"] == message_event()["event"]["message"]["message_id"]
    )
    assert len(output.getvalue().splitlines()) == 1


@pytest.mark.asyncio
async def test_local_settings_poll_uses_mapping_not_remote_identity() -> None:
    from unittest.mock import AsyncMock

    user = User(tenant_key=TENANT, user_id="u1", open_id="ou_1", authorized=True)
    repository = AsyncMock()
    repository.list_active_users.return_value = [user]
    repository.get_mapping.return_value = {"record_id": "rec_owned"}
    bitable = AsyncMock()
    bitable.list_records.return_value = [
        {"record_id": "rec_unknown", "fields": {"包含@所有人": True}},
        {"record_id": "rec_owned", "fields": {"包含@所有人": "false"}},
        {"record_id": "rec_owned", "fields": {"包含@所有人": True, "内部用户ID": "u2"}},
    ]
    supervisor = BackgroundSupervisor(
        local_settings(), repository, AsyncMock(), AsyncMock(), bitable
    )
    await supervisor._pull_bitable_settings()
    repository.update_user_setting.assert_awaited_once_with(TENANT, "u1", True)
    assert supervisor._update_fields("settings", {"包含@所有人": False, "授权状态": "已授权"}) == {
        "授权状态": "已授权"
    }


def test_local_activation_does_not_send_unusable_loopback_url() -> None:
    feishu = FakeFeishu()
    configured = replace(
        local_settings(),
        feishu_app_id="cli_test",
        feishu_app_secret="test-secret",
        bitable_user_role_id="role_test",
    )
    app = create_app(
        configured, repository=MemoryRepository(), feishu=feishu, bitable=FakeBitable()
    )
    with TestClient(app, base_url="http://127.0.0.1:8090", client=("127.0.0.1", 1234)) as client:
        result = client.post(
            "/admin/users",
            json={"user_id": "u1"},
            headers={"Authorization": "Bearer " + local_settings().admin_api_token},
        )
        assert result.status_code == 200
        assert not feishu.activation_messages
        start = client.get("/auth/feishu/start", follow_redirects=False)
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        assert "set-cookie" not in start.headers
        callback = client.get(
            "/auth/feishu/callback", params={"code": "valid-code", "state": state}
        )
        replay = client.get("/auth/feishu/callback", params={"code": "valid-code", "state": state})
        assert callback.status_code == 200
        assert replay.status_code == 400
        assert "oauth_state" not in client.cookies


def test_local_single_user_pilot_reauthorizes_only_existing_user_without_role() -> None:
    repository = MemoryRepository()
    asyncio.run(repository.enable_user(User(tenant_key=TENANT, user_id="u1", open_id="ou_1")))
    bitable = FakeBitable()
    configured = replace(
        local_settings(),
        local_single_user_pilot=True,
        bitable_grant_document_access=False,
        feishu_app_id="cli_test",
        feishu_app_secret="test-secret",
        bitable_user_role_id="",
    )
    app = create_app(configured, repository=repository, feishu=FakeFeishu(), bitable=bitable)
    with TestClient(app, base_url="http://127.0.0.1:8090", client=("127.0.0.1", 1234)) as client:
        start = client.get("/auth/feishu/start", follow_redirects=False)
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        callback = client.get(
            "/auth/feishu/callback", params={"code": "valid-code", "state": state}
        )
        rejected = client.post(
            "/admin/users",
            headers={"Authorization": "Bearer " + configured.admin_api_token},
            json={"user_id": "u2", "open_id": "ou_2", "name": "用户二"},
        )

    assert callback.status_code == 200
    assert rejected.status_code == 409
    assert bitable.granted == []
    assert bitable.revoked == []
    assert len(asyncio.run(repository.list_users())) == 1


@pytest.mark.asyncio
async def test_local_single_user_pilot_serializes_concurrent_enrollment() -> None:
    class YieldingRepository(MemoryRepository):
        async def list_users(self) -> list[User]:
            await asyncio.sleep(0.02)
            return await super().list_users()

    configured = replace(
        local_settings(),
        local_single_user_pilot=True,
        bitable_grant_document_access=False,
    )
    repository = YieldingRepository()
    app = create_app(
        configured,
        repository=repository,
        feishu=FakeFeishu(),
        bitable=FakeBitable(),
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    headers = {"Authorization": "Bearer " + configured.admin_api_token}
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8090") as client:
        responses = await asyncio.gather(
            client.post(
                "/admin/users",
                headers=headers,
                json={"user_id": "u1", "open_id": "ou_1", "name": "用户一"},
            ),
            client.post(
                "/admin/users",
                headers=headers,
                json={"user_id": "u2", "open_id": "ou_2", "name": "用户二"},
            ),
        )

    assert sorted(response.status_code for response in responses) == [200, 409]
    assert len(await repository.list_users()) == 1


@pytest.mark.asyncio
async def test_local_single_user_pilot_refuses_worker_start_with_multiple_users(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'pilot.db'}"
    repository = SQLiteRepository(database_url, TokenCipher("test-secret"))
    await repository.open()
    for suffix in ("1", "2"):
        await repository.enable_user(
            User(tenant_key=TENANT, user_id=f"u{suffix}", open_id=f"ou_{suffix}")
        )
        await repository.activate_user(
            OAuthUserInfo(TENANT, f"u{suffix}", f"ou_{suffix}", f"用户{suffix}"),
            OAuthTokens("access", "refresh", 7200, 30 * 86400),
        )
    await repository.close()
    configured = replace(
        local_settings(),
        local_single_user_pilot=True,
        bitable_grant_document_access=False,
        run_background_workers=True,
        feishu_app_id="cli_test",
        feishu_app_secret="test-secret",
        database_url=database_url,
    )
    app = create_app(
        configured,
        repository=repository,
        feishu=FakeFeishu(),
        bitable=FakeBitable(),
    )

    with pytest.raises(
        RuntimeError,
        match="LOCAL_SINGLE_USER_PILOT requires exactly one enabled, authorized user",
    ):
        await app.state.container.startup()
    assert app.state.container.supervisor is None
    assert app.state.container.receiver is None


def test_local_first_user_bootstrap_is_one_time_and_persists_tenant(tmp_path: Path) -> None:
    from dotenv import dotenv_values

    path = tmp_path / ".env.local"
    initialize(path)
    configured = replace(
        local_settings(),
        feishu_app_id="cli_test",
        feishu_app_secret="test-secret",
        feishu_tenant_key="",
        bitable_user_role_id="",
        local_bootstrap_first_user=True,
        local_config_path=str(path),
    )
    repository = MemoryRepository()
    bitable = FakeBitable()

    class InactiveFeishu(FakeFeishu):
        async def list_user_chats(self, access_token: str):
            raise FeishuAPIError("application is not active")

    app = create_app(configured, repository=repository, feishu=InactiveFeishu(), bitable=bitable)
    with TestClient(app, base_url="http://127.0.0.1:8090", client=("127.0.0.1", 1234)) as client:
        start = client.get("/auth/feishu/start", follow_redirects=False)
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        callback = client.get(
            "/auth/feishu/callback", params={"code": "valid-code", "state": state}
        )

    assert callback.status_code == 200
    assert "本机身份初始化成功" in callback.text
    assert dotenv_values(path)["FEISHU_TENANT_KEY"] == TENANT
    assert bitable.granted == []
    users = list(repository.users.values())
    assert len(users) == 1
    assert users[0].authorized


def test_oauth_start_fails_locally_instead_of_redirecting_with_empty_app_id() -> None:
    app = create_app(local_settings(), repository=MemoryRepository(), bitable=FakeBitable())
    with TestClient(app, base_url="http://127.0.0.1:8090", client=("127.0.0.1", 1234)) as client:
        response = client.get("/auth/feishu/start")
    assert response.status_code == 503
