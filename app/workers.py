from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from .bitable import BitableClient
from .config import Settings
from .database import PersistentRepository
from .feishu import FeishuAPIError, FeishuClient
from .models import InboxStatus
from .projections import coverage_fields, inbox_fields, settings_fields, user_admin_fields
from .service import MentionProcessor

logger = logging.getLogger(__name__)


class BackgroundSupervisor:
    def __init__(
        self,
        settings: Settings,
        repository: PersistentRepository,
        processor: MentionProcessor,
        feishu: FeishuClient,
        bitable: BitableClient,
    ):
        self.settings = settings
        self.repository = repository
        self.processor = processor
        self.feishu = feishu
        self.bitable = bitable
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._event_loop(), name="event-worker"),
            asyncio.create_task(self._outbox_loop(), name="bitable-outbox-worker"),
            asyncio.create_task(self._coverage_loop(), name="coverage-worker"),
            asyncio.create_task(self._reconciliation_loop(), name="reconciliation-worker"),
            asyncio.create_task(self._retention_loop(), name="retention-worker"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _event_loop(self) -> None:
        while True:
            try:
                jobs = await self.repository.claim_events()
                if not jobs:
                    await asyncio.sleep(self.settings.worker_poll_seconds)
                    continue
                for job in jobs:
                    try:
                        await self._dispatch_event(job.event_type, job.payload)
                        await self.repository.finish_event(job.id)
                    except Exception as exc:
                        logger.exception("event processing failed", extra={"job_id": str(job.id)})
                        await self.repository.retry_event(job.id, str(exc), job.attempts)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("event worker iteration failed")
                await asyncio.sleep(self.settings.worker_poll_seconds)

    async def _dispatch_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if event_type == "im.message.receive_v1":
            await self.processor.process_receive_event(payload)
            return
        if event_type == "im.message.recalled_v1":
            await self.processor.process_recall_event(payload)
            return
        header = payload.get("header") or {}
        event = payload.get("event") or {}
        tenant_key = str(header.get("tenant_key") or "")
        chat_id = str(event.get("chat_id") or "")
        if not tenant_key or not chat_id:
            return
        if event_type == "im.chat.member.bot.added_v1":
            await self.repository.set_bot_membership(tenant_key, chat_id, True)
        elif event_type == "im.chat.member.bot.deleted_v1":
            await self.repository.set_bot_membership(tenant_key, chat_id, False)
        elif event_type == "im.chat.disbanded_v1":
            await self.repository.disband_chat(tenant_key, chat_id)

    async def _outbox_loop(self) -> None:
        while True:
            try:
                jobs = await self.repository.claim_outbox()
                if not jobs:
                    await asyncio.sleep(self.settings.worker_poll_seconds)
                    continue
                await self._flush_outbox(jobs)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("outbox worker iteration failed")
                await asyncio.sleep(self.settings.worker_poll_seconds)

    async def _flush_outbox(self, jobs: list[dict[str, Any]]) -> None:
        jobs, superseded_ids = _coalesce_outbox_jobs(jobs)
        await self.repository.finish_superseded_outbox(superseded_ids)
        prepared: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None, int]] = []
        for job in jobs:
            try:
                fields, version = await self._project(job)
                mapping = await self.repository.get_mapping(
                    job["entity_type"], UUID(str(job["entity_id"])), job["table_key"]
                )
                prepared.append((job, fields, mapping, version))
            except Exception as exc:
                await self.repository.retry_outbox(
                    UUID(str(job["id"])), str(exc), int(job["attempts"])
                )

        for table_key in {job["table_key"] for job, _, _, _ in prepared}:
            group = [item for item in prepared if item[0]["table_key"] == table_key]
            creates = [item for item in group if item[2] is None]
            updates = [item for item in group if item[2] is not None]
            if creates:
                try:
                    record_ids = await self.bitable.batch_create(
                        table_key, [fields for _, fields, _, _ in creates]
                    )
                    for (job, _, _, version), record_id in zip(creates, record_ids, strict=True):
                        await self._finish_outbox_job(job, record_id, version)
                except Exception as exc:
                    for job, _, _, _ in creates:
                        await self.repository.retry_outbox(
                            UUID(str(job["id"])), str(exc), int(job["attempts"])
                        )
            if updates:
                try:
                    records = [
                        (str(mapping["record_id"]), fields)
                        for _, fields, mapping, _ in updates
                        if mapping
                    ]
                    await self.bitable.batch_update(table_key, records)
                    for job, _, mapping, version in updates:
                        if mapping is None:
                            raise RuntimeError("existing outbox update has no record mapping")
                        await self._finish_outbox_job(job, str(mapping["record_id"]), version)
                except Exception as exc:
                    for job, _, _, _ in updates:
                        await self.repository.retry_outbox(
                            UUID(str(job["id"])), str(exc), int(job["attempts"])
                        )

    async def _project(self, job: dict[str, Any]) -> tuple[dict[str, Any], int]:
        entity_id = UUID(str(job["entity_id"]))
        entity_type = job["entity_type"]
        table_key = job["table_key"]
        if entity_type == "inbox":
            inbox_context = await self.repository.get_inbox_context(entity_id)
            if not inbox_context:
                raise RuntimeError("inbox item no longer exists")
            item, source, inbox_user = inbox_context
            return inbox_fields(item, source, inbox_user), item.version
        if entity_type == "user":
            projection_user = await self.repository.get_user_by_pk(entity_id)
            if not projection_user:
                raise RuntimeError("user no longer exists")
            if table_key == "settings":
                covered, total = await self.repository.count_user_coverage(projection_user.id)
                return settings_fields(projection_user, covered, total), int(
                    datetime.now(UTC).timestamp()
                )
            return user_admin_fields(projection_user), int(datetime.now(UTC).timestamp())
        if entity_type == "coverage":
            coverage_context = await self.repository.get_coverage_context(entity_id)
            if not coverage_context:
                raise RuntimeError("coverage record no longer exists")
            coverage_user, chat, coverage_status, last_seen_at = coverage_context
            return coverage_fields(coverage_user, chat, coverage_status, last_seen_at), int(
                last_seen_at.timestamp()
            )
        raise RuntimeError(f"unsupported outbox entity type: {entity_type}")

    async def _finish_outbox_job(self, job: dict[str, Any], record_id: str, version: int) -> None:
        await self.repository.finish_outbox(
            UUID(str(job["id"])),
            entity_type=job["entity_type"],
            entity_id=UUID(str(job["entity_id"])),
            table_key=job["table_key"],
            record_id=record_id,
            synced_version=version,
        )

    async def _coverage_loop(self) -> None:
        while True:
            try:
                await self.run_coverage_check()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("coverage check failed")
            await asyncio.sleep(self.settings.coverage_interval_seconds)

    async def run_coverage_check(self) -> None:
        users = await self.repository.list_active_users()
        if not users:
            return
        tenants = {user.tenant_key for user in users}
        for tenant_key in tenants:
            bot_chats = await self.feishu.list_bot_chat_ids()
            await self.repository.set_bot_chats(tenant_key, bot_chats)
        for user in users:
            try:
                if user.access_token_expires_at and user.access_token_expires_at <= datetime.now(
                    UTC
                ) + timedelta(minutes=5):
                    tokens = await self.feishu.refresh_user_token(user.refresh_token)
                    user = await self.repository.update_user_tokens(user, tokens)
                memberships = await self.feishu.list_user_chats(user.access_token)
                await self.repository.replace_user_chats(user, memberships)
            except FeishuAPIError as exc:
                if exc.authorization_failed:
                    await self.repository.mark_user_auth_expired(user.id)
                    continue
                raise

    async def _reconciliation_loop(self) -> None:
        while True:
            try:
                await self.repository.reconcile_outbox()
                await self._pull_bitable_changes()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Bitable reconciliation failed")
            await asyncio.sleep(self.settings.reconciliation_interval_seconds)

    async def _pull_bitable_changes(self) -> None:
        local_records = await self.repository.inbox_reconciliation_state()
        if not local_records:
            return
        for remote in await self.bitable.list_records("inbox"):
            record_id = str(remote.get("record_id") or "")
            local = local_records.get(record_id)
            if not local:
                continue
            fields = remote.get("fields") or {}
            try:
                remote_version_raw = fields.get("版本")
                if remote_version_raw is None:
                    continue
                remote_version = int(remote_version_raw)
                remote_status = InboxStatus(str(fields.get("处理状态") or ""))
            except (TypeError, ValueError):
                continue
            remote_note = str(fields.get("处理备注") or "")[:4000]
            if remote_status.value == local["status"] and remote_note == local["note"]:
                continue
            changed_at_raw = fields.get("表格修改时间")
            try:
                if changed_at_raw is None:
                    raise ValueError
                changed_at = datetime.fromtimestamp(int(changed_at_raw) / 1000, tz=UTC)
            except (TypeError, ValueError, OSError):
                changed_at = None
            if remote_version != int(local["version"]) and changed_at is None:
                continue
            await self.repository.update_inbox_item(
                UUID(str(local["id"])),
                remote_status,
                remote_note,
                expected_version=remote_version,
                changed_at=changed_at,
            )

    async def _retention_loop(self) -> None:
        while True:
            try:
                before = datetime.now(UTC) - timedelta(days=self.settings.content_retention_days)
                purged = await self.repository.purge_expired_content(before)
                if purged:
                    logger.info("expired message bodies removed", extra={"count": purged})
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("retention cleanup failed")
            await asyncio.sleep(self.settings.retention_interval_seconds)


def _coalesce_outbox_jobs(
    jobs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[UUID]]:
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    superseded: list[UUID] = []
    for job in jobs:
        key = (
            str(job["entity_type"]),
            str(job["entity_id"]),
            str(job["table_key"]),
        )
        previous = latest.get(key)
        if previous is not None:
            superseded.append(UUID(str(previous["id"])))
        latest[key] = job
    return list(latest.values()), superseded
