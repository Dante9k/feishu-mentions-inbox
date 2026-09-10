from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.feishu import FeishuAPIError, FeishuClient


def _settings() -> Settings:
    return Settings(
        feishu_base_url="https://open.feishu.test",
        feishu_app_id="app-id",
        feishu_app_secret="app-secret",
        feishu_tenant_key="tenant-a",
    )


@pytest.mark.asyncio
async def test_chat_resolution_failure_is_not_downgraded_to_internal_chat() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
            )
        return httpx.Response(503, json={"code": 500001, "msg": "temporarily unavailable"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = FeishuClient(_settings(), http)
        with pytest.raises(FeishuAPIError):
            await client.resolve_chat("tenant-a", "oc_unknown")


@pytest.mark.asyncio
async def test_external_chat_is_classified_before_collection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
            )
        return httpx.Response(
            200,
            json={"code": 0, "data": {"chat": {"name": "External", "chat_tag": "external"}}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = FeishuClient(_settings(), http)
        chat = await client.resolve_chat("tenant-a", "oc_external")

    assert chat.external
    assert chat.bot_present


@pytest.mark.asyncio
async def test_missing_chat_classification_never_defaults_to_internal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
            )
        return httpx.Response(200, json={"code": 0, "data": {"chat": {"name": "Unknown"}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = FeishuClient(_settings(), http)
        with pytest.raises(FeishuAPIError, match="external classification"):
            await client.resolve_chat("tenant-a", "oc_unknown")


@pytest.mark.asyncio
async def test_chat_list_quarantines_missing_external_flag() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/open-apis/im/v1/chats")
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"items": [{"chat_id": "oc_unknown", "name": "Unknown"}]},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = FeishuClient(_settings(), http)
        memberships = await client.list_user_chats("user-access-token")

    assert len(memberships) == 1
    assert memberships[0].external
