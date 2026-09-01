from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.database import create_repository
from app.models import (
    Chat,
    ChatMembership,
    InboxStatus,
    OAuthTokens,
    OAuthUserInfo,
    User,
)
from app.security import TokenCipher
from app.service import MentionProcessor, StatusService
from app.sqlite_repository import SQLiteRepository
from app.workers import BackgroundSupervisor


def _database_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _message_event(tenant: str, chat_id: str, message_id: str) -> dict[str, object]:
    return {
        "header": {
            "tenant_key": tenant,
            "event_id": f"event-{message_id}",
            "event_type": "im.message.receive_v1",
        },
        "event": {
            "sender": {
                "sender_id": {"user_id": "sender"},
                "name": "Sender",
            },
            "message": {
                "message_id": message_id,
                "chat_id": chat_id,
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": "Please review"}),
                "create_time": str(int(datetime.now(UTC).timestamp() * 1000)),
                "mentions": [
                    {
                        "key": f"@_user_{index}",
                        "name": f"User {index}",
                        "id": {"user_id": f"user-{index}", "open_id": f"open-{index}"},
                    }
                    for index in (1, 2)
                ],
            },
        },
    }


class PipelineFeishu:
    pass


class PipelineBitable:
    def __init__(self) -> None:
        self.created: dict[str, list[dict[str, object]]] = {}
        self.updated: dict[str, list[tuple[str, dict[str, object]]]] = {}

    async def batch_create(self, table_key: str, records: list[dict[str, object]]) -> list[str]:
        self.created.setdefault(table_key, []).extend(records)
        return [f"record-{table_key}-{index}" for index in range(len(records))]

    async def batch_update(
        self, table_key: str, records: list[tuple[str, dict[str, object]]]
    ) -> None:
        self.updated.setdefault(table_key, []).extend(records)


@pytest.mark.asyncio
async def test_sqlite_full_message_lifecycle_and_restart_recovery(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path / "mentions.db")
    cipher = TokenCipher("sqlite-integration-test-cipher-secret")
    repo = SQLiteRepository(database_url, cipher)
    await repo.open()
    tenant = "tenant-sqlite"
    chat_id = "chat-sqlite"
    active_users = []
    try:
        for index in (1, 2):
            await repo.enable_user(
                User(
                    tenant_key=tenant,
                    user_id=f"user-{index}",
                    open_id=f"open-{index}",
                    name=f"User {index}",
                )
            )
            active = await repo.activate_user(
                OAuthUserInfo(
                    tenant_key=tenant,
                    user_id=f"user-{index}",
                    open_id=f"open-{index}",
                    name=f"User {index}",
                ),
                OAuthTokens(
                    access_token=f"access-{index}",
                    refresh_token=f"refresh-{index}",
                    expires_in=3600,
                    refresh_expires_in=7200,
                ),
            )
            assert active is not None
            active_users.append(active)
            await repo.replace_user_chats(
                active, [ChatMembership(chat_id=chat_id, name="SQLite Chat")]
            )

        await repo.upsert_chat(
            Chat(
                tenant_key=tenant,
                chat_id=chat_id,
                name="SQLite Chat",
                bot_present=True,
            )
        )
        covered_chat = await repo.set_bot_membership(tenant, chat_id, True)
        assert covered_chat.bot_present
        assert await repo.count_user_coverage(active_users[0].id) == (1, 1)

        setting = await repo.update_user_setting(tenant, "user-1", True)
        assert setting is not None and setting.include_at_all
        at_all_users = await repo.find_at_all_users(tenant, chat_id, {"user-1", "user-2"})
        assert [user.user_id for user in at_all_users] == ["user-1"]

        payload = _message_event(tenant, chat_id, "message-sqlite")
        processor = MentionProcessor(repo, allowed_tenant_key=tenant)
        created = await processor.process_receive_event(payload)
        duplicated = await processor.process_receive_event(payload)

        assert len(created) == 2
        assert duplicated == []
        first = next(item for item in created if item.target_user_id == active_users[0].id)
        second = next(item for item in created if item.target_user_id == active_users[1].id)

        updated = await StatusService(repo).update_from_bitable(
            item_id=first.id,
            status=InboxStatus.DONE,
            note="handled",
            expected_version=first.version,
        )
        second_context = await repo.get_inbox_context(second.id)

        assert updated is not None and updated.status == InboxStatus.DONE
        assert second_context is not None
        assert second_context[0].status == InboxStatus.PENDING
        assert await repo.health()

        outbox = await repo.claim_outbox()
        inbox_job = next(job for job in outbox if job["entity_type"] == "inbox")
        await repo.finish_outbox(
            inbox_job["id"],
            entity_type="inbox",
            entity_id=inbox_job["entity_id"],
            table_key="inbox",
            record_id="record-inbox-1",
            synced_version=1,
        )
        mapping = await repo.get_mapping("inbox", inbox_job["entity_id"], "inbox")
        assert mapping is not None and mapping["record_id"] == "record-inbox-1"
        await repo.finish_superseded_outbox(
            [job["id"] for job in outbox if job["id"] != inbox_job["id"]]
        )
        assert await repo.reconcile_outbox() >= 1

        recalled = await repo.recall_message(tenant, "message-sqlite", datetime.now(UTC))
        recalled_by_user = {item.target_user_id: item for item in recalled}
        assert recalled_by_user[active_users[0].id].status == InboxStatus.DONE
        assert recalled_by_user[active_users[1].id].status == InboxStatus.IGNORED

        unsupported = await repo.set_chat_unsupported(tenant, chat_id, True, "pilot exception")
        assert unsupported is not None and unsupported.unsupported
        await repo.set_chat_unsupported(tenant, chat_id, False, "")
        await repo.set_bot_chats(tenant, {chat_id})
        summary = await repo.coverage_summary()
        assert len(summary) == 2
        assert all(row["covered_groups"] == 1 for row in summary)

        retention_created = await processor.process_receive_event(
            _message_event(tenant, chat_id, "message-for-retention")
        )
        assert len(retention_created) == 2
        purged = await repo.purge_expired_content(datetime.now(UTC))
        assert purged == 1

        assert await repo.enqueue_event("event-once", "im.message.receive_v1", payload)
        assert not await repo.enqueue_event("event-once", "im.message.receive_v1", payload)
        claimed = await repo.claim_events()
        assert len(claimed) == 1
        assert claimed[0].payload == payload
    finally:
        await repo.close()

    recovered = SQLiteRepository(database_url, cipher)
    await recovered.open()
    try:
        claimed_again = await recovered.claim_events()
        assert len(claimed_again) == 1
        assert claimed_again[0].attempts == 2
        await recovered.finish_event(claimed_again[0].id)
        assert await recovered.claim_events() == []
        assert len(await recovered.list_users()) == 2
        await recovered.mark_user_auth_expired(active_users[1].id)
        assert [user.user_id for user in await recovered.list_active_users()] == ["user-1"]
    finally:
        await recovered.close()


def test_repository_factory_keeps_both_database_backends(tmp_path: Path) -> None:
    cipher = TokenCipher("repository-factory-test-cipher-secret")

    sqlite_repo = create_repository(_database_url(tmp_path / "factory.db"), cipher)
    postgres_repo = create_repository("postgresql://user:password@localhost/database", cipher)

    assert isinstance(sqlite_repo, SQLiteRepository)
    assert postgres_repo.__class__.__name__ == "PostgresRepository"

    with pytest.raises(ValueError, match="DATABASE_URL"):
        create_repository("mysql://localhost/database", cipher)


@pytest.mark.asyncio
async def test_sqlite_event_worker_and_bitable_outbox_pipeline(tmp_path: Path) -> None:
    tenant = "tenant-pipeline"
    chat_id = "chat-pipeline"
    repo = SQLiteRepository(
        _database_url(tmp_path / "pipeline.db"),
        TokenCipher("sqlite-pipeline-test-cipher-secret"),
    )
    await repo.open()
    try:
        await repo.enable_user(
            User(
                tenant_key=tenant,
                user_id="user-1",
                open_id="open-1",
                name="User 1",
            )
        )
        active = await repo.activate_user(
            OAuthUserInfo(tenant, "user-1", "open-1", "User 1"),
            OAuthTokens("access", "refresh", 3600, 7200),
        )
        assert active is not None
        await repo.upsert_chat(Chat(tenant, chat_id, "Pipeline Chat", bot_present=True))

        payload = _message_event(tenant, chat_id, "message-pipeline")
        assert await repo.enqueue_event("event-pipeline", "im.message.receive_v1", payload)
        processor = MentionProcessor(repo, allowed_tenant_key=tenant)
        bitable = PipelineBitable()
        supervisor = BackgroundSupervisor(
            Settings(app_env="test", run_background_workers=False),
            repo,
            processor,
            PipelineFeishu(),  # type: ignore[arg-type]
            bitable,  # type: ignore[arg-type]
        )

        claimed = await repo.claim_events()
        assert len(claimed) == 1
        await supervisor._dispatch_event(claimed[0].event_type, claimed[0].payload)
        await repo.finish_event(claimed[0].id)
        await supervisor._flush_outbox(await repo.claim_outbox())

        assert len(bitable.created["inbox"]) == 1
        assert bitable.created["settings"]
        assert bitable.created["users"]
        reconciliation = await repo.inbox_reconciliation_state()
        assert len(reconciliation) == 1
        assert next(iter(reconciliation.values()))["status"] == InboxStatus.PENDING.value
    finally:
        await repo.close()


def test_application_starts_with_sqlite_and_reports_health(tmp_path: Path) -> None:
    database_path = tmp_path / "app-health.db"
    app = create_app(
        Settings(
            app_env="test",
            database_url=_database_url(database_path),
            run_background_workers=False,
        )
    )

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": True}
    assert database_path.is_file()
