"""Resumable Base structure provisioning; never grants employee access."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from dotenv import dotenv_values

from .config import Settings
from .feishu import FeishuAPIError, FeishuClient
from .setup import update_config


def field(name: str, kind: int = 1, options: list[str] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"field_name": name, "type": kind}
    if options:
        result["property"] = {"options": [{"name": value} for value in options]}
    elif kind == 11:
        result["property"] = {"multiple": False}
    elif kind == 5:
        result["property"] = {"date_formatter": "yyyy/MM/dd HH:mm"}
    return result


TABLES = {
    "inbox": (
        "个人 @收件箱",
        [
            field("内部待办ID"),
            field("目标用户", 11),
            field("群名"),
            field("发送人"),
            field("提及类型", 3, ["直接@我", "@所有人"]),
            field("正文"),
            field("消息类型"),
            field("发送时间", 5),
            field("处理状态", 3, ["待处理", "处理中", "已处理", "忽略"]),
            field("处理备注"),
            field("处理时间", 5),
            field("源状态", 3, ["有效", "已撤回"]),
            field("定位信息"),
            field("源消息ID"),
            field("目标用户ID"),
            field("版本", 2),
            field("表格修改时间", 1002),
        ],
    ),
    "settings": (
        "个人设置",
        [
            field("内部用户ID"),
            field("用户", 11),
            field("包含@所有人", 7),
            field("授权状态", 3, ["待授权", "已授权"]),
            field("群覆盖率", 2),
            field("已覆盖群数", 2),
            field("目标群数", 2),
            field("最后检查时间", 5),
        ],
    ),
    "coverage": (
        "群覆盖管理",
        [
            field("群ID"),
            field("用户", 11),
            field("用户姓名"),
            field("群名"),
            field("是否内部群", 7),
            field("机器人在群", 7),
            field("覆盖状态", 3, ["已覆盖", "待加机器人", "无法覆盖", "授权失效"]),
            field("处理建议"),
            field("最后检查时间", 5),
            field("内部用户ID"),
        ],
    ),
    "users": (
        "启用用户管理",
        [
            field("内部用户ID"),
            field("用户", 11),
            field("姓名"),
            field("启用", 7),
            field("启用状态", 3, ["已启用", "已停用"]),
            field("授权状态", 3, ["待授权", "已授权"]),
            field("包含@所有人", 7),
        ],
    ),
}


class Provisioner:
    def __init__(self, path: Path, settings: Settings, client: FeishuClient):
        self.path = path
        self.settings = settings
        self.client = client
        self.stage = "credentials"

    async def list_items(self, path: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        params: dict[str, Any] = {"page_size": 100}
        while True:
            data = await self.client._request("GET", path, params=params)
            result.extend(data.get("items") or [])
            if not data.get("has_more"):
                return result
            token = data.get("page_token")
            if not token:
                raise ValueError("pagination_missing_token")
            params["page_token"] = token

    async def run(self) -> dict[str, Any]:
        if self.settings.feishu_base_url != "https://open.feishu.cn":
            raise ValueError("untrusted_api_origin")
        await self.client.tenant_access_token()
        self.stage = "tenant"
        if not self.settings.feishu_tenant_key:
            try:
                data = await self.client._request("GET", "/open-apis/tenant/v2/tenant/query")
                key = str((data.get("tenant") or {}).get("tenant_key") or "")
                if key:
                    update_config(self.path, {"FEISHU_TENANT_KEY": key})
            except FeishuAPIError:
                # User OAuth can supply tenant_key later; never guess it.
                print(
                    json.dumps(
                        {"stage": "tenant", "result": "requires_user_oauth_or_tenant_permission"}
                    ),
                    flush=True,
                )
        self.stage = "base"
        config = dotenv_values(self.path, interpolate=False)
        app_token = config.get("BITABLE_APP_TOKEN")
        if not app_token:
            if config.get("SETUP_BASE_CREATE_PENDING") == "true":
                raise ValueError("previous_base_create_uncertain_do_not_duplicate")
            update_config(self.path, {"SETUP_BASE_CREATE_PENDING": "true"})
            try:
                data = await self.client._request(
                    "POST",
                    "/open-apis/bitable/v1/apps",
                    json_body={
                        "name": "个人 @收件箱 · 本机试点",
                        "time_zone": "Asia/Shanghai",
                    },
                )
            except FeishuAPIError:
                update_config(self.path, {"SETUP_BASE_CREATE_PENDING": "false"})
                raise
            app = data.get("app") or {}
            app_token = str(app.get("app_token") or "")
            if not app_token:
                raise ValueError("base_response_missing_identity")
            update_config(
                self.path,
                {
                    "BITABLE_APP_TOKEN": app_token,
                    "SETUP_BASE_CREATE_PENDING": "false",
                    "BITABLE_URL": str(app.get("url") or ""),
                },
            )
        base = "/open-apis/bitable/v1/apps/" + quote(app_token, safe="")
        existing = await self.list_items(base + "/tables")
        for key, (name, fields) in TABLES.items():
            self.stage = f"table_{key}"
            matches = [item for item in existing if item.get("name") == name]
            configured_id = config.get(f"BITABLE_{key.upper()}_TABLE_ID")
            if len(matches) > 1:
                raise ValueError("duplicate_table_names_require_resolution")
            if configured_id:
                table_id = configured_id
            elif matches:
                table_id = str(matches[0]["table_id"])
            else:
                created = await self.client._request(
                    "POST",
                    base + "/tables",
                    json_body={
                        "table": {
                            "name": name,
                            "default_view_name": "全部记录",
                            "fields": fields,
                        }
                    },
                )
                table_id = str(created.get("table_id") or "")
                if not table_id:
                    raise ValueError("table_response_missing_identity")
            update_config(self.path, {f"BITABLE_{key.upper()}_TABLE_ID": table_id})
            remote_fields = await self.list_items(
                base + "/tables/" + quote(table_id, safe="") + "/fields"
            )
            by_name = {item["field_name"]: item for item in remote_fields}
            for expected in fields:
                actual = by_name.get(expected["field_name"])
                if not actual or actual.get("type") != expected["type"]:
                    raise ValueError("schema_mismatch_no_automatic_overwrite")
            print(json.dumps({"stage": self.stage, "schema_verified": True}), flush=True)
        # Do not pretend field creation proves current-user row-level isolation.
        return {
            "structure_ready": True,
            "employee_access_granted": False,
            "next": "configure_and_verify_advanced_permissions_before_activation",
        }


async def run(path: Path, settings: Settings) -> dict[str, Any]:
    client = FeishuClient(settings)
    provisioner = Provisioner(path, settings, client)
    try:
        return await provisioner.run()
    except FeishuAPIError as exc:
        return {
            "structure_ready": False,
            "stage": provisioner.stage,
            "api_error_code": exc.code,
            "http_status": exc.status_code,
        }
    except httpx.HTTPError:
        return {"structure_ready": False, "stage": provisioner.stage, "error": "network_error"}
    except ValueError as exc:
        # Only static application messages may appear here, not remote bodies.
        return {"structure_ready": False, "stage": provisioner.stage, "error": str(exc)}
    finally:
        await client.close()


def main() -> None:
    path = Path.cwd() / ".env.local"
    if not path.is_file():
        raise SystemExit("Run python -m app.local --init first")
    os.environ.update(
        {k: v for k, v in dotenv_values(path, interpolate=False).items() if v is not None}
    )
    print(json.dumps(asyncio.run(run(path, Settings.from_env())), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
