from __future__ import annotations

import json
from uuid import uuid4

import pytest

from app.models import Chat, InboxStatus, User
from app.repository import MemoryRepository
from app.service import MentionProcessor
from app.workers import BackgroundSupervisor, _coalesce_outbox_jobs


class ReconciliationRepository:
    def __init__(self) -> None:
        self.item_id = uuid4()
        self.updates: list[tuple] = []

    async def inbox_reconciliation_state(self) -> dict[str, dict[str, object]]:
        return {
            "rec_current": {
                "id": self.item_id,
                "status": InboxStatus.PENDING.value,
                "note": "",
                "version": 3,
            },
            "rec_stale": {
                "id": uuid4(),
                "status": InboxStatus.PENDING.value,
                "note": "",
                "version": 4,
            },
        }

    async def update_inbox_item(
        self, item_id, status, note, expected_version=None, changed_at=None
    ) -> None:
        self.updates.append((item_id, status, note, expected_version, changed_at))


class ReconciliationBitable:
    async def list_records(self, table_key: str) -> list[dict[str, object]]:
        assert table_key == "inbox"
        return [
            {
                "record_id": "rec_current",
                "fields": {"处理状态": "已处理", "处理备注": "已确认", "版本": 3},
            },
            {
                "record_id": "rec_stale",
                "fields": {"处理状态": "已处理", "处理备注": "过期写入", "版本": 3},
            },
        ]


class ExternalChatFeishu:
    def __init__(self) -> None:
        self.resolved: list[tuple[str, str]] = []

    async def resolve_chat(self, tenant_key: str, chat_id: str) -> Chat:
        self.resolved.append((tenant_key, chat_id))
        return Chat(
            tenant_key=tenant_key,
            chat_id=chat_id,
            name="External customer chat",
            external=True,
        )

    async def list_chat_member_user_ids(self, chat_id: str) -> set[str]:
        return set()


def _direct_mention_event(tenant_key: str, chat_id: str) -> dict:
    return {
        "header": {"tenant_key": tenant_key},
        "event": {
            "sender": {"sender_id": {"user_id": "sender"}},
            "message": {
                "message_id": "message-after-bot-added",
                "chat_id": chat_id,
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": "private external content"}),
                "create_time": "1787673600000",
                "mentions": [
                    {
                        "key": "@_user_1",
                        "name": "User 1",
                        "id": {"user_id": "user-1"},
                    }
                ],
            },
        },
    }


@pytest.mark.asyncio
async def test_reconciliation_pulls_current_base_change_and_ignores_stale_version() -> None:
    repository = ReconciliationRepository()
    supervisor = object.__new__(BackgroundSupervisor)
    supervisor.repository = repository  # type: ignore[assignment]
    supervisor.bitable = ReconciliationBitable()  # type: ignore[assignment]

    await supervisor._pull_bitable_changes()

    assert repository.updates == [(repository.item_id, InboxStatus.DONE, "已确认", 3, None)]


@pytest.mark.asyncio
async def test_bot_added_external_chat_is_resolved_before_first_message() -> None:
    tenant_key = "tenant-a"
    chat_id = "external-chat"
    repository = MemoryRepository()
    await repository.enable_user(
        User(
            tenant_key=tenant_key,
            user_id="user-1",
            enabled=True,
            authorized=True,
        )
    )
    feishu = ExternalChatFeishu()
    supervisor = object.__new__(BackgroundSupervisor)
    supervisor.repository = repository  # type: ignore[assignment]
    supervisor.feishu = feishu  # type: ignore[assignment]
    supervisor.processor = MentionProcessor(
        repository,
        chat_resolver=feishu,
        allowed_tenant_key=tenant_key,
    )

    await supervisor._dispatch_event(
        "im.chat.member.bot.added_v1",
        {"header": {"tenant_key": tenant_key}, "event": {"chat_id": chat_id}},
    )
    await supervisor._dispatch_event(
        "im.message.receive_v1", _direct_mention_event(tenant_key, chat_id)
    )

    chat = await repository.get_chat(tenant_key, chat_id)
    assert feishu.resolved == [(tenant_key, chat_id)]
    assert chat is not None and chat.external and chat.bot_present
    assert repository.sources == {}


@pytest.mark.asyncio
async def test_unknown_deleted_and_disbanded_chats_stay_fail_closed() -> None:
    repository = MemoryRepository()

    deleted = await repository.set_bot_membership("tenant-a", "deleted-chat", False)
    await repository.disband_chat("tenant-a", "disbanded-chat")
    disbanded = await repository.get_chat("tenant-a", "disbanded-chat")
    await repository.set_bot_membership("tenant-a", "disbanded-chat", False)

    assert deleted.external and not deleted.bot_present
    assert disbanded is not None and disbanded.external and disbanded.disbanded


def test_outbox_coalescing_keeps_only_latest_projection_per_entity() -> None:
    first_id, second_id, other_id = uuid4(), uuid4(), uuid4()
    entity_id, other_entity_id = uuid4(), uuid4()
    jobs = [
        {
            "id": first_id,
            "entity_type": "inbox",
            "entity_id": entity_id,
            "table_key": "inbox",
        },
        {
            "id": second_id,
            "entity_type": "inbox",
            "entity_id": entity_id,
            "table_key": "inbox",
        },
        {
            "id": other_id,
            "entity_type": "inbox",
            "entity_id": other_entity_id,
            "table_key": "inbox",
        },
    ]

    latest, superseded = _coalesce_outbox_jobs(jobs)

    assert [job["id"] for job in latest] == [second_id, other_id]
    assert superseded == [first_id]
