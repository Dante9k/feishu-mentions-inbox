from __future__ import annotations

import json

from app.models import Mention
from app.normalizer import normalize_message_content


def test_text_message_replaces_mentions_and_normalizes_whitespace() -> None:
    mentions = (Mention(key="@_user_1", name="测试用户", user_id="user-1"),)
    content = json.dumps({"text": "@_user_1   请\n确认"}, ensure_ascii=False)

    result = normalize_message_content("text", content, mentions)

    assert result == "@测试用户 请 确认"


def test_rich_text_extracts_readable_fields_without_duplicates() -> None:
    content = json.dumps(
        {
            "title": "发布提醒",
            "content": [[{"tag": "text", "text": "请检查部署"}]],
            "metadata": {"name": "生产环境"},
        },
        ensure_ascii=False,
    )

    result = normalize_message_content("post", content)

    assert result == "发布提醒 请检查部署 生产环境"


def test_binary_message_uses_placeholder_and_long_text_is_truncated() -> None:
    assert normalize_message_content("image", "{}") == "[图片]"
    assert normalize_message_content("text", json.dumps({"text": "abcdef"}), max_length=4) == "abc…"


def test_malformed_json_is_treated_as_plain_text() -> None:
    assert normalize_message_content("text", "not-json") == "not-json"
