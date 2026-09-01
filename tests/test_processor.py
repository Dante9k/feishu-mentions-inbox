from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.models import Chat, InboxStatus, MentionType, SourceState, User
from app.repository import MemoryRepository
from app.service import MentionProcessor, StatusService

TENANT = "tenant-a"
CHAT_ID = "oc_internal"


def message_event(
    *,
    message_id: str = "om_1",
    chat_type: str = "group",
    mentions: list[dict] | None = None,
    content: str = "@_user_1 请确认",
) -> dict:
    return {
        "schema": "2.0",
        "header": {
            "tenant_key": TENANT,
            "event_type": "im.message.receive_v1",
            "event_id": f"event-{message_id}",
        },
        "event": {
            "sender": {
                "sender_id": {"user_id": "sender-1", "open_id": "ou_sender"},
                "sender_type": "user",
            },
            "message": {
                "message_id": message_id,
                "chat_id": CHAT_ID,
                "chat_type": chat_type,
                "message_type": "text",
                "content": json.dumps({"text": content}, ensure_ascii=False),
                "create_time": "1787673600000",
                "mentions": mentions
                or [
                    {
                        "key": "@_user_1",
                        "id": {"user_id": "u1", "open_id": "ou_1"},
                        "name": "用户一",
                    }
                ],
            },
        },
    }


async def configured_repo() -> tuple[MemoryRepository, list[User]]:
    repo = MemoryRepository()
    users = []
    for index in range(1, 4):
        user = User(
            tenant_key=TENANT,
            user_id=f"u{index}",
            open_id=f"ou_{index}",
            name=f"用户{index}",
            enabled=True,
            authorized=True,
        )
        await repo.enable_user(user)
        users.append(user)
    repo.chats[(TENANT, CHAT_ID)] = Chat(
        tenant_key=TENANT,
        chat_id=CHAT_ID,
        name="项目群",
        bot_present=True,
    )
    return repo, users


@pytest.mark.asyncio
async def test_multi_user_mentions_create_independent_items() -> None:
    repo, users = await configured_repo()
    payload = message_event(
        mentions=[
            {"key": f"@_user_{i}", "id": {"user_id": f"u{i}"}, "name": f"用户{i}"}
            for i in range(1, 4)
        ],
        content="@_user_1 @_user_2 @_user_3 请分别确认",
    )

    items = await MentionProcessor(repo, allowed_tenant_key=TENANT).process_receive_event(payload)

    assert len(items) == 3
    assert {item.target_user_id for item in items} == {user.id for user in users}
    first, second = items[:2]
    updated = await StatusService(repo).update_from_bitable(
        item_id=first.id,
        status=InboxStatus.DONE,
        note="已完成",
        expected_version=1,
    )
    assert updated and updated.status == InboxStatus.DONE
    assert repo.items[second.id].status == InboxStatus.PENDING


@pytest.mark.asyncio
async def test_later_bitable_edit_wins_even_if_visible_version_is_stale() -> None:
    repo, _ = await configured_repo()
    item = (await MentionProcessor(repo).process_receive_event(message_event()))[0]
    first_change = datetime.now(UTC) + timedelta(seconds=1)
    second_change = first_change + timedelta(seconds=1)

    first = await repo.update_inbox_item(
        item.id,
        InboxStatus.IN_PROGRESS,
        "先处理",
        expected_version=1,
        changed_at=first_change,
    )
    second = await repo.update_inbox_item(
        item.id,
        InboxStatus.DONE,
        "后完成",
        expected_version=1,
        changed_at=second_change,
    )
    older = await repo.update_inbox_item(
        item.id,
        InboxStatus.PENDING,
        "乱序旧请求",
        expected_version=1,
        changed_at=first_change,
    )

    assert first and first.version == 3
    assert second and second.status == InboxStatus.DONE
    assert second.version == 3
    assert older is None


@pytest.mark.asyncio
async def test_duplicate_delivery_creates_no_duplicate_items() -> None:
    repo, _ = await configured_repo()
    processor = MentionProcessor(repo, allowed_tenant_key=TENANT)
    payload = message_event()

    first = await processor.process_receive_event(payload)
    second = await processor.process_receive_event(payload)

    assert len(first) == 1
    assert second == []
    assert len(repo.items) == 1


@pytest.mark.asyncio
async def test_retention_clears_database_and_projection_content() -> None:
    repo, _ = await configured_repo()
    item = (await MentionProcessor(repo).process_receive_event(message_event()))[0]
    source = repo.sources[(TENANT, "om_1")]
    source.sent_at = datetime.now(UTC) - timedelta(days=181)
    outbox_before = len(repo.outbox)

    purged = await repo.purge_expired_content(datetime.now(UTC) - timedelta(days=180))

    assert purged == 1
    assert source.content == ""
    assert item.version == 2
    assert len(repo.outbox) == outbox_before + 1


@pytest.mark.asyncio
async def test_inactive_user_is_ignored() -> None:
    repo, users = await configured_repo()
    users[0].authorized = False

    items = await MentionProcessor(repo).process_receive_event(message_event())

    assert items == []


@pytest.mark.asyncio
async def test_manually_unsupported_chat_is_not_collected() -> None:
    repo, _ = await configured_repo()
    chat = await repo.set_chat_unsupported(TENANT, CHAT_ID, True, "该群禁止机器人")

    items = await MentionProcessor(repo).process_receive_event(message_event())

    assert chat and chat.coverage_status.value == "无法覆盖"
    assert items == []


@pytest.mark.asyncio
async def test_at_all_uses_membership_and_personal_setting() -> None:
    repo, users = await configured_repo()
    users[0].include_at_all = True
    users[1].include_at_all = True
    repo.add_memberships(users[0], [repo.chats[(TENANT, CHAT_ID)]])
    # User 2 enabled the option but is not a member; user 3 is a member but left it disabled.
    repo.add_memberships(users[2], [repo.chats[(TENANT, CHAT_ID)]])
    payload = message_event(
        mentions=[{"key": "@_all", "id": {"user_id": "all"}, "name": "所有人"}],
        content="@_all 请阅读",
    )

    items = await MentionProcessor(repo).process_receive_event(payload)

    assert len(items) == 1
    assert items[0].target_user_id == users[0].id
    assert items[0].mention_type == MentionType.ALL


@pytest.mark.asyncio
async def test_at_all_checks_current_chat_members_from_feishu() -> None:
    repo, users = await configured_repo()
    for user in users[:2]:
        user.include_at_all = True
        repo.add_memberships(user, [repo.chats[(TENANT, CHAT_ID)]])

    class CurrentMemberResolver:
        async def resolve_chat(self, tenant_key: str, chat_id: str) -> Chat:
            raise AssertionError("chat is already cached")

        async def list_chat_member_user_ids(self, chat_id: str) -> set[str]:
            assert chat_id == CHAT_ID
            return {"u1"}

    payload = message_event(
        mentions=[{"key": "@_all", "id": {"user_id": "all"}, "name": "所有人"}],
        content="@_all 请阅读",
    )

    items = await MentionProcessor(
        repo, chat_resolver=CurrentMemberResolver()
    ).process_receive_event(payload)

    assert [item.target_user_id for item in items] == [users[0].id]


@pytest.mark.asyncio
async def test_direct_mention_takes_precedence_over_at_all() -> None:
    repo, users = await configured_repo()
    users[0].include_at_all = True
    repo.add_memberships(users[0], [repo.chats[(TENANT, CHAT_ID)]])
    payload = message_event(
        mentions=[
            {"key": "@_all", "id": {"user_id": "all"}, "name": "所有人"},
            {"key": "@_user_1", "id": {"user_id": "u1"}, "name": "用户一"},
        ],
        content="@_all @_user_1 请优先处理",
    )

    items = await MentionProcessor(repo).process_receive_event(payload)

    assert len(items) == 1
    assert items[0].mention_type == MentionType.DIRECT


@pytest.mark.asyncio
async def test_non_group_and_external_group_are_rejected() -> None:
    repo, _ = await configured_repo()
    processor = MentionProcessor(repo)

    assert await processor.process_receive_event(message_event(chat_type="p2p")) == []
    repo.chats[(TENANT, CHAT_ID)].external = True
    assert await processor.process_receive_event(message_event(message_id="om_2")) == []


@pytest.mark.asyncio
async def test_recall_clears_content_and_ignores_open_item() -> None:
    repo, _ = await configured_repo()
    processor = MentionProcessor(repo)
    [item] = await processor.process_receive_event(message_event())
    recall = {
        "header": {"tenant_key": TENANT, "event_type": "im.message.recalled_v1"},
        "event": {"message_id": "om_1", "recall_time": "1787673660000"},
    }

    changed = await processor.process_recall_event(recall)

    assert [changed_item.id for changed_item in changed] == [item.id]
    source = repo.sources[(TENANT, "om_1")]
    assert source.content == ""
    assert source.source_state == SourceState.RECALLED
    assert repo.items[item.id].status == InboxStatus.IGNORED


@pytest.mark.asyncio
async def test_at_all_fanout_for_two_hundred_users_is_idempotent() -> None:
    repo = MemoryRepository()
    chat = Chat(tenant_key=TENANT, chat_id=CHAT_ID, name="全员群", bot_present=True)
    repo.chats[(TENANT, CHAT_ID)] = chat
    for index in range(200):
        user = User(
            tenant_key=TENANT,
            user_id=f"user-{index}",
            open_id=f"open-{index}",
            enabled=True,
            authorized=True,
            include_at_all=True,
        )
        await repo.enable_user(user)
        repo.add_memberships(user, [chat])
    payload = message_event(
        message_id="om_all_200",
        mentions=[{"key": "@_all", "id": {"user_id": "all"}, "name": "所有人"}],
        content="@_all 全员通知",
    )
    processor = MentionProcessor(repo)

    created = await processor.process_receive_event(payload)
    duplicated = await processor.process_receive_event(payload)

    assert len(created) == 200
    assert duplicated == []
    assert len(repo.items) == 200
