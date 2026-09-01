from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class InboxStatus(StrEnum):
    PENDING = "待处理"
    IN_PROGRESS = "处理中"
    DONE = "已处理"
    IGNORED = "忽略"


class MentionType(StrEnum):
    DIRECT = "直接@我"
    ALL = "@所有人"


class SourceState(StrEnum):
    ACTIVE = "有效"
    RECALLED = "已撤回"


class CoverageStatus(StrEnum):
    COVERED = "已覆盖"
    BOT_MISSING = "待加机器人"
    UNSUPPORTED = "无法覆盖"
    AUTH_EXPIRED = "授权失效"


class JobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


TERMINAL_INBOX_STATUSES = {InboxStatus.DONE, InboxStatus.IGNORED}


@dataclass(slots=True)
class User:
    tenant_key: str
    user_id: str
    open_id: str = ""
    name: str = ""
    id: UUID = field(default_factory=uuid4)
    enabled: bool = True
    authorized: bool = False
    include_at_all: bool = False
    departed: bool = False
    access_token: str = ""
    refresh_token: str = ""
    access_token_expires_at: datetime | None = None
    refresh_token_expires_at: datetime | None = None
    last_coverage_check_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.enabled and self.authorized and not self.departed


@dataclass(slots=True)
class Chat:
    tenant_key: str
    chat_id: str
    name: str
    id: UUID = field(default_factory=uuid4)
    external: bool = False
    bot_present: bool = False
    disbanded: bool = False
    unsupported: bool = False
    unsupported_reason: str = ""
    last_checked_at: datetime | None = None

    @property
    def coverage_status(self) -> CoverageStatus:
        if self.external or self.disbanded or self.unsupported:
            return CoverageStatus.UNSUPPORTED
        return CoverageStatus.COVERED if self.bot_present else CoverageStatus.BOT_MISSING


@dataclass(frozen=True, slots=True)
class Mention:
    key: str
    name: str
    user_id: str = ""
    open_id: str = ""
    union_id: str = ""

    @property
    def is_all(self) -> bool:
        normalized_key = self.key.strip().lower()
        normalized_name = self.name.strip().lower()
        ids = {self.user_id.strip().lower(), self.open_id.strip().lower()}
        return (
            normalized_key in {"@_all", "@all", "all"}
            or normalized_name in {"所有人", "全体成员", "all", "everyone"}
            or "all" in ids
        )


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    tenant_key: str
    message_id: str
    chat_id: str
    chat_type: str
    message_type: str
    content: str
    create_time: datetime
    sender_user_id: str = ""
    sender_open_id: str = ""
    sender_name: str = ""
    root_id: str = ""
    parent_id: str = ""
    mentions: tuple[Mention, ...] = ()

    @property
    def is_group_message(self) -> bool:
        return self.chat_type in {"group", "topic_group"}


@dataclass(slots=True)
class SourceMessage:
    tenant_key: str
    message_id: str
    chat_id: str
    chat_name: str
    sender_id: str
    sender_name: str
    message_type: str
    content: str
    sent_at: datetime
    root_id: str = ""
    parent_id: str = ""
    id: UUID = field(default_factory=uuid4)
    source_state: SourceState = SourceState.ACTIVE
    recalled_at: datetime | None = None

    @property
    def locator(self) -> str:
        sender = self.sender_name or self.sender_id or "未知发送人"
        timestamp = self.sent_at.astimezone(UTC).isoformat()
        return f"{self.chat_name} | {sender} | {timestamp} | {self.message_id}"


@dataclass(slots=True)
class InboxItem:
    source_message_id: UUID
    target_user_id: UUID
    mention_type: MentionType
    id: UUID = field(default_factory=uuid4)
    status: InboxStatus = InboxStatus.PENDING
    note: str = ""
    handled_at: datetime | None = None
    version: int = 1
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class OAuthTokens:
    access_token: str
    refresh_token: str
    expires_in: int
    refresh_expires_in: int


@dataclass(frozen=True, slots=True)
class OAuthUserInfo:
    tenant_key: str
    user_id: str
    open_id: str
    name: str


@dataclass(frozen=True, slots=True)
class ChatMembership:
    chat_id: str
    name: str
    external: bool = False


@dataclass(slots=True)
class ClaimedJob:
    id: UUID
    event_type: str
    payload: dict[str, Any]
    attempts: int


def parse_milliseconds(value: str | int | None) -> datetime:
    if value is None or value == "":
        return datetime.now(UTC)
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


def parse_incoming_message(payload: dict[str, Any]) -> IncomingMessage:
    header = payload.get("header") or {}
    event = payload.get("event") or {}
    message = event.get("message") or {}
    sender = event.get("sender") or {}
    sender_id = sender.get("sender_id") or {}
    mentions = tuple(
        Mention(
            key=str(raw.get("key") or ""),
            name=str(raw.get("name") or ""),
            user_id=str((raw.get("id") or {}).get("user_id") or ""),
            open_id=str((raw.get("id") or {}).get("open_id") or ""),
            union_id=str((raw.get("id") or {}).get("union_id") or ""),
        )
        for raw in (message.get("mentions") or [])
    )
    return IncomingMessage(
        tenant_key=str(header.get("tenant_key") or sender.get("tenant_key") or ""),
        message_id=str(message.get("message_id") or ""),
        root_id=str(message.get("root_id") or ""),
        parent_id=str(message.get("parent_id") or ""),
        chat_id=str(message.get("chat_id") or ""),
        chat_type=str(message.get("chat_type") or ""),
        message_type=str(message.get("message_type") or ""),
        content=str(message.get("content") or ""),
        create_time=parse_milliseconds(message.get("create_time")),
        sender_user_id=str(sender_id.get("user_id") or ""),
        sender_open_id=str(sender_id.get("open_id") or ""),
        sender_name=str(sender.get("name") or ""),
        mentions=mentions,
    )
