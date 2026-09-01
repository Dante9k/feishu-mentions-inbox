from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .models import (
    Chat,
    ChatMembership,
    ClaimedJob,
    CoverageStatus,
    InboxItem,
    InboxStatus,
    IncomingMessage,
    MentionType,
    OAuthTokens,
    OAuthUserInfo,
    SourceMessage,
    SourceState,
    User,
)
from .security import TokenCipher


def _as_uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


class PostgresRepository:
    def __init__(self, database_url: str, token_cipher: TokenCipher):
        self.pool = cast(
            AsyncConnectionPool[AsyncConnection[dict[str, Any]]],
            AsyncConnectionPool(
                database_url,
                open=False,
                kwargs={"row_factory": dict_row},
                min_size=1,
                max_size=10,
            ),
        )
        self._token_cipher = token_cipher

    async def open(self) -> None:
        await self.pool.open()
        migration = Path(__file__).parent / "migrations" / "001_init.sql"
        sql = await asyncio.to_thread(migration.read_text, encoding="utf-8")
        async with self.pool.connection() as conn:
            await conn.execute(sql)
            await conn.execute(
                """
                UPDATE incoming_events SET status='failed', available_at=NOW(),
                    last_error='recovered after service restart', updated_at=NOW()
                WHERE status='processing'
                """
            )
            await conn.execute(
                """
                UPDATE outbox_jobs SET status='failed', available_at=NOW(),
                    last_error='recovered after service restart', updated_at=NOW()
                WHERE status='processing'
                """
            )

    async def close(self) -> None:
        await self.pool.close()

    @staticmethod
    def _user(row: dict[str, Any]) -> User:
        return User(
            id=_as_uuid(row["id"]),
            tenant_key=row["tenant_key"],
            user_id=row["user_id"],
            open_id=row["open_id"],
            name=row["name"],
            enabled=row["enabled"],
            authorized=row["authorized"],
            include_at_all=row["include_at_all"],
            departed=row["departed"],
            access_token=row.get("access_token_encrypted", ""),
            refresh_token=row.get("refresh_token_encrypted", ""),
            access_token_expires_at=row["access_token_expires_at"],
            refresh_token_expires_at=row["refresh_token_expires_at"],
            last_coverage_check_at=row["last_coverage_check_at"],
        )

    def _user_with_tokens(self, row: dict[str, Any]) -> User:
        user = self._user(row)
        user.access_token = self._token_cipher.decrypt(user.access_token)
        user.refresh_token = self._token_cipher.decrypt(user.refresh_token)
        return user

    @staticmethod
    def _chat(row: dict[str, Any]) -> Chat:
        return Chat(
            id=_as_uuid(row["id"]),
            tenant_key=row["tenant_key"],
            chat_id=row["chat_id"],
            name=row["name"],
            external=row["external"],
            bot_present=row["bot_present"],
            disbanded=row["disbanded"],
            unsupported=row["unsupported"],
            unsupported_reason=row["unsupported_reason"],
            last_checked_at=row["last_checked_at"],
        )

    @staticmethod
    def _source(row: dict[str, Any]) -> SourceMessage:
        return SourceMessage(
            id=_as_uuid(row["id"]),
            tenant_key=row["tenant_key"],
            message_id=row["message_id"],
            chat_id=row["chat_id"],
            chat_name=row["chat_name"],
            sender_id=row["sender_id"],
            sender_name=row["sender_name"],
            message_type=row["message_type"],
            content=row["content"],
            sent_at=row["sent_at"],
            root_id=row["root_id"],
            parent_id=row["parent_id"],
            source_state=SourceState(row["source_state"]),
            recalled_at=row["recalled_at"],
        )

    @staticmethod
    def _item(row: dict[str, Any]) -> InboxItem:
        return InboxItem(
            id=_as_uuid(row["id"]),
            source_message_id=_as_uuid(row["source_message_id"]),
            target_user_id=_as_uuid(row["target_user_id"]),
            mention_type=MentionType(row["mention_type"]),
            status=InboxStatus(row["status"]),
            note=row["note"],
            handled_at=row["handled_at"],
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def enqueue_event(self, event_key: str, event_type: str, payload: dict[str, Any]) -> bool:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO incoming_events(id, event_key, event_type, payload)
                VALUES (%s, %s, %s, %s::jsonb)
                ON CONFLICT (event_key) DO NOTHING
                """,
                (uuid4(), event_key, event_type, json.dumps(payload)),
            )
            return cursor.rowcount == 1

    async def claim_events(self, limit: int = 20) -> list[ClaimedJob]:
        async with self.pool.connection() as conn, conn.transaction():
            cursor = await conn.execute(
                """
                SELECT id, event_type, payload, attempts
                FROM incoming_events
                WHERE (status IN ('pending', 'failed')
                       AND available_at <= NOW())
                   OR (status='processing' AND updated_at < NOW()-INTERVAL '5 minutes')
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT %s
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
            if rows:
                await conn.execute(
                    """
                    UPDATE incoming_events
                    SET status='processing', attempts=attempts+1, updated_at=NOW()
                    WHERE id = ANY(%s)
                    """,
                    ([row["id"] for row in rows],),
                )
        return [
            ClaimedJob(
                id=_as_uuid(row["id"]),
                event_type=row["event_type"],
                payload=row["payload"]
                if isinstance(row["payload"], dict)
                else json.loads(row["payload"]),
                attempts=row["attempts"] + 1,
            )
            for row in rows
        ]

    async def finish_event(self, job_id: UUID) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                UPDATE incoming_events SET status='succeeded', payload='{}'::jsonb,
                    last_error='', updated_at=NOW() WHERE id=%s
                """,
                (job_id,),
            )

    async def retry_event(self, job_id: UUID, error: str, attempts: int) -> None:
        delay = min(300, 2 ** min(attempts, 8))
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                UPDATE incoming_events
                SET status=%s, last_error=%s, available_at=NOW()+(%s * INTERVAL '1 second'),
                    updated_at=NOW()
                WHERE id=%s
                """,
                ("failed", error[:1000], delay, job_id),
            )

    async def find_active_users(
        self, tenant_key: str, user_ids: set[str], open_ids: set[str]
    ) -> list[User]:
        if not user_ids and not open_ids:
            return []
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM users
                WHERE tenant_key=%s AND enabled AND authorized AND NOT departed
                  AND (user_id = ANY(%s) OR open_id = ANY(%s))
                """,
                (tenant_key, list(user_ids), list(open_ids)),
            )
            rows = await cursor.fetchall()
        return [self._user(row) for row in rows]

    async def find_at_all_users(
        self,
        tenant_key: str,
        chat_id: str,
        member_user_ids: set[str] | None = None,
    ) -> list[User]:
        if member_user_ids is not None and not member_user_ids:
            return []
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT u.* FROM users u
                JOIN user_chats uc ON uc.user_pk=u.id
                JOIN chats c ON c.id=uc.chat_pk
                WHERE u.tenant_key=%s AND c.chat_id=%s
                  AND u.enabled AND u.authorized AND NOT u.departed AND u.include_at_all
                  AND uc.active AND NOT c.external AND NOT c.disbanded
                  AND (%s OR u.user_id = ANY(%s))
                """,
                (
                    tenant_key,
                    chat_id,
                    member_user_ids is None,
                    list(member_user_ids or set()),
                ),
            )
            rows = await cursor.fetchall()
        return [self._user(row) for row in rows]

    async def get_chat(self, tenant_key: str, chat_id: str) -> Chat | None:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM chats WHERE tenant_key=%s AND chat_id=%s",
                (tenant_key, chat_id),
            )
            row = await cursor.fetchone()
        return self._chat(row) if row else None

    async def upsert_chat(self, chat: Chat) -> Chat:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO chats(
                    id, tenant_key, chat_id, name, external, bot_present,
                    disbanded, last_checked_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_key, chat_id) DO UPDATE SET
                    name=CASE WHEN EXCLUDED.name='' THEN chats.name ELSE EXCLUDED.name END,
                    external=EXCLUDED.external,
                    bot_present=(chats.bot_present OR EXCLUDED.bot_present),
                    disbanded=EXCLUDED.disbanded,
                    last_checked_at=EXCLUDED.last_checked_at,
                    updated_at=NOW()
                RETURNING *
                """,
                (
                    chat.id,
                    chat.tenant_key,
                    chat.chat_id,
                    chat.name,
                    chat.external,
                    chat.bot_present,
                    chat.disbanded,
                    chat.last_checked_at,
                ),
            )
            row = await cursor.fetchone()
        if not row:
            raise RuntimeError("failed to upsert chat")
        return self._chat(row)

    async def save_message_targets(
        self,
        incoming: IncomingMessage,
        chat: Chat,
        normalized_content: str,
        targets: dict[UUID, tuple[User, MentionType]],
    ) -> tuple[SourceMessage, list[InboxItem]]:
        async with self.pool.connection() as conn, conn.transaction():
            source_id = uuid4()
            cursor = await conn.execute(
                """
                INSERT INTO source_messages(
                    id, tenant_key, message_id, chat_id, chat_name, sender_id,
                    sender_name, message_type, content, sent_at, root_id, parent_id
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_key, message_id) DO UPDATE SET
                    chat_name=CASE WHEN source_messages.chat_name='' THEN EXCLUDED.chat_name
                                   ELSE source_messages.chat_name END
                RETURNING *
                """,
                (
                    source_id,
                    incoming.tenant_key,
                    incoming.message_id,
                    incoming.chat_id,
                    chat.name or incoming.chat_id,
                    incoming.sender_user_id or incoming.sender_open_id,
                    incoming.sender_name,
                    incoming.message_type,
                    normalized_content,
                    incoming.create_time,
                    incoming.root_id,
                    incoming.parent_id,
                ),
            )
            source_row = await cursor.fetchone()
            if not source_row:
                raise RuntimeError("failed to store source message")
            source = self._source(source_row)
            created: list[InboxItem] = []
            for user_pk, (_, mention_type) in targets.items():
                item_id = uuid4()
                item_cursor = await conn.execute(
                    """
                    INSERT INTO inbox_items(
                        id, source_message_id, target_user_id, mention_type
                    ) VALUES (%s,%s,%s,%s)
                    ON CONFLICT (source_message_id, target_user_id) DO NOTHING
                    RETURNING *
                    """,
                    (item_id, source.id, user_pk, mention_type.value),
                )
                row = await item_cursor.fetchone()
                if not row:
                    continue
                item = self._item(row)
                created.append(item)
                await self._enqueue_outbox_conn(
                    conn,
                    entity_type="inbox",
                    entity_id=item.id,
                    table_key="inbox",
                    operation="upsert",
                    version=item.version,
                )
        return source, created

    async def recall_message(
        self, tenant_key: str, message_id: str, recalled_at: datetime
    ) -> list[InboxItem]:
        async with self.pool.connection() as conn, conn.transaction():
            source_cursor = await conn.execute(
                """
                UPDATE source_messages SET content='', source_state=%s, recalled_at=%s,
                    updated_at=NOW()
                WHERE tenant_key=%s AND message_id=%s
                RETURNING id
                """,
                (SourceState.RECALLED.value, recalled_at, tenant_key, message_id),
            )
            source_row = await source_cursor.fetchone()
            if not source_row:
                return []
            items_cursor = await conn.execute(
                """
                UPDATE inbox_items SET
                    status=CASE WHEN status IN ('已处理','忽略') THEN status ELSE '忽略' END,
                    handled_at=CASE WHEN status IN ('已处理','忽略') THEN handled_at ELSE %s END,
                    version=version+1, updated_at=NOW()
                WHERE source_message_id=%s
                RETURNING *
                """,
                (recalled_at, source_row["id"]),
            )
            rows = await items_cursor.fetchall()
            items = [self._item(row) for row in rows]
            for item in items:
                await self._enqueue_outbox_conn(
                    conn,
                    entity_type="inbox",
                    entity_id=item.id,
                    table_key="inbox",
                    operation="upsert",
                    version=item.version,
                )
        return items

    async def get_inbox_context(
        self, item_id: UUID
    ) -> tuple[InboxItem, SourceMessage, User] | None:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT
                    i.id AS i_id, i.source_message_id, i.target_user_id, i.mention_type,
                    i.status, i.note, i.handled_at, i.version, i.created_at AS i_created_at,
                    i.updated_at AS i_updated_at,
                    s.*,
                    u.id AS u_id, u.tenant_key AS u_tenant_key, u.user_id AS u_user_id,
                    u.open_id AS u_open_id, u.name AS u_name, u.enabled AS u_enabled,
                    u.authorized AS u_authorized, u.include_at_all AS u_include_at_all,
                    u.departed AS u_departed, u.access_token_encrypted,
                    u.refresh_token_encrypted, u.access_token_expires_at,
                    u.refresh_token_expires_at, u.last_coverage_check_at
                FROM inbox_items i
                JOIN source_messages s ON s.id=i.source_message_id
                JOIN users u ON u.id=i.target_user_id
                WHERE i.id=%s
                """,
                (item_id,),
            )
            row = await cursor.fetchone()
        if not row:
            return None
        item = InboxItem(
            id=_as_uuid(row["i_id"]),
            source_message_id=_as_uuid(row["source_message_id"]),
            target_user_id=_as_uuid(row["target_user_id"]),
            mention_type=MentionType(row["mention_type"]),
            status=InboxStatus(row["status"]),
            note=row["note"],
            handled_at=row["handled_at"],
            version=row["version"],
            created_at=row["i_created_at"],
            updated_at=row["i_updated_at"],
        )
        source = self._source(row)
        user = User(
            id=_as_uuid(row["u_id"]),
            tenant_key=row["u_tenant_key"],
            user_id=row["u_user_id"],
            open_id=row["u_open_id"],
            name=row["u_name"],
            enabled=row["u_enabled"],
            authorized=row["u_authorized"],
            include_at_all=row["u_include_at_all"],
            departed=row["u_departed"],
            access_token=row["access_token_encrypted"],
            refresh_token=row["refresh_token_encrypted"],
            access_token_expires_at=row["access_token_expires_at"],
            refresh_token_expires_at=row["refresh_token_expires_at"],
            last_coverage_check_at=row["last_coverage_check_at"],
        )
        return item, source, user

    async def update_inbox_item(
        self,
        item_id: UUID,
        status: InboxStatus,
        note: str,
        expected_version: int | None = None,
        changed_at: datetime | None = None,
    ) -> InboxItem | None:
        async with self.pool.connection() as conn, conn.transaction():
            current_cursor = await conn.execute(
                "SELECT * FROM inbox_items WHERE id=%s FOR UPDATE", (item_id,)
            )
            current_row = await current_cursor.fetchone()
            if not current_row:
                return None
            current = self._item(current_row)
            if current.status == status and current.note == note:
                return current
            if (
                expected_version is not None
                and current.version != expected_version
                and (changed_at is None or changed_at <= current.updated_at)
            ):
                return None
            cursor = await conn.execute(
                """
                UPDATE inbox_items SET status=%s, note=%s,
                    handled_at=CASE WHEN %s IN ('已处理','忽略') THEN COALESCE(handled_at,NOW())
                                    ELSE NULL END,
                    version=version+1,
                    updated_at=GREATEST(NOW(),COALESCE(%s,NOW()))
                WHERE id=%s
                RETURNING *
                """,
                (status.value, note, status.value, changed_at, item_id),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            item = self._item(row)
            await self._enqueue_outbox_conn(
                conn,
                entity_type="inbox",
                entity_id=item.id,
                table_key="inbox",
                operation="upsert",
                version=item.version,
            )
        return item

    async def enable_user(self, user: User, bitable_record_id: str = "") -> User:
        async with self.pool.connection() as conn, conn.transaction():
            existing_cursor = await conn.execute(
                """
                SELECT * FROM users
                WHERE tenant_key=%s AND user_id=%s
                FOR UPDATE
                """,
                (user.tenant_key, user.user_id),
            )
            existing_row = await existing_cursor.fetchone()
            if existing_row:
                existing = self._user(existing_row)
                open_id_unchanged = not user.open_id or user.open_id == existing.open_id
                name_unchanged = not user.name or user.name == existing.name
                if (
                    existing.enabled
                    and not existing.departed
                    and open_id_unchanged
                    and name_unchanged
                ):
                    await self._bind_user_mapping_conn(conn, existing.id, bitable_record_id)
                    return existing
            cursor = await conn.execute(
                """
                INSERT INTO users(id, tenant_key, user_id, open_id, name, enabled, departed)
                VALUES (%s,%s,%s,%s,%s,TRUE,FALSE)
                ON CONFLICT (tenant_key, user_id) DO UPDATE SET
                    enabled=TRUE, departed=FALSE,
                    open_id=CASE WHEN EXCLUDED.open_id='' THEN users.open_id ELSE EXCLUDED.open_id END,
                    name=CASE WHEN EXCLUDED.name='' THEN users.name ELSE EXCLUDED.name END,
                    updated_at=NOW()
                RETURNING *
                """,
                (user.id, user.tenant_key, user.user_id, user.open_id, user.name),
            )
            row = await cursor.fetchone()
            if not row:
                raise RuntimeError("failed to enable user")
            stored = self._user(row)
            await self._bind_user_mapping_conn(conn, stored.id, bitable_record_id)
            await self._enqueue_user_projections(conn, stored)
        return stored

    async def disable_user(
        self, tenant_key: str, user_id: str, bitable_record_id: str = ""
    ) -> User | None:
        async with self.pool.connection() as conn, conn.transaction():
            current_cursor = await conn.execute(
                """
                SELECT * FROM users
                WHERE tenant_key=%s AND user_id=%s
                FOR UPDATE
                """,
                (tenant_key, user_id),
            )
            current_row = await current_cursor.fetchone()
            if not current_row:
                return None
            current = self._user(current_row)
            await self._bind_user_mapping_conn(conn, current.id, bitable_record_id)
            if not current.enabled and not current.authorized:
                return current
            cursor = await conn.execute(
                """
                UPDATE users SET enabled=FALSE, authorized=FALSE,
                    access_token_encrypted='', refresh_token_encrypted='', updated_at=NOW()
                WHERE tenant_key=%s AND user_id=%s RETURNING *
                """,
                (tenant_key, user_id),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            user = self._user(row)
            await self._enqueue_user_projections(conn, user)
        return user

    async def _bind_user_mapping_conn(
        self,
        conn: AsyncConnection[dict[str, Any]],
        user_pk: UUID,
        record_id: str,
    ) -> None:
        if not record_id:
            return
        cursor = await conn.execute(
            """
            SELECT entity_id, record_id FROM bitable_mappings
            WHERE (entity_type='user' AND entity_id=%s AND table_key='users')
               OR (table_key='users' AND record_id=%s)
            """,
            (user_pk, record_id),
        )
        for row in await cursor.fetchall():
            if _as_uuid(row["entity_id"]) != user_pk or row["record_id"] != record_id:
                raise ValueError("Bitable user record mapping conflicts with another row")
        await conn.execute(
            """
            INSERT INTO bitable_mappings(
                entity_type, entity_id, table_key, record_id, synced_version
            ) VALUES ('user',%s,'users',%s,0)
            ON CONFLICT (entity_type, entity_id, table_key) DO NOTHING
            """,
            (user_pk, record_id),
        )

    async def get_enabled_user(self, tenant_key: str, user_id: str) -> User | None:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM users WHERE tenant_key=%s AND user_id=%s AND enabled",
                (tenant_key, user_id),
            )
            row = await cursor.fetchone()
        return self._user_with_tokens(row) if row else None

    async def activate_user(self, info: OAuthUserInfo, tokens: OAuthTokens) -> User | None:
        now = datetime.now(UTC)
        async with self.pool.connection() as conn, conn.transaction():
            cursor = await conn.execute(
                """
                UPDATE users SET open_id=%s, name=CASE WHEN %s='' THEN name ELSE %s END,
                    authorized=TRUE, departed=FALSE,
                    access_token_encrypted=%s, refresh_token_encrypted=%s,
                    access_token_expires_at=%s, refresh_token_expires_at=%s,
                    updated_at=NOW()
                WHERE tenant_key=%s AND user_id=%s AND enabled
                RETURNING *
                """,
                (
                    info.open_id,
                    info.name,
                    info.name,
                    self._token_cipher.encrypt(tokens.access_token),
                    self._token_cipher.encrypt(tokens.refresh_token),
                    now + timedelta(seconds=tokens.expires_in),
                    now + timedelta(seconds=tokens.refresh_expires_in),
                    info.tenant_key,
                    info.user_id,
                ),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            user = self._user_with_tokens(row)
            await self._enqueue_user_projections(conn, user)
        return user

    async def update_user_tokens(self, user: User, tokens: OAuthTokens) -> User:
        now = datetime.now(UTC)
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE users SET access_token_encrypted=%s, refresh_token_encrypted=%s,
                    access_token_expires_at=%s, refresh_token_expires_at=%s,
                    authorized=TRUE, updated_at=NOW()
                WHERE id=%s RETURNING *
                """,
                (
                    self._token_cipher.encrypt(tokens.access_token),
                    self._token_cipher.encrypt(tokens.refresh_token),
                    now + timedelta(seconds=tokens.expires_in),
                    now + timedelta(seconds=tokens.refresh_expires_in),
                    user.id,
                ),
            )
            row = await cursor.fetchone()
        if not row:
            raise RuntimeError("failed to update OAuth tokens")
        return self._user_with_tokens(row)

    async def mark_user_auth_expired(self, user_id: UUID) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            user_cursor = await conn.execute(
                """
                UPDATE users SET authorized=FALSE, access_token_encrypted='',
                    refresh_token_encrypted='', updated_at=NOW() WHERE id=%s
                RETURNING *
                """,
                (user_id,),
            )
            coverage_cursor = await conn.execute(
                """
                UPDATE user_chats SET coverage_status=%s WHERE user_pk=%s AND active
                RETURNING id
                """,
                (CoverageStatus.AUTH_EXPIRED.value, user_id),
            )
            projection_version = int(datetime.now(UTC).timestamp() * 1_000_000)
            for row in await coverage_cursor.fetchall():
                await self._enqueue_outbox_conn(
                    conn,
                    entity_type="coverage",
                    entity_id=_as_uuid(row["id"]),
                    table_key="coverage",
                    operation="upsert",
                    version=projection_version,
                )
            user_row = await user_cursor.fetchone()
            if user_row:
                await self._enqueue_user_projections(
                    conn, self._user(user_row), version=projection_version
                )

    async def list_active_users(self) -> list[User]:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM users WHERE enabled AND authorized AND NOT departed ORDER BY name"
            )
            rows = await cursor.fetchall()
        return [self._user_with_tokens(row) for row in rows]

    async def list_users(self) -> list[User]:
        async with self.pool.connection() as conn:
            cursor = await conn.execute("SELECT * FROM users ORDER BY name, user_id")
            rows = await cursor.fetchall()
        return [self._user(row) for row in rows]

    async def update_user_setting(
        self, tenant_key: str, user_id: str, include_at_all: bool
    ) -> User | None:
        async with self.pool.connection() as conn, conn.transaction():
            current_cursor = await conn.execute(
                """
                SELECT * FROM users
                WHERE tenant_key=%s AND user_id=%s AND enabled
                FOR UPDATE
                """,
                (tenant_key, user_id),
            )
            current_row = await current_cursor.fetchone()
            if not current_row:
                return None
            current = self._user(current_row)
            if current.include_at_all == include_at_all:
                return current
            cursor = await conn.execute(
                """
                UPDATE users SET include_at_all=%s, updated_at=NOW()
                WHERE tenant_key=%s AND user_id=%s AND enabled
                RETURNING *
                """,
                (include_at_all, tenant_key, user_id),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            user = self._user(row)
            await self._enqueue_user_projections(
                conn, user, version=int(datetime.now(UTC).timestamp() * 1_000_000)
            )
        return user

    async def replace_user_chats(self, user: User, memberships: list[ChatMembership]) -> list[Chat]:
        now = datetime.now(UTC)
        async with self.pool.connection() as conn, conn.transaction():
            stale_cursor = await conn.execute(
                """
                UPDATE user_chats SET active=FALSE, coverage_status=%s
                WHERE user_pk=%s AND active=TRUE RETURNING id
                """,
                (CoverageStatus.UNSUPPORTED.value, user.id),
            )
            stale_rows = await stale_cursor.fetchall()
            projection_version = int(now.timestamp() * 1_000_000)
            for stale in stale_rows:
                await self._enqueue_outbox_conn(
                    conn,
                    entity_type="coverage",
                    entity_id=_as_uuid(stale["id"]),
                    table_key="coverage",
                    operation="upsert",
                    version=projection_version,
                )
            chats: list[Chat] = []
            for membership in memberships:
                cursor = await conn.execute(
                    """
                    INSERT INTO chats(
                        id, tenant_key, chat_id, name, external, last_checked_at
                    ) VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (tenant_key, chat_id) DO UPDATE SET
                        name=EXCLUDED.name, external=EXCLUDED.external,
                        last_checked_at=EXCLUDED.last_checked_at, updated_at=NOW()
                    RETURNING *
                    """,
                    (
                        uuid4(),
                        user.tenant_key,
                        membership.chat_id,
                        membership.name,
                        membership.external,
                        now,
                    ),
                )
                row = await cursor.fetchone()
                if not row:
                    continue
                chat = self._chat(row)
                chats.append(chat)
                coverage = chat.coverage_status
                coverage_id = uuid4()
                coverage_cursor = await conn.execute(
                    """
                    INSERT INTO user_chats(
                        id, user_pk, chat_pk, coverage_status, active, last_seen_at
                    ) VALUES (%s,%s,%s,%s,TRUE,%s)
                    ON CONFLICT (user_pk, chat_pk) DO UPDATE SET
                        coverage_status=EXCLUDED.coverage_status,
                        active=TRUE,
                        last_seen_at=EXCLUDED.last_seen_at
                    RETURNING id
                    """,
                    (coverage_id, user.id, chat.id, coverage.value, now),
                )
                coverage_row = await coverage_cursor.fetchone()
                if not coverage_row:
                    raise RuntimeError("failed to upsert user chat coverage")
                coverage_id = _as_uuid(coverage_row["id"])
                await self._enqueue_outbox_conn(
                    conn,
                    entity_type="coverage",
                    entity_id=coverage_id,
                    table_key="coverage",
                    operation="upsert",
                    version=projection_version,
                )
            await conn.execute(
                "UPDATE users SET last_coverage_check_at=%s, updated_at=NOW() WHERE id=%s",
                (now, user.id),
            )
            await self._enqueue_user_projections(conn, user, version=projection_version)
        return chats

    async def set_bot_chats(self, tenant_key: str, chat_ids: set[str]) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "UPDATE chats SET bot_present=FALSE, updated_at=NOW() WHERE tenant_key=%s",
                (tenant_key,),
            )
            if chat_ids:
                await conn.execute(
                    """
                    UPDATE chats SET bot_present=TRUE, disbanded=FALSE,
                        unsupported=FALSE, unsupported_reason='', updated_at=NOW()
                    WHERE tenant_key=%s AND chat_id = ANY(%s)
                    """,
                    (tenant_key, list(chat_ids)),
                )
            await conn.execute(
                """
                UPDATE user_chats uc SET coverage_status=CASE
                    WHEN c.external OR c.disbanded OR c.unsupported THEN %s
                    WHEN c.bot_present THEN %s ELSE %s END
                FROM chats c WHERE c.id=uc.chat_pk AND c.tenant_key=%s
                """,
                (
                    CoverageStatus.UNSUPPORTED.value,
                    CoverageStatus.COVERED.value,
                    CoverageStatus.BOT_MISSING.value,
                    tenant_key,
                ),
            )

    async def set_bot_membership(
        self, tenant_key: str, chat_id: str, present: bool, name: str = ""
    ) -> Chat:
        async with self.pool.connection() as conn, conn.transaction():
            cursor = await conn.execute(
                """
                INSERT INTO chats(id, tenant_key, chat_id, name, bot_present)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_key, chat_id) DO UPDATE SET
                    name=CASE WHEN EXCLUDED.name='' THEN chats.name ELSE EXCLUDED.name END,
                    bot_present=EXCLUDED.bot_present, disbanded=FALSE,
                    unsupported=CASE WHEN EXCLUDED.bot_present THEN FALSE
                                     ELSE chats.unsupported END,
                    unsupported_reason=CASE WHEN EXCLUDED.bot_present THEN ''
                                            ELSE chats.unsupported_reason END,
                    updated_at=NOW()
                RETURNING *
                """,
                (uuid4(), tenant_key, chat_id, name or chat_id, present),
            )
            row = await cursor.fetchone()
            if not row:
                raise RuntimeError("failed to update bot membership")
            chat = self._chat(row)
            coverage_cursor = await conn.execute(
                """
                UPDATE user_chats SET coverage_status=%s
                WHERE chat_pk=%s
                RETURNING id, user_pk
                """,
                (chat.coverage_status.value, chat.id),
            )
            projection_version = int(datetime.now(UTC).timestamp() * 1_000_000)
            await self._enqueue_coverage_and_users(
                conn, await coverage_cursor.fetchall(), projection_version
            )
        return chat

    async def disband_chat(self, tenant_key: str, chat_id: str) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            cursor = await conn.execute(
                """
                UPDATE chats SET disbanded=TRUE, bot_present=FALSE, updated_at=NOW()
                WHERE tenant_key=%s AND chat_id=%s RETURNING id
                """,
                (tenant_key, chat_id),
            )
            row = await cursor.fetchone()
            if row:
                coverage_cursor = await conn.execute(
                    """
                    UPDATE user_chats SET coverage_status=%s WHERE chat_pk=%s
                    RETURNING id, user_pk
                    """,
                    (CoverageStatus.UNSUPPORTED.value, row["id"]),
                )
                await self._enqueue_coverage_and_users(
                    conn,
                    await coverage_cursor.fetchall(),
                    int(datetime.now(UTC).timestamp() * 1_000_000),
                )

    async def set_chat_unsupported(
        self, tenant_key: str, chat_id: str, unsupported: bool, reason: str
    ) -> Chat | None:
        async with self.pool.connection() as conn, conn.transaction():
            cursor = await conn.execute(
                """
                UPDATE chats SET unsupported=%s, unsupported_reason=%s, updated_at=NOW()
                WHERE tenant_key=%s AND chat_id=%s
                RETURNING *
                """,
                (unsupported, reason[:500] if unsupported else "", tenant_key, chat_id),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            chat = self._chat(row)
            coverage_cursor = await conn.execute(
                """
                UPDATE user_chats SET coverage_status=%s WHERE chat_pk=%s AND active
                RETURNING id, user_pk
                """,
                (chat.coverage_status.value, chat.id),
            )
            await self._enqueue_coverage_and_users(
                conn,
                await coverage_cursor.fetchall(),
                int(datetime.now(UTC).timestamp() * 1_000_000),
            )
        return chat

    async def purge_expired_content(self, before: datetime) -> int:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                """
                UPDATE incoming_events
                SET payload='{}'::jsonb, status='succeeded',
                    last_error='discarded by content retention policy', updated_at=NOW()
                WHERE created_at < %s AND status <> 'processing' AND payload <> '{}'::jsonb
                """,
                (before,),
            )
            source_cursor = await conn.execute(
                """
                UPDATE source_messages SET content='', updated_at=NOW()
                WHERE sent_at < %s AND content <> ''
                RETURNING id
                """,
                (before,),
            )
            source_rows = await source_cursor.fetchall()
            if not source_rows:
                return 0
            item_cursor = await conn.execute(
                """
                UPDATE inbox_items SET version=version+1, updated_at=NOW()
                WHERE source_message_id = ANY(%s)
                RETURNING id, version
                """,
                ([row["id"] for row in source_rows],),
            )
            for row in await item_cursor.fetchall():
                await self._enqueue_outbox_conn(
                    conn,
                    entity_type="inbox",
                    entity_id=_as_uuid(row["id"]),
                    table_key="inbox",
                    operation="upsert",
                    version=int(row["version"]),
                )
            return len(source_rows)

    async def _enqueue_user_projections(
        self,
        conn: AsyncConnection[dict[str, Any]],
        user: User,
        version: int | None = None,
    ) -> None:
        projection_version = version or int(datetime.now(UTC).timestamp() * 1_000_000)
        for table_key in ("settings", "users"):
            await self._enqueue_outbox_conn(
                conn,
                entity_type="user",
                entity_id=user.id,
                table_key=table_key,
                operation="upsert",
                version=projection_version,
            )

    async def _enqueue_coverage_and_users(
        self,
        conn: AsyncConnection[dict[str, Any]],
        coverage_rows: list[dict[str, Any]],
        projection_version: int,
    ) -> None:
        user_ids: set[UUID] = set()
        for row in coverage_rows:
            user_ids.add(_as_uuid(row["user_pk"]))
            await self._enqueue_outbox_conn(
                conn,
                entity_type="coverage",
                entity_id=_as_uuid(row["id"]),
                table_key="coverage",
                operation="upsert",
                version=projection_version,
            )
        if not user_ids:
            return
        cursor = await conn.execute("SELECT * FROM users WHERE id = ANY(%s)", (list(user_ids),))
        for row in await cursor.fetchall():
            await self._enqueue_user_projections(conn, self._user(row), version=projection_version)

    async def _enqueue_outbox_conn(
        self,
        conn: AsyncConnection[dict[str, Any]],
        *,
        entity_type: str,
        entity_id: UUID,
        table_key: str,
        operation: str,
        version: int,
    ) -> None:
        key = f"{entity_type}:{entity_id}:{table_key}:v{version}"
        await conn.execute(
            """
            INSERT INTO outbox_jobs(
                id, idempotency_key, entity_type, entity_id, table_key, operation, payload
            ) VALUES (%s,%s,%s,%s,%s,%s,'{}'::jsonb)
            ON CONFLICT (idempotency_key) DO NOTHING
            """,
            (uuid4(), key, entity_type, entity_id, table_key, operation),
        )

    async def claim_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn, conn.transaction():
            cursor = await conn.execute(
                """
                SELECT * FROM outbox_jobs
                WHERE (status IN ('pending','failed')
                       AND available_at <= NOW())
                   OR (status='processing' AND updated_at < NOW()-INTERVAL '5 minutes')
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT %s
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
            if rows:
                await conn.execute(
                    """
                    UPDATE outbox_jobs SET status='processing', attempts=attempts+1,
                        updated_at=NOW() WHERE id = ANY(%s)
                    """,
                    ([row["id"] for row in rows],),
                )
        for row in rows:
            row["attempts"] += 1
        return rows

    async def get_mapping(
        self, entity_type: str, entity_id: UUID, table_key: str
    ) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM bitable_mappings
                WHERE entity_type=%s AND entity_id=%s AND table_key=%s
                """,
                (entity_type, entity_id, table_key),
            )
            return await cursor.fetchone()

    async def finish_outbox(
        self,
        job_id: UUID,
        *,
        entity_type: str,
        entity_id: UUID,
        table_key: str,
        record_id: str,
        synced_version: int,
    ) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO bitable_mappings(
                    entity_type, entity_id, table_key, record_id, synced_version
                ) VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (entity_type, entity_id, table_key) DO UPDATE SET
                    record_id=EXCLUDED.record_id,
                    synced_version=GREATEST(bitable_mappings.synced_version,EXCLUDED.synced_version),
                    updated_at=NOW()
                """,
                (entity_type, entity_id, table_key, record_id, synced_version),
            )
            await conn.execute(
                "UPDATE outbox_jobs SET status='succeeded', updated_at=NOW() WHERE id=%s",
                (job_id,),
            )

    async def retry_outbox(self, job_id: UUID, error: str, attempts: int) -> None:
        delay = min(600, 2 ** min(attempts, 9))
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                UPDATE outbox_jobs SET status=%s, last_error=%s,
                    available_at=NOW()+(%s * INTERVAL '1 second'), updated_at=NOW()
                WHERE id=%s
                """,
                ("failed", error[:1000], delay, job_id),
            )

    async def finish_superseded_outbox(self, job_ids: list[UUID]) -> None:
        if not job_ids:
            return
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                UPDATE outbox_jobs SET status='succeeded',
                    last_error='superseded by a newer projection', updated_at=NOW()
                WHERE id = ANY(%s)
                """,
                (job_ids,),
            )

    async def get_user_by_pk(self, user_pk: UUID) -> User | None:
        async with self.pool.connection() as conn:
            cursor = await conn.execute("SELECT * FROM users WHERE id=%s", (user_pk,))
            row = await cursor.fetchone()
        return self._user(row) if row else None

    async def get_coverage_context(
        self, coverage_id: UUID
    ) -> tuple[User, Chat, CoverageStatus, datetime] | None:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT uc.coverage_status, uc.last_seen_at,
                    u.id AS u_id, u.tenant_key AS u_tenant_key, u.user_id AS u_user_id,
                    u.open_id AS u_open_id, u.name AS u_name, u.enabled AS u_enabled,
                    u.authorized AS u_authorized, u.include_at_all AS u_include_at_all,
                    u.departed AS u_departed, u.access_token_encrypted,
                    u.refresh_token_encrypted, u.access_token_expires_at,
                    u.refresh_token_expires_at, u.last_coverage_check_at,
                    c.id AS c_id, c.tenant_key AS c_tenant_key, c.chat_id AS c_chat_id,
                    c.name AS c_name, c.external AS c_external,
                    c.bot_present AS c_bot_present, c.disbanded AS c_disbanded,
                    c.unsupported AS c_unsupported,
                    c.unsupported_reason AS c_unsupported_reason,
                    c.last_checked_at AS c_last_checked_at
                FROM user_chats uc
                JOIN users u ON u.id=uc.user_pk
                JOIN chats c ON c.id=uc.chat_pk
                WHERE uc.id=%s
                """,
                (coverage_id,),
            )
            row = await cursor.fetchone()
        if not row:
            return None
        user = User(
            id=_as_uuid(row["u_id"]),
            tenant_key=row["u_tenant_key"],
            user_id=row["u_user_id"],
            open_id=row["u_open_id"],
            name=row["u_name"],
            enabled=row["u_enabled"],
            authorized=row["u_authorized"],
            include_at_all=row["u_include_at_all"],
            departed=row["u_departed"],
            access_token=row["access_token_encrypted"],
            refresh_token=row["refresh_token_encrypted"],
            access_token_expires_at=row["access_token_expires_at"],
            refresh_token_expires_at=row["refresh_token_expires_at"],
            last_coverage_check_at=row["last_coverage_check_at"],
        )
        chat = Chat(
            id=_as_uuid(row["c_id"]),
            tenant_key=row["c_tenant_key"],
            chat_id=row["c_chat_id"],
            name=row["c_name"],
            external=row["c_external"],
            bot_present=row["c_bot_present"],
            disbanded=row["c_disbanded"],
            unsupported=row["c_unsupported"],
            unsupported_reason=row["c_unsupported_reason"],
            last_checked_at=row["c_last_checked_at"],
        )
        return user, chat, CoverageStatus(row["coverage_status"]), row["last_seen_at"]

    async def count_user_coverage(self, user_pk: UUID) -> tuple[int, int]:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE coverage_status=%s) AS covered
                FROM user_chats uc
                JOIN chats c ON c.id=uc.chat_pk
                WHERE uc.user_pk=%s AND uc.active=TRUE
                  AND NOT c.external AND NOT c.disbanded AND NOT c.unsupported
                """,
                (CoverageStatus.COVERED.value, user_pk),
            )
            row = await cursor.fetchone()
        return (int(row["covered"]), int(row["total"])) if row else (0, 0)

    async def reconcile_outbox(self) -> int:
        async with self.pool.connection() as conn, conn.transaction():
            cursor = await conn.execute(
                """
                SELECT i.id, i.version
                FROM inbox_items i
                LEFT JOIN bitable_mappings m
                  ON m.entity_type='inbox' AND m.entity_id=i.id AND m.table_key='inbox'
                WHERE m.entity_id IS NULL OR m.synced_version < i.version
                LIMIT 1000
                """
            )
            rows = await cursor.fetchall()
            for row in rows:
                await self._enqueue_outbox_conn(
                    conn,
                    entity_type="inbox",
                    entity_id=_as_uuid(row["id"]),
                    table_key="inbox",
                    operation="upsert",
                    version=row["version"],
                )
        return len(rows)

    async def inbox_reconciliation_state(self) -> dict[str, dict[str, Any]]:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT m.record_id, i.id, i.status, i.note, i.version
                FROM bitable_mappings m
                JOIN inbox_items i ON i.id=m.entity_id
                WHERE m.entity_type='inbox' AND m.table_key='inbox'
                """
            )
            rows = await cursor.fetchall()
        return {str(row["record_id"]): dict(row) for row in rows}

    async def health(self) -> bool:
        async with self.pool.connection() as conn:
            cursor = await conn.execute("SELECT 1 AS ok")
            row = await cursor.fetchone()
        return bool(row and row["ok"] == 1)

    async def coverage_summary(self) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT u.user_id, u.name, u.authorized,
                    COUNT(uc.id) FILTER (
                        WHERE NOT c.external AND NOT c.disbanded AND NOT c.unsupported
                    ) AS target_groups,
                    COUNT(uc.id) FILTER (
                        WHERE uc.coverage_status=%s AND NOT c.external AND NOT c.disbanded
                          AND NOT c.unsupported
                    ) AS covered_groups,
                    COUNT(uc.id) FILTER (
                        WHERE uc.coverage_status=%s AND NOT c.external AND NOT c.disbanded
                          AND NOT c.unsupported
                    ) AS missing_groups,
                    u.last_coverage_check_at
                FROM users u
                LEFT JOIN user_chats uc ON uc.user_pk=u.id AND uc.active=TRUE
                LEFT JOIN chats c ON c.id=uc.chat_pk
                WHERE u.enabled
                GROUP BY u.id ORDER BY u.name, u.user_id
                """,
                (CoverageStatus.COVERED.value, CoverageStatus.BOT_MISSING.value),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]
