from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from .models import Mention

PLACEHOLDERS = {
    "image": "[图片]",
    "file": "[文件]",
    "audio": "[音频]",
    "media": "[视频]",
    "sticker": "[表情]",
    "interactive": "[消息卡片]",
    "share_chat": "[群名片]",
    "share_user": "[个人名片]",
}


def _walk_text(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        text = value.strip()
        if text:
            yield text
        return
    if isinstance(value, list):
        for item in value:
            yield from _walk_text(item)
        return
    if not isinstance(value, dict):
        return
    preferred_keys = ("title", "text", "content", "name", "file_name", "href")
    seen: set[int] = set()
    for key in preferred_keys:
        if key in value:
            seen.add(id(value[key]))
            yield from _walk_text(value[key])
    structural_keys = {"tag", "type", "style"}
    for key, child in value.items():
        if key not in structural_keys and id(child) not in seen:
            yield from _walk_text(child)


def normalize_message_content(
    message_type: str,
    raw_content: str,
    mentions: tuple[Mention, ...] = (),
    *,
    max_length: int = 4000,
) -> str:
    try:
        parsed = json.loads(raw_content) if raw_content else {}
    except json.JSONDecodeError:
        parsed = raw_content

    if message_type == "text" and isinstance(parsed, dict):
        text = str(parsed.get("text") or "")
    else:
        parts = list(dict.fromkeys(_walk_text(parsed)))
        text = "\n".join(parts)

    for mention in mentions:
        if mention.key:
            replacement = "@所有人" if mention.is_all else f"@{mention.name or '用户'}"
            text = text.replace(mention.key, replacement)

    text = " ".join(text.split())
    if not text:
        text = PLACEHOLDERS.get(message_type, f"[{message_type or '未知消息'}]")
    if len(text) > max_length:
        text = f"{text[: max_length - 1]}…"
    return text
