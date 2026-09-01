from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import Chat, CoverageStatus, InboxItem, SourceMessage, User


def _millis(value: datetime | None) -> int | None:
    return int(value.timestamp() * 1000) if value else None


def inbox_fields(item: InboxItem, source: SourceMessage, user: User) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "目标用户": [{"id": user.open_id}],
        "群名": source.chat_name,
        "发送人": source.sender_name or source.sender_id,
        "提及类型": item.mention_type.value,
        "正文": source.content,
        "消息类型": source.message_type,
        "发送时间": _millis(source.sent_at),
        "处理状态": item.status.value,
        "处理备注": item.note,
        "源状态": source.source_state.value,
        "定位信息": source.locator,
        "内部待办ID": str(item.id),
        "源消息ID": source.message_id,
        "目标用户ID": user.user_id,
        "版本": item.version,
    }
    fields["处理时间"] = _millis(item.handled_at)
    return fields


def settings_fields(user: User, covered: int, total: int) -> dict[str, Any]:
    coverage = 1.0 if total == 0 else covered / total
    return {
        "用户": [{"id": user.open_id}] if user.open_id else [],
        "包含@所有人": user.include_at_all,
        "授权状态": "已授权" if user.authorized else "待授权",
        "群覆盖率": coverage,
        "已覆盖群数": covered,
        "目标群数": total,
        "最后检查时间": _millis(user.last_coverage_check_at),
        "内部用户ID": user.user_id,
    }


def user_admin_fields(user: User) -> dict[str, Any]:
    return {
        "用户": [{"id": user.open_id}] if user.open_id else [],
        "姓名": user.name,
        "内部用户ID": user.user_id,
        "启用": user.enabled,
        "启用状态": "已启用" if user.enabled else "已停用",
        "授权状态": "已授权" if user.authorized else "待授权",
        "包含@所有人": user.include_at_all,
    }


def coverage_fields(
    user: User,
    chat: Chat,
    status: CoverageStatus,
    last_seen_at: datetime,
) -> dict[str, Any]:
    guidance = {
        CoverageStatus.COVERED: "无需处理",
        CoverageStatus.BOT_MISSING: "请群主将应用机器人加入该群",
        CoverageStatus.UNSUPPORTED: chat.unsupported_reason
        or "外部群、已解散群或禁止机器人群不纳入覆盖",
        CoverageStatus.AUTH_EXPIRED: "请用户重新完成飞书授权",
    }[status]
    return {
        "用户": [{"id": user.open_id}] if user.open_id else [],
        "用户姓名": user.name,
        "群名": chat.name,
        "群ID": chat.chat_id,
        "是否内部群": not chat.external,
        "机器人在群": chat.bot_present,
        "覆盖状态": status.value,
        "处理建议": guidance,
        "最后检查时间": _millis(last_seen_at),
        "内部用户ID": user.user_id,
    }
