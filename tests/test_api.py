from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.models import Chat, ChatMembership, InboxStatus, OAuthTokens, OAuthUserInfo, User
from app.repository import MemoryRepository
from app.service import MentionProcessor
from tests.test_processor import TENANT, message_event


class FakeFeishu:
    def __init__(self) -> None:
        self.activation_messages: list[tuple[str, str]] = []

    async def close(self) -> None:
        return None

    def authorize_url(self, state: str) -> str:
        return f"https://example.test/authorize?state={state}"

    async def exchange_oauth_code(self, code: str) -> OAuthTokens:
        assert code == "valid-code"
        return OAuthTokens("access", "refresh", 7200, 30 * 86400)

    async def get_oauth_user(self, access_token: str) -> OAuthUserInfo:
        assert access_token == "access"
        return OAuthUserInfo(TENANT, "u1", "ou_1", "用户一")

    async def list_user_chats(self, access_token: str) -> list[ChatMembership]:
        return [ChatMembership("oc_internal", "项目群", False)]

    async def get_contact_user(
        self, user_identifier: str, id_type: str = "user_id"
    ) -> OAuthUserInfo:
        if id_type == "open_id":
            return OAuthUserInfo(TENANT, "u1", user_identifier, "用户一")
        return OAuthUserInfo(TENANT, user_identifier, "ou_1", "用户一")

    async def send_activation_message(self, user_id: str, url: str) -> None:
        self.activation_messages.append((user_id, url))

    async def resolve_chat(self, tenant_key: str, chat_id: str):
        raise AssertionError("chat resolution is not expected in this API test")


class FakeBitable:
    def __init__(self) -> None:
        self.granted: list[str] = []
        self.revoked: list[str] = []

    async def close(self) -> None:
        return None

    async def grant_user_access(self, open_id: str) -> None:
        self.granted.append(open_id)

    async def revoke_user_access(self, open_id: str) -> None:
        self.revoked.append(open_id)


def settings() -> Settings:
    return Settings(
        app_env="test",
        run_background_workers=False,
        feishu_tenant_key=TENANT,
        feishu_verification_token="verification-token",
        feishu_require_signature=False,
        admin_api_token="admin-token",
        bitable_callback_token="bitable-token",
        oauth_state_secret="oauth-secret",
        token_encryption_secret="cipher-secret",
        public_base_url="https://mentions.example.com",
    )


def test_event_endpoint_verifies_token_and_deduplicates() -> None:
    repo = MemoryRepository()
    app = create_app(settings(), repository=repo, feishu=FakeFeishu(), bitable=FakeBitable())
    payload = message_event()
    payload["header"]["token"] = "verification-token"

    with TestClient(app) as client:
        first = client.post("/integrations/feishu/events", json=payload)
        second = client.post("/integrations/feishu/events", json=payload)
        rejected_payload = message_event(message_id="om_2")
        rejected_payload["header"]["token"] = "wrong"
        rejected = client.post("/integrations/feishu/events", json=rejected_payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert rejected.status_code == 401
    assert len(repo.events) == 1


def test_admin_enable_and_oauth_activation() -> None:
    repo = MemoryRepository()
    feishu = FakeFeishu()
    bitable = FakeBitable()
    app = create_app(settings(), repository=repo, feishu=feishu, bitable=bitable)

    with TestClient(app) as client:
        enabled = client.post(
            "/admin/users",
            headers={"Authorization": "Bearer admin-token"},
            json={"user_id": "u1"},
        )
        start = client.get("/auth/feishu/start", follow_redirects=False)
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        callback = client.get(
            "/auth/feishu/callback",
            params={"code": "valid-code", "state": state},
        )

    assert enabled.status_code == 200
    assert feishu.activation_messages == [("u1", "https://mentions.example.com/auth/feishu/start")]
    assert callback.status_code == 200
    assert bitable.granted == ["ou_1"]
    user = next(iter(repo.users.values()))
    assert user.authorized
    assert user.open_id == "ou_1"
    assert (TENANT, "oc_internal") in repo.user_chats[user.id]


def test_bitable_status_callback_updates_only_current_version() -> None:
    repo = MemoryRepository()
    app = create_app(settings(), repository=repo, feishu=FakeFeishu(), bitable=FakeBitable())

    async def prepare():
        user_response = await repo.enable_user(
            User(
                tenant_key=TENANT,
                user_id="u1",
                open_id="ou_1",
                authorized=True,
            )
        )
        repo.chats[(TENANT, "oc_internal")] = Chat(
            TENANT, "oc_internal", "项目群", bot_present=True
        )
        items = await MentionProcessor(repo).process_receive_event(message_event())
        return user_response, items[0]

    import asyncio

    _, item = asyncio.run(prepare())
    body = {
        "item_id": str(item.id),
        "record_id": "rec_1",
        "status": "已处理",
        "note": "已答复",
        "version": 1,
    }
    with TestClient(app) as client:
        updated = client.post(
            "/integrations/bitable/status",
            headers={"Authorization": "Bearer bitable-token"},
            json=body,
        )
        duplicate = client.post(
            "/integrations/bitable/status",
            headers={"Authorization": "Bearer bitable-token"},
            json=body,
        )
        stale_body = dict(body)
        stale_body["status"] = "处理中"
        stale = client.post(
            "/integrations/bitable/status",
            headers={"Authorization": "Bearer bitable-token"},
            json=stale_body,
        )

    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    assert duplicate.status_code == 200
    assert duplicate.json()["version"] == 2
    assert stale.status_code == 409


def test_bitable_projection_callback_is_a_noop_instead_of_a_sync_loop() -> None:
    repo = MemoryRepository()
    app = create_app(settings(), repository=repo, feishu=FakeFeishu(), bitable=FakeBitable())

    async def prepare():
        await repo.enable_user(
            User(
                tenant_key=TENANT,
                user_id="u1",
                open_id="ou_1",
                authorized=True,
            )
        )
        repo.chats[(TENANT, "oc_internal")] = Chat(
            TENANT, "oc_internal", "项目群", bot_present=True
        )
        item = (await MentionProcessor(repo).process_receive_event(message_event()))[0]
        item.status = InboxStatus.DONE
        item.note = "已答复"
        item.version = 2
        return item

    import asyncio

    item = asyncio.run(prepare())
    outbox_count = len(repo.outbox)
    with TestClient(app) as client:
        response = client.post(
            "/integrations/bitable/status",
            headers={"Authorization": "Bearer bitable-token"},
            json={
                "item_id": str(item.id),
                "record_id": "rec_1",
                "status": "已处理",
                "note": "已答复",
                "version": 2,
            },
        )

    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert len(repo.outbox) == outbox_count


def test_admin_endpoint_requires_bearer_token() -> None:
    app = create_app(
        settings(), repository=MemoryRepository(), feishu=FakeFeishu(), bitable=FakeBitable()
    )
    with TestClient(app) as client:
        response = client.get("/admin/users")
    assert response.status_code == 401


def test_health_check_returns_service_unavailable_when_database_is_down() -> None:
    class DegradedRepository(MemoryRepository):
        async def health(self) -> bool:
            return False

    app = create_app(
        settings(),
        repository=DegradedRepository(),
        feishu=FakeFeishu(),
        bitable=FakeBitable(),
    )
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "database": False}


def test_user_management_table_can_enable_by_open_id_and_disable_access() -> None:
    repo = MemoryRepository()
    bitable = FakeBitable()
    app = create_app(settings(), repository=repo, feishu=FakeFeishu(), bitable=bitable)
    headers = {"Authorization": "Bearer bitable-token"}

    with TestClient(app) as client:
        enabled = client.post(
            "/integrations/bitable/users",
            headers=headers,
            json={"enabled": True, "open_id": "ou_1", "record_id": "rec_user_1"},
        )
        disabled = client.post(
            "/integrations/bitable/users",
            headers=headers,
            json={"enabled": False, "open_id": "ou_1", "record_id": "rec_user_1"},
        )

    assert enabled.status_code == 200
    assert enabled.json()["user_id"] == "u1"
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert bitable.revoked == ["ou_1"]
    user = next(iter(repo.users.values()))
    assert repo.mappings[("user", user.id, "users")] == "rec_user_1"
