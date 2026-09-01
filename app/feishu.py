from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlencode

import httpx

from .config import Settings
from .models import Chat, ChatMembership, OAuthTokens, OAuthUserInfo


class FeishuAPIError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code

    @property
    def authorization_failed(self) -> bool:
        return self.status_code in {401, 403} or self.code in {
            99991663,
            99991668,
            99991679,
            99991681,
        }


@dataclass(slots=True)
class _CachedToken:
    value: str = ""
    expires_at: float = 0.0


class FeishuClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        self.settings = settings
        self._http = http or httpx.AsyncClient(timeout=15.0)
        self._owns_http = http is None
        self._tenant_token = _CachedToken()
        self._token_lock = asyncio.Lock()

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def authorize_url(self, state: str) -> str:
        query = urlencode(
            {
                "app_id": self.settings.feishu_app_id,
                "redirect_uri": self.settings.oauth_redirect_uri,
                "scope": self.settings.feishu_oauth_scopes,
                "state": state,
            }
        )
        return f"{self.settings.feishu_accounts_url}/open-apis/authen/v1/authorize?{query}"

    async def tenant_access_token(self) -> str:
        now = time.time()
        if self._tenant_token.value and self._tenant_token.expires_at > now + 60:
            return self._tenant_token.value
        async with self._token_lock:
            now = time.time()
            if self._tenant_token.value and self._tenant_token.expires_at > now + 60:
                return self._tenant_token.value
            response = await self._http.post(
                f"{self.settings.feishu_base_url}/open-apis/auth/v3/tenant_access_token/internal",
                json={
                    "app_id": self.settings.feishu_app_id,
                    "app_secret": self.settings.feishu_app_secret,
                },
            )
            data = self._decode(response)
            token = str(data.get("tenant_access_token") or data.get("data", {}).get("token") or "")
            if not token:
                raise FeishuAPIError("tenant token response did not include a token")
            expires_in = int(data.get("expire") or data.get("expires_in") or 7200)
            self._tenant_token = _CachedToken(token, time.time() + expires_in)
            return token

    async def exchange_oauth_code(self, code: str) -> OAuthTokens:
        data = await self._oauth_token_request(
            {
                "grant_type": "authorization_code",
                "client_id": self.settings.feishu_app_id,
                "client_secret": self.settings.feishu_app_secret,
                "code": code,
                "redirect_uri": self.settings.oauth_redirect_uri,
            }
        )
        return self._oauth_tokens(data)

    async def refresh_user_token(self, refresh_token: str) -> OAuthTokens:
        data = await self._oauth_token_request(
            {
                "grant_type": "refresh_token",
                "client_id": self.settings.feishu_app_id,
                "client_secret": self.settings.feishu_app_secret,
                "refresh_token": refresh_token,
            }
        )
        return self._oauth_tokens(data)

    async def _oauth_token_request(self, body: dict[str, Any]) -> dict[str, Any]:
        response = await self._http.post(
            f"{self.settings.feishu_base_url}/open-apis/authen/v2/oauth/token",
            json=body,
        )
        data = self._decode(response)
        nested = data.get("data")
        return nested if isinstance(nested, dict) else data

    @staticmethod
    def _oauth_tokens(data: dict[str, Any]) -> OAuthTokens:
        access_token = str(data.get("access_token") or "")
        refresh_token = str(data.get("refresh_token") or "")
        if not access_token or not refresh_token:
            raise FeishuAPIError("OAuth response did not include access and refresh tokens")
        return OAuthTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=int(data.get("expires_in") or 7200),
            refresh_expires_in=int(data.get("refresh_token_expires_in") or 30 * 86400),
        )

    async def get_oauth_user(self, access_token: str) -> OAuthUserInfo:
        data = await self._request(
            "GET", "/open-apis/authen/v1/user_info", access_token=access_token
        )
        return OAuthUserInfo(
            tenant_key=str(data.get("tenant_key") or self.settings.feishu_tenant_key),
            user_id=str(data.get("user_id") or ""),
            open_id=str(data.get("open_id") or ""),
            name=str(data.get("name") or ""),
        )

    async def get_contact_user(
        self, user_identifier: str, id_type: str = "user_id"
    ) -> OAuthUserInfo:
        if id_type not in {"user_id", "open_id", "union_id"}:
            raise ValueError("unsupported contact user ID type")
        data = await self._request(
            "GET",
            f"/open-apis/contact/v3/users/{user_identifier}",
            params={"user_id_type": id_type},
        )
        user = data.get("user") or data
        return OAuthUserInfo(
            tenant_key=self.settings.feishu_tenant_key,
            user_id=str(user.get("user_id") or ""),
            open_id=str(user.get("open_id") or ""),
            name=str(user.get("name") or ""),
        )

    async def list_user_chats(self, access_token: str) -> list[ChatMembership]:
        items = await self._paginate_chats(access_token)
        memberships: list[ChatMembership] = []
        for item in items:
            chat_id = str(item.get("chat_id") or "")
            if not chat_id:
                continue
            chat_tag = str(item.get("chat_tag") or "").lower()
            tenant_key = str(item.get("tenant_key") or "")
            external = bool(item.get("external", False)) or chat_tag == "external"
            if tenant_key and self.settings.feishu_tenant_key:
                external = external or tenant_key != self.settings.feishu_tenant_key
            memberships.append(
                ChatMembership(
                    chat_id=chat_id,
                    name=str(item.get("name") or chat_id),
                    external=external,
                )
            )
        return memberships

    async def list_bot_chat_ids(self) -> set[str]:
        items = await self._paginate_chats(await self.tenant_access_token())
        return {str(item.get("chat_id")) for item in items if item.get("chat_id")}

    async def list_chat_member_user_ids(self, chat_id: str) -> set[str]:
        page_token = ""
        member_ids: set[str] = set()
        while True:
            params: dict[str, Any] = {
                "member_id_type": "user_id",
                "page_size": 100,
            }
            if page_token:
                params["page_token"] = page_token
            data = await self._request(
                "GET",
                f"/open-apis/im/v1/chats/{chat_id}/members",
                params=params,
            )
            member_ids.update(
                str(item.get("member_id"))
                for item in (data.get("items") or [])
                if item.get("member_id")
            )
            if not data.get("has_more"):
                return member_ids
            page_token = str(data.get("page_token") or "")
            if not page_token:
                raise FeishuAPIError("chat member pagination omitted page_token")

    async def _paginate_chats(self, access_token: str) -> list[dict[str, Any]]:
        page_token = ""
        items: list[dict[str, Any]] = []
        while True:
            params: dict[str, Any] = {"page_size": 100, "user_id_type": "user_id"}
            if page_token:
                params["page_token"] = page_token
            data = await self._request(
                "GET", "/open-apis/im/v1/chats", access_token=access_token, params=params
            )
            items.extend(data.get("items") or [])
            if not data.get("has_more"):
                return items
            page_token = str(data.get("page_token") or "")
            if not page_token:
                return items

    async def resolve_chat(self, tenant_key: str, chat_id: str) -> Chat:
        # Classification failures must be retried. Treating an unknown chat as an
        # internal chat could collect content from an external or cross-tenant group.
        data = await self._request(
            "GET",
            f"/open-apis/im/v1/chats/{chat_id}",
            params={"user_id_type": "user_id"},
        )
        chat_data = data.get("chat") or data
        chat_tag = str(chat_data.get("chat_tag") or "").lower()
        external = bool(chat_data.get("external", False)) or chat_tag == "external"
        return Chat(
            tenant_key=tenant_key,
            chat_id=chat_id,
            name=str(chat_data.get("name") or chat_id),
            external=external,
            bot_present=True,
        )

    async def send_activation_message(self, user_id: str, activation_url: str) -> None:
        content = {"text": f"你已被管理员加入“个人 @收件箱”。首次使用请完成授权：{activation_url}"}
        await self._request(
            "POST",
            "/open-apis/im/v1/messages",
            params={"receive_id_type": "user_id"},
            json_body={
                "receive_id": user_id,
                "msg_type": "text",
                "content": json.dumps(content, ensure_ascii=False),
                "uuid": f"mention-inbox-activation-{user_id}-{int(time.time() * 1000)}",
            },
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        access_token: str | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = access_token or await self.tenant_access_token()
        response = await self._http.request(
            method,
            f"{self.settings.feishu_base_url}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            json=json_body,
        )
        data = self._decode(response)
        nested = data.get("data")
        return nested if isinstance(nested, dict) else data

    @staticmethod
    def _decode(response: httpx.Response) -> dict[str, Any]:
        try:
            raw_data = response.json()
        except ValueError as exc:
            raise FeishuAPIError(
                "Feishu returned a non-JSON response", status_code=response.status_code
            ) from exc
        if not isinstance(raw_data, dict):
            raise FeishuAPIError(
                "Feishu returned a non-object JSON response",
                status_code=response.status_code,
            )
        data = cast(dict[str, Any], raw_data)
        code = data.get("code")
        if response.status_code >= 400 or (code not in {None, 0}):
            raise FeishuAPIError(
                str(data.get("msg") or data.get("message") or "Feishu API request failed"),
                code=int(code) if code is not None else None,
                status_code=response.status_code,
            )
        return data
