from __future__ import annotations

import json

import httpx
import pytest

from app.bitable import BitableClient
from app.config import Settings


class TokenProvider:
    async def tenant_access_token(self) -> str:
        return "tenant-token"


@pytest.mark.asyncio
async def test_grant_and_revoke_user_access_are_idempotent() -> None:
    calls: list[httpx.Request] = []
    role_members: set[str] = set()
    document_members: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        body = json.loads(request.content or b"{}")
        if path.endswith("/roles/rol_employee/members"):
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {"items": [{"member_id": item} for item in role_members]},
                    },
                )
            role_members.add(body["member_id"])
            return httpx.Response(200, json={"code": 0, "data": {}})
        if "/roles/rol_employee/members/" in path and request.method == "DELETE":
            role_members.discard(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"code": 0, "data": {}})
        if path.endswith("/permissions/app_token/members"):
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {"items": [{"member_id": item} for item in document_members]},
                    },
                )
            document_members.add(body["member_id"])
            return httpx.Response(200, json={"code": 0, "data": {}})
        if "/permissions/app_token/members/" in path and request.method == "DELETE":
            document_members.discard(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"code": 0, "data": {}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    settings = Settings(
        feishu_base_url="https://open.feishu.test",
        bitable_app_token="app_token",
        bitable_user_role_id="rol_employee",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BitableClient(settings, TokenProvider(), http)  # type: ignore[arg-type]
        await client.grant_user_access("ou_1")
        await client.grant_user_access("ou_1")
        await client.revoke_user_access("ou_1")
        await client.revoke_user_access("ou_1")

    assert role_members == set()
    assert document_members == set()
    assert sum(request.method == "POST" for request in calls) == 2
    assert sum(request.method == "DELETE" for request in calls) == 2


@pytest.mark.asyncio
async def test_list_records_follows_pagination() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page_token = request.url.params.get("page_token")
        if not page_token:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [{"record_id": "rec_1", "fields": {}}],
                        "has_more": True,
                        "page_token": "next",
                    },
                },
            )
        assert page_token == "next"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "items": [{"record_id": "rec_2", "fields": {}}],
                    "has_more": False,
                },
            },
        )

    settings = Settings(
        feishu_base_url="https://open.feishu.test",
        bitable_app_token="app_token",
        bitable_inbox_table_id="tbl_inbox",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BitableClient(settings, TokenProvider(), http)  # type: ignore[arg-type]
        records = await client.list_records("inbox")

    assert [record["record_id"] for record in records] == ["rec_1", "rec_2"]
