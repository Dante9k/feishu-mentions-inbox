from __future__ import annotations

import hashlib
import json
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .bitable import BitableClient, BitableError
from .config import Settings
from .database import PersistentRepository, create_repository
from .feishu import FeishuAPIError, FeishuClient
from .models import InboxStatus, User
from .repository import Repository
from .security import (
    OAuthStateSigner,
    SecurityError,
    TokenCipher,
    decrypt_feishu_event,
    require_bearer,
    verify_feishu_signature,
    verify_request_timestamp,
)
from .service import MentionProcessor, StatusService, event_key
from .workers import BackgroundSupervisor

logger = logging.getLogger(__name__)


class EnableUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=128)
    tenant_key: str = Field(default="", max_length=128)
    open_id: str = Field(default="", max_length=128)
    name: str = Field(default="", max_length=256)
    send_activation_message: bool = True


class StatusCallbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: UUID
    record_id: str = Field(min_length=1, max_length=128)
    status: InboxStatus
    note: str = Field(default="", max_length=4000)
    version: int = Field(ge=1)
    changed_at: int = Field(default=0, ge=0)


class SettingsCallbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=128)
    record_id: str = Field(min_length=1, max_length=128)
    include_at_all: bool


class UserManagementCallbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    record_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(default="", max_length=128)
    open_id: str = Field(default="", max_length=128)
    name: str = Field(default="", max_length=256)
    send_activation_message: bool = True


class CoverageExceptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unsupported: bool = True
    reason: str = Field(default="禁止机器人加入", max_length=500)


class AppContainer:
    def __init__(
        self,
        settings: Settings,
        repository: Repository | None = None,
        feishu: FeishuClient | None = None,
        bitable: BitableClient | None = None,
    ):
        self.settings = settings
        cipher_secret = settings.token_encryption_secret
        if not cipher_secret and settings.app_env != "production":
            cipher_secret = "development-only-token-encryption-secret"
        self.repository = repository or create_repository(
            settings.database_url, TokenCipher(cipher_secret)
        )
        self.feishu = feishu or FeishuClient(settings)
        self.bitable = bitable or BitableClient(settings, self.feishu)
        self.processor = MentionProcessor(
            self.repository,
            chat_resolver=self.feishu,
            allowed_tenant_key=settings.feishu_tenant_key,
        )
        self.status_service = StatusService(self.repository)
        state_secret = settings.oauth_state_secret
        if not state_secret and settings.app_env != "production":
            state_secret = "development-only-oauth-state-secret"
        self.state_signer = OAuthStateSigner(state_secret)
        self.supervisor: BackgroundSupervisor | None = None

    async def startup(self) -> None:
        if self.settings.app_env == "production":
            missing = self.settings.validate_production()
            if missing:
                raise RuntimeError("missing production settings: " + ", ".join(missing))
            if not self.settings.public_base_url.startswith("https://"):
                raise RuntimeError("PUBLIC_BASE_URL must use HTTPS in production")
        open_method = getattr(self.repository, "open", None)
        if open_method:
            await open_method()
        if self.settings.run_background_workers and isinstance(
            self.repository, PersistentRepository
        ):
            self.supervisor = BackgroundSupervisor(
                self.settings,
                self.repository,
                self.processor,
                self.feishu,
                self.bitable,
            )
            await self.supervisor.start()

    async def shutdown(self) -> None:
        if self.supervisor:
            await self.supervisor.stop()
        await self.bitable.close()
        await self.feishu.close()
        close_method = getattr(self.repository, "close", None)
        if close_method:
            await close_method()


def create_app(
    settings: Settings | None = None,
    *,
    repository: Repository | None = None,
    feishu: FeishuClient | None = None,
    bitable: BitableClient | None = None,
) -> FastAPI:
    app_settings = settings or Settings.from_env()
    container = AppContainer(app_settings, repository, feishu, bitable)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await container.startup()
        try:
            yield
        finally:
            await container.shutdown()

    expose_api_docs = app_settings.app_env != "production"
    app = FastAPI(
        title="飞书个人 @收件箱",
        version=__version__,
        description="Enterprise multi-user mention inbox for Feishu/Lark.",
        docs_url="/docs" if expose_api_docs else None,
        redoc_url="/redoc" if expose_api_docs else None,
        openapi_url="/openapi.json" if expose_api_docs else None,
        lifespan=lifespan,
    )
    app.state.container = container

    def admin_auth(authorization: str | None = Header(default=None)) -> None:
        try:
            require_bearer(authorization, app_settings.admin_api_token)
        except SecurityError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    def bitable_auth(authorization: str | None = Header(default=None)) -> None:
        try:
            require_bearer(authorization, app_settings.bitable_callback_token)
        except SecurityError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    async def _enable_allowlist_user(
        body: EnableUserRequest,
        bitable_record_id: str = "",
        resend_existing: bool = False,
    ) -> tuple[User, str]:
        tenant_key = body.tenant_key or app_settings.feishu_tenant_key
        if not tenant_key:
            raise HTTPException(status_code=400, detail="tenant_key is required")
        open_id, name = body.open_id, body.name
        if not open_id or not name:
            try:
                info = await container.feishu.get_contact_user(body.user_id)
                open_id = open_id or info.open_id
                name = name or info.name
            except FeishuAPIError as exc:
                raise HTTPException(
                    status_code=502, detail="unable to resolve employee from contacts"
                ) from exc
        if not open_id:
            raise HTTPException(status_code=400, detail="employee open_id is unavailable")
        existing = await container.repository.get_enabled_user(tenant_key, body.user_id)
        try:
            user = await container.repository.enable_user(
                User(
                    tenant_key=tenant_key,
                    user_id=body.user_id,
                    open_id=open_id,
                    name=name,
                ),
                bitable_record_id=bitable_record_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        activation_url = f"{app_settings.public_base_url}/auth/feishu/start"
        if body.send_activation_message and (existing is None or resend_existing):
            await container.feishu.send_activation_message(body.user_id, activation_url)
        return user, activation_url

    async def _disable_allowlist_user(user_id: str, bitable_record_id: str = "") -> User:
        try:
            user = await container.repository.disable_user(
                app_settings.feishu_tenant_key, user_id, bitable_record_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not user:
            raise HTTPException(status_code=404, detail="user not found")
        try:
            await container.bitable.revoke_user_access(user.open_id)
        except BitableError as exc:
            logger.exception("user disabled but Bitable access removal failed")
            raise HTTPException(
                status_code=502,
                detail="collection stopped, but Bitable access removal must be retried",
            ) from exc
        return user

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        health_method = getattr(container.repository, "health", None)
        database_ok = await health_method() if health_method else True
        payload = {"status": "ok" if database_ok else "degraded", "database": database_ok}
        return JSONResponse(payload, status_code=200 if database_ok else 503)

    @app.post("/integrations/feishu/events")
    async def feishu_events(request: Request) -> dict[str, Any]:
        raw_body = await request.body()
        if len(raw_body) > 2 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="event body is too large")
        timestamp = request.headers.get("X-Lark-Request-Timestamp", "")
        nonce = request.headers.get("X-Lark-Request-Nonce", "")
        signature = request.headers.get("X-Lark-Signature", "")
        if app_settings.feishu_encrypt_key and app_settings.feishu_require_signature:
            if not timestamp or not nonce or not signature:
                raise HTTPException(status_code=401, detail="missing Feishu signature headers")
            if not verify_request_timestamp(timestamp):
                raise HTTPException(status_code=401, detail="stale Feishu request timestamp")
            if not verify_feishu_signature(
                timestamp=timestamp,
                nonce=nonce,
                encrypt_key=app_settings.feishu_encrypt_key,
                raw_body=raw_body,
                signature=signature,
            ):
                raise HTTPException(status_code=401, detail="invalid Feishu signature")
        try:
            envelope = json.loads(raw_body)
            payload = (
                decrypt_feishu_event(envelope["encrypt"], app_settings.feishu_encrypt_key)
                if envelope.get("encrypt")
                else envelope
            )
        except (json.JSONDecodeError, SecurityError, KeyError) as exc:
            raise HTTPException(status_code=400, detail="invalid Feishu event") from exc

        token = str(payload.get("token") or (payload.get("header") or {}).get("token") or "")
        if app_settings.feishu_verification_token and not secrets.compare_digest(
            token, app_settings.feishu_verification_token
        ):
            raise HTTPException(status_code=401, detail="invalid verification token")
        if payload.get("challenge"):
            return {"challenge": payload["challenge"]}

        key, event_type = event_key(payload)
        if not key or key.endswith(":"):
            key = f"raw:{hashlib.sha256(raw_body).hexdigest()}"
        if event_type:
            await container.repository.enqueue_event(key, event_type, payload)
        return {"code": 0}

    @app.get("/auth/feishu/start")
    async def oauth_start() -> RedirectResponse:
        state_value = container.state_signer.sign(secrets.token_urlsafe(24))
        return RedirectResponse(container.feishu.authorize_url(state_value), status_code=302)

    @app.get("/auth/feishu/callback")
    async def oauth_callback(
        code: str = Query(min_length=1), state: str = Query(min_length=1)
    ) -> HTMLResponse:
        try:
            container.state_signer.verify(state)
        except SecurityError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        tokens = await container.feishu.exchange_oauth_code(code)
        info = await container.feishu.get_oauth_user(tokens.access_token)
        if not info.user_id or not info.tenant_key:
            raise HTTPException(status_code=400, detail="Feishu user identity is incomplete")
        allowlisted = await container.repository.get_enabled_user(info.tenant_key, info.user_id)
        if allowlisted is None:
            raise HTTPException(
                status_code=403, detail="user is not on the administrator allowlist"
            )
        memberships = await container.feishu.list_user_chats(tokens.access_token)
        try:
            await container.bitable.grant_user_access(info.open_id)
        except BitableError as exc:
            logger.exception("unable to grant Bitable access during activation")
            raise HTTPException(
                status_code=502, detail="unable to grant personal inbox access"
            ) from exc
        user = await container.repository.activate_user(info, tokens)
        if user is None:
            await container.bitable.revoke_user_access(info.open_id)
            raise HTTPException(
                status_code=403, detail="user is not on the administrator allowlist"
            )
        await container.repository.replace_user_chats(user, memberships)
        return HTMLResponse(
            "<h1>授权成功</h1><p>个人 @收件箱已启用，可以关闭此页面。</p>",
            status_code=200,
        )

    @app.post("/integrations/bitable/status", dependencies=[Depends(bitable_auth)])
    async def bitable_status(body: StatusCallbackRequest) -> dict[str, Any]:
        mapping_method = getattr(container.repository, "get_mapping", None)
        if mapping_method:
            mapping = await mapping_method("inbox", body.item_id, "inbox")
            if not mapping or mapping["record_id"] != body.record_id:
                raise HTTPException(status_code=403, detail="record does not match inbox item")
        item = await container.status_service.update_from_bitable(
            item_id=body.item_id,
            status=body.status,
            note=body.note,
            expected_version=body.version,
            changed_at=(
                datetime.fromtimestamp(body.changed_at / 1000, tz=UTC) if body.changed_at else None
            ),
        )
        if item is None:
            raise HTTPException(status_code=409, detail="item version is stale or item is missing")
        return {"item_id": str(item.id), "version": item.version, "status": item.status.value}

    @app.post("/integrations/bitable/settings", dependencies=[Depends(bitable_auth)])
    async def bitable_settings(body: SettingsCallbackRequest) -> dict[str, Any]:
        user = await container.repository.get_enabled_user(
            app_settings.feishu_tenant_key, body.user_id
        )
        if not user:
            raise HTTPException(status_code=404, detail="enabled user not found")
        mapping_method = getattr(container.repository, "get_mapping", None)
        if mapping_method:
            mapping = await mapping_method("user", user.id, "settings")
            if not mapping or mapping["record_id"] != body.record_id:
                raise HTTPException(status_code=403, detail="record does not match user settings")
        updated = await container.repository.update_user_setting(
            app_settings.feishu_tenant_key, body.user_id, body.include_at_all
        )
        if not updated:
            raise HTTPException(status_code=404, detail="enabled user not found")
        return {"user_id": body.user_id, "include_at_all": updated.include_at_all}

    @app.post("/integrations/bitable/users", dependencies=[Depends(bitable_auth)])
    async def bitable_users(body: UserManagementCallbackRequest) -> dict[str, Any]:
        user_id = body.user_id
        open_id = body.open_id
        name = body.name
        if not user_id:
            if not open_id:
                raise HTTPException(status_code=400, detail="user_id or open_id is required")
            info = await container.feishu.get_contact_user(open_id, "open_id")
            user_id = info.user_id
            name = name or info.name
        if not user_id:
            raise HTTPException(status_code=400, detail="unable to resolve stable user_id")
        if body.enabled:
            user, activation_url = await _enable_allowlist_user(
                EnableUserRequest(
                    user_id=user_id,
                    open_id=open_id,
                    name=name,
                    send_activation_message=body.send_activation_message,
                ),
                body.record_id,
            )
            return _public_user(user, activation_url)
        return _public_user(await _disable_allowlist_user(user_id, body.record_id))

    @app.post("/admin/users", dependencies=[Depends(admin_auth)])
    async def enable_user(body: EnableUserRequest) -> dict[str, Any]:
        user, activation_url = await _enable_allowlist_user(body, resend_existing=True)
        return _public_user(user, activation_url)

    @app.delete("/admin/users/{user_id}", dependencies=[Depends(admin_auth)])
    async def disable_user(user_id: str) -> dict[str, Any]:
        return _public_user(await _disable_allowlist_user(user_id))

    @app.get("/admin/users", dependencies=[Depends(admin_auth)])
    async def list_users() -> list[dict[str, Any]]:
        return [_public_user(user) for user in await container.repository.list_users()]

    @app.get("/admin/coverage", dependencies=[Depends(admin_auth)])
    async def coverage() -> list[dict[str, Any]]:
        method = getattr(container.repository, "coverage_summary", None)
        return await method() if method else []

    @app.post("/admin/coverage/run", dependencies=[Depends(admin_auth)])
    async def run_coverage() -> dict[str, Any]:
        if not container.supervisor:
            raise HTTPException(status_code=503, detail="background workers are disabled")
        await container.supervisor.run_coverage_check()
        return {"status": "completed"}

    @app.post(
        "/admin/chats/{chat_id}/coverage-exception",
        dependencies=[Depends(admin_auth)],
    )
    async def set_coverage_exception(
        chat_id: str, body: CoverageExceptionRequest
    ) -> dict[str, Any]:
        chat = await container.repository.set_chat_unsupported(
            app_settings.feishu_tenant_key,
            chat_id,
            body.unsupported,
            body.reason,
        )
        if not chat:
            raise HTTPException(status_code=404, detail="chat not found")
        return {
            "chat_id": chat.chat_id,
            "coverage_status": chat.coverage_status.value,
            "reason": chat.unsupported_reason,
        }

    return app


def _public_user(user: User, activation_url: str = "") -> dict[str, Any]:
    result = {
        "id": str(user.id),
        "tenant_key": user.tenant_key,
        "user_id": user.user_id,
        "open_id": user.open_id,
        "name": user.name,
        "enabled": user.enabled,
        "authorized": user.authorized,
        "include_at_all": user.include_at_all,
        "departed": user.departed,
        "last_coverage_check_at": (
            user.last_coverage_check_at.isoformat() if user.last_coverage_check_at else None
        ),
    }
    if activation_url:
        result["activation_url"] = activation_url
    return result
