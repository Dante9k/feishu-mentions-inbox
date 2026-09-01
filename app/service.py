from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from .models import (
    Chat,
    InboxItem,
    InboxStatus,
    MentionType,
    User,
    parse_incoming_message,
    parse_milliseconds,
)
from .normalizer import normalize_message_content
from .repository import Repository


class ChatResolver(Protocol):
    async def resolve_chat(self, tenant_key: str, chat_id: str) -> Chat: ...

    async def list_chat_member_user_ids(self, chat_id: str) -> set[str]: ...


class MentionProcessor:
    def __init__(
        self,
        repository: Repository,
        chat_resolver: ChatResolver | None = None,
        allowed_tenant_key: str = "",
    ):
        self._repository = repository
        self._chat_resolver = chat_resolver
        self._allowed_tenant_key = allowed_tenant_key

    async def process_receive_event(self, payload: dict) -> list[InboxItem]:
        incoming = parse_incoming_message(payload)
        if not incoming.message_id or not incoming.chat_id:
            return []
        if self._allowed_tenant_key and incoming.tenant_key != self._allowed_tenant_key:
            return []
        if not incoming.is_group_message:
            return []

        chat = await self._repository.get_chat(incoming.tenant_key, incoming.chat_id)
        if chat is None and self._chat_resolver is not None:
            chat = await self._chat_resolver.resolve_chat(incoming.tenant_key, incoming.chat_id)
            chat = await self._repository.upsert_chat(chat)
        if chat is None:
            chat = await self._repository.upsert_chat(
                Chat(
                    tenant_key=incoming.tenant_key,
                    chat_id=incoming.chat_id,
                    name=incoming.chat_id,
                    bot_present=True,
                )
            )
        if chat.external or chat.disbanded or chat.unsupported:
            return []

        direct_mentions = [mention for mention in incoming.mentions if not mention.is_all]
        user_ids = {mention.user_id for mention in direct_mentions if mention.user_id}
        open_ids = {mention.open_id for mention in direct_mentions if mention.open_id}
        direct_users = await self._repository.find_active_users(
            incoming.tenant_key, user_ids, open_ids
        )
        targets: dict[UUID, tuple[User, MentionType]] = {
            user.id: (user, MentionType.DIRECT) for user in direct_users
        }

        if any(mention.is_all for mention in incoming.mentions):
            member_user_ids = None
            if self._chat_resolver is not None:
                member_user_ids = await self._chat_resolver.list_chat_member_user_ids(
                    incoming.chat_id
                )
            for user in await self._repository.find_at_all_users(
                incoming.tenant_key, incoming.chat_id, member_user_ids
            ):
                targets.setdefault(user.id, (user, MentionType.ALL))

        if not targets:
            return []

        normalized = normalize_message_content(
            incoming.message_type, incoming.content, incoming.mentions
        )
        _, created = await self._repository.save_message_targets(
            incoming, chat, normalized, targets
        )
        return created

    async def process_recall_event(self, payload: dict) -> list[InboxItem]:
        header = payload.get("header") or {}
        event = payload.get("event") or {}
        tenant_key = str(header.get("tenant_key") or "")
        if self._allowed_tenant_key and tenant_key != self._allowed_tenant_key:
            return []
        message_id = str(event.get("message_id") or "")
        if not message_id:
            return []
        recalled_at = parse_milliseconds(event.get("recall_time"))
        return await self._repository.recall_message(tenant_key, message_id, recalled_at)


class StatusService:
    def __init__(self, repository: Repository):
        self._repository = repository

    async def update_from_bitable(
        self,
        *,
        item_id: UUID,
        status: InboxStatus,
        note: str,
        expected_version: int | None,
        changed_at: datetime | None = None,
    ) -> InboxItem | None:
        return await self._repository.update_inbox_item(
            item_id, status, note[:4000], expected_version, changed_at
        )


def event_key(payload: dict) -> tuple[str, str]:
    header = payload.get("header") or {}
    event_type = str(header.get("event_type") or payload.get("type") or "")
    event_id = str(header.get("event_id") or "")
    event = payload.get("event") or {}
    if event_type == "im.message.receive_v1":
        message_id = str((event.get("message") or {}).get("message_id") or "")
        return f"receive:{message_id or event_id}", event_type
    if event_type == "im.message.recalled_v1":
        message_id = str(event.get("message_id") or "")
        return f"recall:{message_id or event_id}", event_type
    return f"event:{event_id}", event_type


def now_utc() -> datetime:
    return datetime.now(UTC)
