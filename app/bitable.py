from __future__ import annotations

from typing import Any, cast
from urllib.parse import quote

import httpx

from .config import Settings
from .feishu import FeishuClient


class BitableError(RuntimeError):
    pass


class BitableClient:
    def __init__(
        self,
        settings: Settings,
        feishu: FeishuClient,
        http: httpx.AsyncClient | None = None,
    ):
        self.settings = settings
        self._feishu = feishu
        self._http = http or httpx.AsyncClient(timeout=20.0)
        self._owns_http = http is None

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def table_id(self, key: str) -> str:
        mapping = {
            "inbox": self.settings.bitable_inbox_table_id,
            "settings": self.settings.bitable_settings_table_id,
            "coverage": self.settings.bitable_coverage_table_id,
            "users": self.settings.bitable_users_table_id,
        }
        table_id = mapping.get(key, "")
        if not table_id:
            raise BitableError(f"Bitable table is not configured: {key}")
        return table_id

    async def batch_create(self, table_key: str, fields: list[dict[str, Any]]) -> list[str]:
        if not fields:
            return []
        records = await self._request(
            "POST",
            table_key,
            "/records/batch_create",
            {"records": [{"fields": item} for item in fields]},
        )
        result = [str(record.get("record_id") or "") for record in records]
        if len(result) != len(fields) or any(not record_id for record_id in result):
            raise BitableError("Bitable batch create returned incomplete record IDs")
        return result

    async def batch_update(
        self, table_key: str, records: list[tuple[str, dict[str, Any]]]
    ) -> list[str]:
        if not records:
            return []
        result = await self._request(
            "POST",
            table_key,
            "/records/batch_update",
            {
                "records": [
                    {"record_id": record_id, "fields": fields} for record_id, fields in records
                ]
            },
        )
        ids = [str(record.get("record_id") or "") for record in result]
        if len(ids) != len(records):
            raise BitableError("Bitable batch update returned an incomplete response")
        return ids

    async def list_records(self, table_key: str) -> list[dict[str, Any]]:
        table_id = self.table_id(table_key)
        path = (
            f"/open-apis/bitable/v1/apps/"
            f"{quote(self.settings.bitable_app_token, safe='')}/tables/"
            f"{quote(table_id, safe='')}/records"
        )
        records: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            payload = await self._json_request("GET", path, params=params)
            data = payload.get("data") or {}
            records.extend(data.get("items") or data.get("records") or [])
            if not data.get("has_more"):
                return records
            page_token = str(data.get("page_token") or "")
            if not page_token:
                raise BitableError("Bitable record pagination omitted page_token")

    async def grant_user_access(self, open_id: str) -> None:
        """Grant document visibility and the restricted employee role idempotently."""
        if not open_id:
            raise BitableError("open_id is required to grant Bitable access")
        if self.settings.bitable_grant_document_access:
            document_members = await self._list_items(
                f"/open-apis/drive/v1/permissions/"
                f"{quote(self.settings.bitable_app_token, safe='')}/members",
                {"type": "bitable", "page_size": 100},
            )
            if not _contains_member(document_members, open_id):
                await self._json_request(
                    "POST",
                    f"/open-apis/drive/v1/permissions/"
                    f"{quote(self.settings.bitable_app_token, safe='')}/members",
                    params={"type": "bitable", "need_notification": "false"},
                    body={
                        "member_type": "openid",
                        "member_id": open_id,
                        "perm": "view",
                    },
                )

        role_members = await self._list_items(
            self._role_members_path(),
            {"member_id_type": "open_id", "page_size": 100},
        )
        if not _contains_member(role_members, open_id):
            await self._json_request(
                "POST",
                self._role_members_path(),
                params={"member_id_type": "open_id"},
                body={"member_id": open_id},
            )

    async def revoke_user_access(self, open_id: str) -> None:
        """Remove the restricted role and document collaborator idempotently."""
        if not open_id:
            return
        role_members = await self._list_items(
            self._role_members_path(),
            {"member_id_type": "open_id", "page_size": 100},
        )
        if _contains_member(role_members, open_id):
            await self._json_request(
                "DELETE",
                f"{self._role_members_path()}/{quote(open_id, safe='')}",
                params={"member_id_type": "open_id"},
            )

        if self.settings.bitable_grant_document_access:
            document_path = (
                f"/open-apis/drive/v1/permissions/"
                f"{quote(self.settings.bitable_app_token, safe='')}/members"
            )
            document_members = await self._list_items(
                document_path,
                {"type": "bitable", "page_size": 100},
            )
            if _contains_member(document_members, open_id):
                await self._json_request(
                    "DELETE",
                    f"{document_path}/{quote(open_id, safe='')}",
                    params={"type": "bitable", "member_type": "openid"},
                )

    def _role_members_path(self) -> str:
        if not self.settings.bitable_user_role_id:
            raise BitableError("BITABLE_USER_ROLE_ID is not configured")
        return (
            f"/open-apis/bitable/v1/apps/"
            f"{quote(self.settings.bitable_app_token, safe='')}/roles/"
            f"{quote(self.settings.bitable_user_role_id, safe='')}/members"
        )

    async def _list_items(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token = ""
        while True:
            page_params = dict(params)
            if page_token:
                page_params["page_token"] = page_token
            payload = await self._json_request("GET", path, params=page_params)
            data = payload.get("data") or {}
            items.extend(data.get("items") or data.get("members") or [])
            if not data.get("has_more"):
                return items
            page_token = str(data.get("page_token") or "")
            if not page_token:
                raise BitableError("Bitable pagination response omitted page_token")

    async def _json_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = await self._feishu.tenant_access_token()
        response = await self._http.request(
            method,
            f"{self.settings.feishu_base_url}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            json=body,
        )
        try:
            raw_payload = response.json()
        except ValueError as exc:
            raise BitableError("Bitable returned a non-JSON response") from exc
        if not isinstance(raw_payload, dict):
            raise BitableError("Bitable returned a non-object JSON response")
        payload = cast(dict[str, Any], raw_payload)
        if response.status_code >= 400 or payload.get("code") not in {None, 0}:
            message = str(payload.get("msg") or "Bitable API request failed")
            raise BitableError(f"{message} (code={payload.get('code')})")
        return payload

    async def _request(
        self,
        method: str,
        table_key: str,
        suffix: str,
        body: dict[str, Any],
    ) -> list[dict[str, Any]]:
        table_id = self.table_id(table_key)
        path = (
            f"/open-apis/bitable/v1/apps/"
            f"{self.settings.bitable_app_token}/tables/{table_id}{suffix}"
        )
        payload = await self._json_request(method, path, body=body)
        data = payload.get("data") or {}
        return data.get("records") or []


def _contains_member(items: list[dict[str, Any]], open_id: str) -> bool:
    return any(str(item.get("member_id") or "") == open_id for item in items)
