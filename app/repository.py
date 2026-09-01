from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from .models import (
    TERMINAL_INBOX_STATUSES,
    Chat,
    ChatMembership,
    InboxItem,
    InboxStatus,
    IncomingMessage,
    MentionType,
    OAuthTokens,
    OAuthUserInfo,
    SourceMessage,
    SourceState,
    User,
)


class Repository(Protocol):
    async def enqueue_event(
        self, event_key: str, event_type: str, payload: dict[str, Any]
    ) -> bool: ...

    async def find_active_users(
        self, tenant_key: str, user_ids: set[str], open_ids: set[str]
    ) -> list[User]: ...

    async def find_at_all_users(
        self,
        tenant_key: str,
        chat_id: str,
        member_user_ids: set[str] | None = None,
    ) -> list[User]: ...

    async def get_chat(self, tenant_key: str, chat_id: str) -> Chat | None: ...

    async def upsert_chat(self, chat: Chat) -> Chat: ...

    async def save_message_targets(
        self,
        incoming: IncomingMessage,
        chat: Chat,
        normalized_content: str,
        targets: dict[UUID, tuple[User, MentionType]],
    ) -> tuple[SourceMessage, list[InboxItem]]: ...

    async def recall_message(
        self, tenant_key: str, message_id: str, recalled_at: datetime
    ) -> list[InboxItem]: ...

    async def get_inbox_context(
        self, item_id: UUID
    ) -> tuple[InboxItem, SourceMessage, User] | None: ...

    async def update_inbox_item(
        self,
        item_id: UUID,
        status: InboxStatus,
        note: str,
        expected_version: int | None = None,
        changed_at: datetime | None = None,
    ) -> InboxItem | None: ...

    async def enable_user(self, user: User, bitable_record_id: str = "") -> User: ...

    async def disable_user(
        self, tenant_key: str, user_id: str, bitable_record_id: str = ""
    ) -> User | None: ...

    async def get_enabled_user(self, tenant_key: str, user_id: str) -> User | None: ...

    async def activate_user(self, info: OAuthUserInfo, tokens: OAuthTokens) -> User | None: ...

    async def list_active_users(self) -> list[User]: ...

    async def list_users(self) -> list[User]: ...

    async def update_user_setting(
        self, tenant_key: str, user_id: str, include_at_all: bool
    ) -> User | None: ...

    async def replace_user_chats(
        self, user: User, memberships: list[ChatMembership]
    ) -> list[Chat]: ...

    async def set_bot_chats(self, tenant_key: str, chat_ids: set[str]) -> None: ...

    async def set_bot_membership(
        self, tenant_key: str, chat_id: str, present: bool, name: str = ""
    ) -> Chat: ...

    async def disband_chat(self, tenant_key: str, chat_id: str) -> None: ...

    async def set_chat_unsupported(
        self, tenant_key: str, chat_id: str, unsupported: bool, reason: str
    ) -> Chat | None: ...

    async def purge_expired_content(self, before: datetime) -> int: ...


class MemoryRepository:
    """Deterministic repository used by unit tests and local domain experiments."""

    def __init__(self) -> None:
        self.users: dict[UUID, User] = {}
        self.chats: dict[tuple[str, str], Chat] = {}
        self.user_chats: dict[UUID, set[tuple[str, str]]] = defaultdict(set)
        self.sources: dict[tuple[str, str], SourceMessage] = {}
        self.items: dict[UUID, InboxItem] = {}
        self.item_keys: dict[tuple[UUID, UUID], UUID] = {}
        self.events: dict[str, tuple[str, dict[str, Any]]] = {}
        self.outbox: list[tuple[str, UUID]] = []
        self.mappings: dict[tuple[str, UUID, str], str] = {}

    async def enqueue_event(self, event_key: str, event_type: str, payload: dict[str, Any]) -> bool:
        if event_key in self.events:
            return False
        self.events[event_key] = (event_type, payload)
        return True

    async def find_active_users(
        self, tenant_key: str, user_ids: set[str], open_ids: set[str]
    ) -> list[User]:
        return [
            user
            for user in self.users.values()
            if user.tenant_key == tenant_key
            and user.active
            and (user.user_id in user_ids or (user.open_id and user.open_id in open_ids))
        ]

    async def find_at_all_users(
        self,
        tenant_key: str,
        chat_id: str,
        member_user_ids: set[str] | None = None,
    ) -> list[User]:
        chat_key = (tenant_key, chat_id)
        return [
            user
            for user in self.users.values()
            if user.active
            and user.include_at_all
            and user.tenant_key == tenant_key
            and chat_key in self.user_chats[user.id]
            and (member_user_ids is None or user.user_id in member_user_ids)
        ]

    async def get_chat(self, tenant_key: str, chat_id: str) -> Chat | None:
        return self.chats.get((tenant_key, chat_id))

    async def upsert_chat(self, chat: Chat) -> Chat:
        key = (chat.tenant_key, chat.chat_id)
        existing = self.chats.get(key)
        if existing:
            existing.name = chat.name or existing.name
            existing.external = chat.external
            existing.bot_present = chat.bot_present or existing.bot_present
            existing.disbanded = chat.disbanded
            existing.last_checked_at = chat.last_checked_at
            return existing
        self.chats[key] = chat
        return chat

    async def save_message_targets(
        self,
        incoming: IncomingMessage,
        chat: Chat,
        normalized_content: str,
        targets: dict[UUID, tuple[User, MentionType]],
    ) -> tuple[SourceMessage, list[InboxItem]]:
        source_key = (incoming.tenant_key, incoming.message_id)
        source = self.sources.get(source_key)
        if source is None:
            source = SourceMessage(
                tenant_key=incoming.tenant_key,
                message_id=incoming.message_id,
                chat_id=incoming.chat_id,
                chat_name=chat.name or incoming.chat_id,
                sender_id=incoming.sender_user_id or incoming.sender_open_id,
                sender_name=incoming.sender_name,
                message_type=incoming.message_type,
                content=normalized_content,
                sent_at=incoming.create_time,
                root_id=incoming.root_id,
                parent_id=incoming.parent_id,
            )
            self.sources[source_key] = source
        created: list[InboxItem] = []
        for user_pk, (_, mention_type) in targets.items():
            key = (source.id, user_pk)
            if key in self.item_keys:
                continue
            item = InboxItem(
                source_message_id=source.id,
                target_user_id=user_pk,
                mention_type=mention_type,
            )
            self.items[item.id] = item
            self.item_keys[key] = item.id
            self.outbox.append(("inbox", item.id))
            created.append(item)
        return source, created

    async def recall_message(
        self, tenant_key: str, message_id: str, recalled_at: datetime
    ) -> list[InboxItem]:
        source = self.sources.get((tenant_key, message_id))
        if not source:
            return []
        source.content = ""
        source.source_state = SourceState.RECALLED
        source.recalled_at = recalled_at
        changed: list[InboxItem] = []
        for item in self.items.values():
            if item.source_message_id != source.id:
                continue
            if item.status not in TERMINAL_INBOX_STATUSES:
                item.status = InboxStatus.IGNORED
                item.handled_at = recalled_at
                item.version += 1
                item.updated_at = recalled_at
            self.outbox.append(("inbox", item.id))
            changed.append(item)
        return changed

    async def get_inbox_context(
        self, item_id: UUID
    ) -> tuple[InboxItem, SourceMessage, User] | None:
        item = self.items.get(item_id)
        if not item:
            return None
        source = next(
            source for source in self.sources.values() if source.id == item.source_message_id
        )
        return item, source, self.users[item.target_user_id]

    async def update_inbox_item(
        self,
        item_id: UUID,
        status: InboxStatus,
        note: str,
        expected_version: int | None = None,
        changed_at: datetime | None = None,
    ) -> InboxItem | None:
        item = self.items.get(item_id)
        if not item:
            return None
        if item.status == status and item.note == note:
            return item
        if (
            expected_version is not None
            and item.version != expected_version
            and (changed_at is None or changed_at <= item.updated_at)
        ):
            return None
        item.status = status
        item.note = note
        item.version += 1
        item.updated_at = max(datetime.now(UTC), changed_at or datetime.min.replace(tzinfo=UTC))
        item.handled_at = item.updated_at if status in TERMINAL_INBOX_STATUSES else None
        self.outbox.append(("inbox", item.id))
        return item

    async def enable_user(self, user: User, bitable_record_id: str = "") -> User:
        for existing in self.users.values():
            if existing.tenant_key == user.tenant_key and existing.user_id == user.user_id:
                existing.enabled = True
                existing.departed = False
                existing.name = user.name or existing.name
                existing.open_id = user.open_id or existing.open_id
                self._bind_user_mapping(existing.id, bitable_record_id)
                return existing
        self.users[user.id] = user
        self._bind_user_mapping(user.id, bitable_record_id)
        self.outbox.append(("user", user.id))
        return user

    async def disable_user(
        self, tenant_key: str, user_id: str, bitable_record_id: str = ""
    ) -> User | None:
        user = next(
            (
                item
                for item in self.users.values()
                if item.tenant_key == tenant_key and item.user_id == user_id
            ),
            None,
        )
        if not user:
            return None
        self._bind_user_mapping(user.id, bitable_record_id)
        if not user.enabled and not user.authorized:
            return user
        user.enabled = False
        user.authorized = False
        user.access_token = ""
        user.refresh_token = ""
        self.outbox.append(("user", user.id))
        return user

    def _bind_user_mapping(self, user_pk: UUID, record_id: str) -> None:
        if not record_id:
            return
        key = ("user", user_pk, "users")
        existing = self.mappings.get(key)
        if existing and existing != record_id:
            raise ValueError("user is already mapped to another Bitable record")
        for mapping_key, mapped_record_id in self.mappings.items():
            if mapping_key != key and mapped_record_id == record_id:
                raise ValueError("Bitable record is already mapped to another entity")
        self.mappings[key] = record_id

    async def get_enabled_user(self, tenant_key: str, user_id: str) -> User | None:
        return next(
            (
                user
                for user in self.users.values()
                if user.tenant_key == tenant_key and user.user_id == user_id and user.enabled
            ),
            None,
        )

    async def activate_user(self, info: OAuthUserInfo, tokens: OAuthTokens) -> User | None:
        user = await self.get_enabled_user(info.tenant_key, info.user_id)
        if not user:
            return None
        user.open_id = info.open_id
        user.name = info.name or user.name
        user.authorized = True
        user.access_token = tokens.access_token
        user.refresh_token = tokens.refresh_token
        now = datetime.now(UTC)
        user.access_token_expires_at = datetime.fromtimestamp(
            now.timestamp() + tokens.expires_in, tz=UTC
        )
        user.refresh_token_expires_at = datetime.fromtimestamp(
            now.timestamp() + tokens.refresh_expires_in, tz=UTC
        )
        self.outbox.append(("user", user.id))
        return user

    async def list_active_users(self) -> list[User]:
        return [user for user in self.users.values() if user.active]

    async def list_users(self) -> list[User]:
        return sorted(self.users.values(), key=lambda user: (user.name, user.user_id))

    async def update_user_setting(
        self, tenant_key: str, user_id: str, include_at_all: bool
    ) -> User | None:
        user = await self.get_enabled_user(tenant_key, user_id)
        if not user:
            return None
        if user.include_at_all == include_at_all:
            return user
        user.include_at_all = include_at_all
        self.outbox.append(("user", user.id))
        return user

    async def replace_user_chats(self, user: User, memberships: list[ChatMembership]) -> list[Chat]:
        keys: set[tuple[str, str]] = set()
        chats: list[Chat] = []
        for membership in memberships:
            existing = self.chats.get((user.tenant_key, membership.chat_id))
            chat = await self.upsert_chat(
                Chat(
                    tenant_key=user.tenant_key,
                    chat_id=membership.chat_id,
                    name=membership.name,
                    external=membership.external,
                    bot_present=existing.bot_present if existing else False,
                    last_checked_at=datetime.now(UTC),
                )
            )
            keys.add((user.tenant_key, membership.chat_id))
            chats.append(chat)
        self.user_chats[user.id] = keys
        user.last_coverage_check_at = datetime.now(UTC)
        return chats

    async def set_bot_chats(self, tenant_key: str, chat_ids: set[str]) -> None:
        for (chat_tenant, chat_id), chat in self.chats.items():
            if chat_tenant == tenant_key:
                chat.bot_present = chat_id in chat_ids
                if chat.bot_present:
                    chat.unsupported = False
                    chat.unsupported_reason = ""

    async def set_bot_membership(
        self, tenant_key: str, chat_id: str, present: bool, name: str = ""
    ) -> Chat:
        chat = self.chats.get((tenant_key, chat_id)) or Chat(
            tenant_key=tenant_key, chat_id=chat_id, name=name or chat_id
        )
        chat.bot_present = present
        chat.disbanded = False
        if present:
            chat.unsupported = False
            chat.unsupported_reason = ""
        self.chats[(tenant_key, chat_id)] = chat
        return chat

    async def disband_chat(self, tenant_key: str, chat_id: str) -> None:
        chat = self.chats.get((tenant_key, chat_id))
        if chat:
            chat.disbanded = True
            chat.bot_present = False

    async def set_chat_unsupported(
        self, tenant_key: str, chat_id: str, unsupported: bool, reason: str
    ) -> Chat | None:
        chat = self.chats.get((tenant_key, chat_id))
        if not chat:
            return None
        chat.unsupported = unsupported
        chat.unsupported_reason = reason if unsupported else ""
        return chat

    async def purge_expired_content(self, before: datetime) -> int:
        count = 0
        for source in self.sources.values():
            if source.sent_at < before and source.content:
                source.content = ""
                count += 1
                for item in self.items.values():
                    if item.source_message_id == source.id:
                        item.version += 1
                        self.outbox.append(("inbox", item.id))
        return count

    def add_memberships(self, user: User, chats: Iterable[Chat]) -> None:
        for chat in chats:
            self.chats[(chat.tenant_key, chat.chat_id)] = chat
            self.user_chats[user.id].add((chat.tenant_key, chat.chat_id))

    def get_user(self, user_id: UUID) -> User:
        return self.users[user_id]
