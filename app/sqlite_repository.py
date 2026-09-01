from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import aiosqlite

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


def sqlite_database_path(database_url: str) -> str:
    """Return the local file path encoded in a sqlite:/// database URL."""

    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError("SQLite DATABASE_URL must start with sqlite:///")
    path = database_url[len(prefix) :]
    if not path:
        raise ValueError("SQLite DATABASE_URL must include a database path")
    return path


def _as_uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _db_time(value: datetime | None = None) -> str:
    timestamp = value or datetime.now(UTC)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _row(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _rows(rows: Iterable[aiosqlite.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _prepare_database_directory(path: str) -> None:
    Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


class SQLiteRepository:
    """Single-host persistent repository for lightweight deployments.

    A process-local lock and ``BEGIN IMMEDIATE`` serialize transactions. SQLite
    mode intentionally supports one application process; PostgreSQL remains the
    backend for horizontally scaled deployments.
    """

    def __init__(self, database_url: str, token_cipher: TokenCipher):
        self.path = sqlite_database_path(database_url)
        self._token_cipher = token_cipher
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLite repository is not open")
        return self._conn

    async def open(self) -> None:
        if self._conn is not None:
            return
        if self.path != ":memory:":
            await asyncio.to_thread(_prepare_database_directory, self.path)
        conn = await aiosqlite.connect(self.path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        self._conn = conn
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=FULL")
        migration = Path(__file__).parent / "migrations" / "001_init_sqlite.sql"
        sql = await asyncio.to_thread(migration.read_text, encoding="utf-8")
        await conn.executescript(sql)
        now = _db_time()
        await conn.execute(
            """
            UPDATE incoming_events SET status='failed', available_at=?,
                last_error='recovered after service restart', updated_at=?
            WHERE status='processing'
            """,
            (now, now),
        )
        await conn.execute(
            """
            UPDATE outbox_jobs SET status='failed', available_at=?,
                last_error='recovered after service restart', updated_at=?
            WHERE status='processing'
            """,
            (now, now),
        )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            conn = self._connection()
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                await conn.rollback()
                raise
            else:
                await conn.commit()

    async def _read_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        async with self._lock:
            cursor = await self._connection().execute(sql, params)
            return _row(await cursor.fetchone())

    async def _read_all(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        async with self._lock:
            cursor = await self._connection().execute(sql, params)
            return _rows(await cursor.fetchall())

    @staticmethod
    def _user(row: dict[str, Any]) -> User:
        return User(
            id=_as_uuid(row["id"]),
            tenant_key=str(row["tenant_key"]),
            user_id=str(row["user_id"]),
            open_id=str(row["open_id"]),
            name=str(row["name"]),
            enabled=bool(row["enabled"]),
            authorized=bool(row["authorized"]),
            include_at_all=bool(row["include_at_all"]),
            departed=bool(row["departed"]),
            access_token=str(row.get("access_token_encrypted", "")),
            refresh_token=str(row.get("refresh_token_encrypted", "")),
            access_token_expires_at=_datetime(row["access_token_expires_at"]),
            refresh_token_expires_at=_datetime(row["refresh_token_expires_at"]),
            last_coverage_check_at=_datetime(row["last_coverage_check_at"]),
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
            tenant_key=str(row["tenant_key"]),
            chat_id=str(row["chat_id"]),
            name=str(row["name"]),
            external=bool(row["external"]),
            bot_present=bool(row["bot_present"]),
            disbanded=bool(row["disbanded"]),
            unsupported=bool(row["unsupported"]),
            unsupported_reason=str(row["unsupported_reason"]),
            last_checked_at=_datetime(row["last_checked_at"]),
        )

    @staticmethod
    def _source(row: dict[str, Any]) -> SourceMessage:
        sent_at = _datetime(row["sent_at"])
        if sent_at is None:
            raise ValueError("source message has no sent_at")
        return SourceMessage(
            id=_as_uuid(row["id"]),
            tenant_key=str(row["tenant_key"]),
            message_id=str(row["message_id"]),
            chat_id=str(row["chat_id"]),
            chat_name=str(row["chat_name"]),
            sender_id=str(row["sender_id"]),
            sender_name=str(row["sender_name"]),
            message_type=str(row["message_type"]),
            content=str(row["content"]),
            sent_at=sent_at,
            root_id=str(row["root_id"]),
            parent_id=str(row["parent_id"]),
            source_state=SourceState(str(row["source_state"])),
            recalled_at=_datetime(row["recalled_at"]),
        )

    @staticmethod
    def _item(row: dict[str, Any]) -> InboxItem:
        created_at = _datetime(row["created_at"])
        updated_at = _datetime(row["updated_at"])
        if created_at is None or updated_at is None:
            raise ValueError("inbox item timestamps are missing")
        return InboxItem(
            id=_as_uuid(row["id"]),
            source_message_id=_as_uuid(row["source_message_id"]),
            target_user_id=_as_uuid(row["target_user_id"]),
            mention_type=MentionType(str(row["mention_type"])),
            status=InboxStatus(str(row["status"])),
            note=str(row["note"]),
            handled_at=_datetime(row["handled_at"]),
            version=int(row["version"]),
            created_at=created_at,
            updated_at=updated_at,
        )

    async def enqueue_event(self, event_key: str, event_type: str, payload: dict[str, Any]) -> bool:
        async with self._transaction() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO incoming_events(id, event_key, event_type, payload)
                VALUES (?, ?, ?, ?) ON CONFLICT(event_key) DO NOTHING
                """,
                (str(uuid4()), event_key, event_type, json.dumps(payload, ensure_ascii=False)),
            )
            return cursor.rowcount == 1

    async def claim_events(self, limit: int = 20) -> list[ClaimedJob]:
        now = datetime.now(UTC)
        stale = now - timedelta(minutes=5)
        async with self._transaction() as conn:
            cursor = await conn.execute(
                """
                SELECT id, event_type, payload, attempts FROM incoming_events
                WHERE (status IN ('pending', 'failed') AND available_at <= ?)
                   OR (status='processing' AND updated_at < ?)
                ORDER BY created_at LIMIT ?
                """,
                (_db_time(now), _db_time(stale), limit),
            )
            rows = _rows(await cursor.fetchall())
            if rows:
                await conn.executemany(
                    """
                    UPDATE incoming_events SET status='processing', attempts=attempts+1,
                        updated_at=? WHERE id=?
                    """,
                    [(_db_time(now), str(row["id"])) for row in rows],
                )
        return [
            ClaimedJob(
                id=_as_uuid(row["id"]),
                event_type=str(row["event_type"]),
                payload=json.loads(str(row["payload"])),
                attempts=int(row["attempts"]) + 1,
            )
            for row in rows
        ]

    async def finish_event(self, job_id: UUID) -> None:
        now = _db_time()
        async with self._transaction() as conn:
            await conn.execute(
                """
                UPDATE incoming_events SET status='succeeded', payload='{}',
                    last_error='', updated_at=? WHERE id=?
                """,
                (now, str(job_id)),
            )

    async def retry_event(self, job_id: UUID, error: str, attempts: int) -> None:
        delay = min(300, 2 ** min(attempts, 8))
        now = datetime.now(UTC)
        async with self._transaction() as conn:
            await conn.execute(
                """
                UPDATE incoming_events SET status='failed', last_error=?, available_at=?,
                    updated_at=? WHERE id=?
                """,
                (
                    error[:1000],
                    _db_time(now + timedelta(seconds=delay)),
                    _db_time(now),
                    str(job_id),
                ),
            )

    async def find_active_users(
        self, tenant_key: str, user_ids: set[str], open_ids: set[str]
    ) -> list[User]:
        if not user_ids and not open_ids:
            return []
        clauses: list[str] = []
        params: list[Any] = [tenant_key]
        if user_ids:
            clauses.append(f"user_id IN ({','.join('?' for _ in user_ids)})")
            params.extend(sorted(user_ids))
        if open_ids:
            clauses.append(f"open_id IN ({','.join('?' for _ in open_ids)})")
            params.extend(sorted(open_ids))
        rows = await self._read_all(
            f"""
            SELECT * FROM users WHERE tenant_key=? AND enabled=1 AND authorized=1
                AND departed=0 AND ({" OR ".join(clauses)})
            """,
            params,
        )
        return [self._user(row) for row in rows]

    async def find_at_all_users(
        self,
        tenant_key: str,
        chat_id: str,
        member_user_ids: set[str] | None = None,
    ) -> list[User]:
        if member_user_ids is not None and not member_user_ids:
            return []
        params: list[Any] = [tenant_key, chat_id]
        member_clause = ""
        if member_user_ids is not None:
            member_clause = f" AND u.user_id IN ({','.join('?' for _ in member_user_ids)})"
            params.extend(sorted(member_user_ids))
        rows = await self._read_all(
            f"""
            SELECT u.* FROM users u
            JOIN user_chats uc ON uc.user_pk=u.id
            JOIN chats c ON c.id=uc.chat_pk
            WHERE u.tenant_key=? AND c.chat_id=? AND u.enabled=1 AND u.authorized=1
                AND u.departed=0 AND u.include_at_all=1 AND uc.active=1
                AND c.external=0 AND c.disbanded=0 {member_clause}
            """,
            params,
        )
        return [self._user(row) for row in rows]

    async def get_chat(self, tenant_key: str, chat_id: str) -> Chat | None:
        row = await self._read_one(
            "SELECT * FROM chats WHERE tenant_key=? AND chat_id=?", (tenant_key, chat_id)
        )
        return self._chat(row) if row else None

    async def upsert_chat(self, chat: Chat) -> Chat:
        now = _db_time()
        async with self._transaction() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO chats(
                    id, tenant_key, chat_id, name, external, bot_present,
                    disbanded, last_checked_at
                ) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(tenant_key, chat_id) DO UPDATE SET
                    name=CASE WHEN excluded.name='' THEN chats.name ELSE excluded.name END,
                    external=excluded.external,
                    bot_present=(chats.bot_present OR excluded.bot_present),
                    disbanded=excluded.disbanded,
                    last_checked_at=excluded.last_checked_at,
                    updated_at=?
                RETURNING *
                """,
                (
                    str(chat.id),
                    chat.tenant_key,
                    chat.chat_id,
                    chat.name,
                    int(chat.external),
                    int(chat.bot_present),
                    int(chat.disbanded),
                    _db_time(chat.last_checked_at) if chat.last_checked_at else None,
                    now,
                ),
            )
            row = _row(await cursor.fetchone())
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
        async with self._transaction() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO source_messages(
                    id, tenant_key, message_id, chat_id, chat_name, sender_id,
                    sender_name, message_type, content, sent_at, root_id, parent_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(tenant_key, message_id) DO UPDATE SET
                    chat_name=CASE WHEN source_messages.chat_name=''
                                   THEN excluded.chat_name ELSE source_messages.chat_name END
                RETURNING *
                """,
                (
                    str(uuid4()),
                    incoming.tenant_key,
                    incoming.message_id,
                    incoming.chat_id,
                    chat.name or incoming.chat_id,
                    incoming.sender_user_id or incoming.sender_open_id,
                    incoming.sender_name,
                    incoming.message_type,
                    normalized_content,
                    _db_time(incoming.create_time),
                    incoming.root_id,
                    incoming.parent_id,
                ),
            )
            source_row = _row(await cursor.fetchone())
            if not source_row:
                raise RuntimeError("failed to store source message")
            source = self._source(source_row)
            created: list[InboxItem] = []
            for user_pk, (_, mention_type) in targets.items():
                item_cursor = await conn.execute(
                    """
                    INSERT INTO inbox_items(id, source_message_id, target_user_id, mention_type)
                    VALUES (?,?,?,?)
                    ON CONFLICT(source_message_id, target_user_id) DO NOTHING
                    RETURNING *
                    """,
                    (str(uuid4()), str(source.id), str(user_pk), mention_type.value),
                )
                item_row = _row(await item_cursor.fetchone())
                if not item_row:
                    continue
                item = self._item(item_row)
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
        now = _db_time()
        async with self._transaction() as conn:
            source_cursor = await conn.execute(
                """
                UPDATE source_messages SET content='', source_state=?, recalled_at=?,
                    updated_at=? WHERE tenant_key=? AND message_id=? RETURNING id
                """,
                (
                    SourceState.RECALLED.value,
                    _db_time(recalled_at),
                    now,
                    tenant_key,
                    message_id,
                ),
            )
            source_row = _row(await source_cursor.fetchone())
            if not source_row:
                return []
            items_cursor = await conn.execute(
                """
                UPDATE inbox_items SET
                    status=CASE WHEN status IN ('已处理','忽略') THEN status ELSE '忽略' END,
                    handled_at=CASE WHEN status IN ('已处理','忽略')
                                    THEN handled_at ELSE ? END,
                    version=version+1, updated_at=?
                WHERE source_message_id=? RETURNING *
                """,
                (_db_time(recalled_at), now, str(source_row["id"])),
            )
            items = [self._item(row) for row in _rows(await items_cursor.fetchall())]
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
        row = await self._read_one(
            """
            SELECT
                i.id AS i_id, i.source_message_id, i.target_user_id, i.mention_type,
                i.status, i.note, i.handled_at, i.version,
                i.created_at AS i_created_at, i.updated_at AS i_updated_at,
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
            WHERE i.id=?
            """,
            (str(item_id),),
        )
        if not row:
            return None
        item_row = {
            "id": row["i_id"],
            "source_message_id": row["source_message_id"],
            "target_user_id": row["target_user_id"],
            "mention_type": row["mention_type"],
            "status": row["status"],
            "note": row["note"],
            "handled_at": row["handled_at"],
            "version": row["version"],
            "created_at": row["i_created_at"],
            "updated_at": row["i_updated_at"],
        }
        user_row = {
            "id": row["u_id"],
            "tenant_key": row["u_tenant_key"],
            "user_id": row["u_user_id"],
            "open_id": row["u_open_id"],
            "name": row["u_name"],
            "enabled": row["u_enabled"],
            "authorized": row["u_authorized"],
            "include_at_all": row["u_include_at_all"],
            "departed": row["u_departed"],
            "access_token_encrypted": row["access_token_encrypted"],
            "refresh_token_encrypted": row["refresh_token_encrypted"],
            "access_token_expires_at": row["access_token_expires_at"],
            "refresh_token_expires_at": row["refresh_token_expires_at"],
            "last_coverage_check_at": row["last_coverage_check_at"],
        }
        return self._item(item_row), self._source(row), self._user(user_row)

    async def update_inbox_item(
        self,
        item_id: UUID,
        status: InboxStatus,
        note: str,
        expected_version: int | None = None,
        changed_at: datetime | None = None,
    ) -> InboxItem | None:
        async with self._transaction() as conn:
            cursor = await conn.execute("SELECT * FROM inbox_items WHERE id=?", (str(item_id),))
            current_row = _row(await cursor.fetchone())
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
            updated_at = max(datetime.now(UTC), changed_at or datetime.now(UTC))
            handled_at = current.handled_at
            if status in {InboxStatus.DONE, InboxStatus.IGNORED}:
                handled_at = handled_at or datetime.now(UTC)
            else:
                handled_at = None
            update_cursor = await conn.execute(
                """
                UPDATE inbox_items SET status=?, note=?, handled_at=?, version=version+1,
                    updated_at=? WHERE id=? RETURNING *
                """,
                (
                    status.value,
                    note,
                    _db_time(handled_at) if handled_at else None,
                    _db_time(updated_at),
                    str(item_id),
                ),
            )
            row = _row(await update_cursor.fetchone())
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
        async with self._transaction() as conn:
            cursor = await conn.execute(
                "SELECT * FROM users WHERE tenant_key=? AND user_id=?",
                (user.tenant_key, user.user_id),
            )
            existing_row = _row(await cursor.fetchone())
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
            now = _db_time()
            update_cursor = await conn.execute(
                """
                INSERT INTO users(id, tenant_key, user_id, open_id, name, enabled, departed)
                VALUES (?,?,?,?,?,1,0)
                ON CONFLICT(tenant_key, user_id) DO UPDATE SET
                    enabled=1, departed=0,
                    open_id=CASE WHEN excluded.open_id='' THEN users.open_id
                                 ELSE excluded.open_id END,
                    name=CASE WHEN excluded.name='' THEN users.name ELSE excluded.name END,
                    updated_at=?
                RETURNING *
                """,
                (
                    str(user.id),
                    user.tenant_key,
                    user.user_id,
                    user.open_id,
                    user.name,
                    now,
                ),
            )
            row = _row(await update_cursor.fetchone())
            if not row:
                raise RuntimeError("failed to enable user")
            stored = self._user(row)
            await self._bind_user_mapping_conn(conn, stored.id, bitable_record_id)
            await self._enqueue_user_projections(conn, stored)
        return stored

    async def disable_user(
        self, tenant_key: str, user_id: str, bitable_record_id: str = ""
    ) -> User | None:
        async with self._transaction() as conn:
            cursor = await conn.execute(
                "SELECT * FROM users WHERE tenant_key=? AND user_id=?",
                (tenant_key, user_id),
            )
            current_row = _row(await cursor.fetchone())
            if not current_row:
                return None
            current = self._user(current_row)
            await self._bind_user_mapping_conn(conn, current.id, bitable_record_id)
            if not current.enabled and not current.authorized:
                return current
            update_cursor = await conn.execute(
                """
                UPDATE users SET enabled=0, authorized=0, access_token_encrypted='',
                    refresh_token_encrypted='', updated_at=?
                WHERE tenant_key=? AND user_id=? RETURNING *
                """,
                (_db_time(), tenant_key, user_id),
            )
            row = _row(await update_cursor.fetchone())
            if not row:
                return None
            stored = self._user(row)
            await self._enqueue_user_projections(conn, stored)
        return stored

    async def _bind_user_mapping_conn(
        self, conn: aiosqlite.Connection, user_pk: UUID, record_id: str
    ) -> None:
        if not record_id:
            return
        cursor = await conn.execute(
            """
            SELECT entity_id, record_id FROM bitable_mappings
            WHERE (entity_type='user' AND entity_id=? AND table_key='users')
               OR (table_key='users' AND record_id=?)
            """,
            (str(user_pk), record_id),
        )
        for row in _rows(await cursor.fetchall()):
            if _as_uuid(row["entity_id"]) != user_pk or str(row["record_id"]) != record_id:
                raise ValueError("Bitable user record mapping conflicts with another row")
        await conn.execute(
            """
            INSERT INTO bitable_mappings(
                entity_type, entity_id, table_key, record_id, synced_version
            ) VALUES ('user',?,'users',?,0)
            ON CONFLICT(entity_type, entity_id, table_key) DO NOTHING
            """,
            (str(user_pk), record_id),
        )

    async def get_enabled_user(self, tenant_key: str, user_id: str) -> User | None:
        row = await self._read_one(
            "SELECT * FROM users WHERE tenant_key=? AND user_id=? AND enabled=1",
            (tenant_key, user_id),
        )
        return self._user_with_tokens(row) if row else None

    async def activate_user(self, info: OAuthUserInfo, tokens: OAuthTokens) -> User | None:
        now = datetime.now(UTC)
        async with self._transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE users SET open_id=?, name=CASE WHEN ?='' THEN name ELSE ? END,
                    authorized=1, departed=0, access_token_encrypted=?,
                    refresh_token_encrypted=?, access_token_expires_at=?,
                    refresh_token_expires_at=?, updated_at=?
                WHERE tenant_key=? AND user_id=? AND enabled=1 RETURNING *
                """,
                (
                    info.open_id,
                    info.name,
                    info.name,
                    self._token_cipher.encrypt(tokens.access_token),
                    self._token_cipher.encrypt(tokens.refresh_token),
                    _db_time(now + timedelta(seconds=tokens.expires_in)),
                    _db_time(now + timedelta(seconds=tokens.refresh_expires_in)),
                    _db_time(now),
                    info.tenant_key,
                    info.user_id,
                ),
            )
            row = _row(await cursor.fetchone())
            if not row:
                return None
            user = self._user_with_tokens(row)
            await self._enqueue_user_projections(conn, user)
        return user

    async def update_user_tokens(self, user: User, tokens: OAuthTokens) -> User:
        now = datetime.now(UTC)
        async with self._transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE users SET access_token_encrypted=?, refresh_token_encrypted=?,
                    access_token_expires_at=?, refresh_token_expires_at=?, authorized=1,
                    updated_at=? WHERE id=? RETURNING *
                """,
                (
                    self._token_cipher.encrypt(tokens.access_token),
                    self._token_cipher.encrypt(tokens.refresh_token),
                    _db_time(now + timedelta(seconds=tokens.expires_in)),
                    _db_time(now + timedelta(seconds=tokens.refresh_expires_in)),
                    _db_time(now),
                    str(user.id),
                ),
            )
            row = _row(await cursor.fetchone())
        if not row:
            raise RuntimeError("failed to update OAuth tokens")
        return self._user_with_tokens(row)

    async def mark_user_auth_expired(self, user_id: UUID) -> None:
        now = _db_time()
        projection_version = int(datetime.now(UTC).timestamp() * 1_000_000)
        async with self._transaction() as conn:
            user_cursor = await conn.execute(
                """
                UPDATE users SET authorized=0, access_token_encrypted='',
                    refresh_token_encrypted='', updated_at=? WHERE id=? RETURNING *
                """,
                (now, str(user_id)),
            )
            coverage_cursor = await conn.execute(
                """
                UPDATE user_chats SET coverage_status=? WHERE user_pk=? AND active=1
                RETURNING id
                """,
                (CoverageStatus.AUTH_EXPIRED.value, str(user_id)),
            )
            for coverage_row in _rows(await coverage_cursor.fetchall()):
                await self._enqueue_outbox_conn(
                    conn,
                    entity_type="coverage",
                    entity_id=_as_uuid(coverage_row["id"]),
                    table_key="coverage",
                    operation="upsert",
                    version=projection_version,
                )
            user_row = _row(await user_cursor.fetchone())
            if user_row:
                await self._enqueue_user_projections(
                    conn, self._user(user_row), version=projection_version
                )

    async def list_active_users(self) -> list[User]:
        rows = await self._read_all(
            """
            SELECT * FROM users WHERE enabled=1 AND authorized=1 AND departed=0
            ORDER BY name
            """
        )
        return [self._user_with_tokens(row) for row in rows]

    async def list_users(self) -> list[User]:
        return [
            self._user(row)
            for row in await self._read_all("SELECT * FROM users ORDER BY name, user_id")
        ]

    async def update_user_setting(
        self, tenant_key: str, user_id: str, include_at_all: bool
    ) -> User | None:
        async with self._transaction() as conn:
            cursor = await conn.execute(
                "SELECT * FROM users WHERE tenant_key=? AND user_id=? AND enabled=1",
                (tenant_key, user_id),
            )
            current_row = _row(await cursor.fetchone())
            if not current_row:
                return None
            current = self._user(current_row)
            if current.include_at_all == include_at_all:
                return current
            update_cursor = await conn.execute(
                """
                UPDATE users SET include_at_all=?, updated_at=?
                WHERE tenant_key=? AND user_id=? AND enabled=1 RETURNING *
                """,
                (int(include_at_all), _db_time(), tenant_key, user_id),
            )
            row = _row(await update_cursor.fetchone())
            if not row:
                return None
            stored = self._user(row)
            await self._enqueue_user_projections(
                conn, stored, version=int(datetime.now(UTC).timestamp() * 1_000_000)
            )
        return stored

    async def replace_user_chats(self, user: User, memberships: list[ChatMembership]) -> list[Chat]:
        now = datetime.now(UTC)
        projection_version = int(now.timestamp() * 1_000_000)
        async with self._transaction() as conn:
            stale_cursor = await conn.execute(
                """
                UPDATE user_chats SET active=0, coverage_status=?
                WHERE user_pk=? AND active=1 RETURNING id
                """,
                (CoverageStatus.UNSUPPORTED.value, str(user.id)),
            )
            for stale in _rows(await stale_cursor.fetchall()):
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
                chat_cursor = await conn.execute(
                    """
                    INSERT INTO chats(id, tenant_key, chat_id, name, external, last_checked_at)
                    VALUES (?,?,?,?,?,?)
                    ON CONFLICT(tenant_key, chat_id) DO UPDATE SET
                        name=excluded.name, external=excluded.external,
                        last_checked_at=excluded.last_checked_at, updated_at=?
                    RETURNING *
                    """,
                    (
                        str(uuid4()),
                        user.tenant_key,
                        membership.chat_id,
                        membership.name,
                        int(membership.external),
                        _db_time(now),
                        _db_time(now),
                    ),
                )
                chat_row = _row(await chat_cursor.fetchone())
                if not chat_row:
                    continue
                chat = self._chat(chat_row)
                chats.append(chat)
                coverage_cursor = await conn.execute(
                    """
                    INSERT INTO user_chats(
                        id, user_pk, chat_pk, coverage_status, active, last_seen_at
                    ) VALUES (?,?,?,?,1,?)
                    ON CONFLICT(user_pk, chat_pk) DO UPDATE SET
                        coverage_status=excluded.coverage_status, active=1,
                        last_seen_at=excluded.last_seen_at
                    RETURNING id
                    """,
                    (
                        str(uuid4()),
                        str(user.id),
                        str(chat.id),
                        chat.coverage_status.value,
                        _db_time(now),
                    ),
                )
                coverage_row = _row(await coverage_cursor.fetchone())
                if not coverage_row:
                    raise RuntimeError("failed to upsert user chat coverage")
                await self._enqueue_outbox_conn(
                    conn,
                    entity_type="coverage",
                    entity_id=_as_uuid(coverage_row["id"]),
                    table_key="coverage",
                    operation="upsert",
                    version=projection_version,
                )
            await conn.execute(
                "UPDATE users SET last_coverage_check_at=?, updated_at=? WHERE id=?",
                (_db_time(now), _db_time(now), str(user.id)),
            )
            await self._enqueue_user_projections(conn, user, version=projection_version)
        return chats

    async def set_bot_chats(self, tenant_key: str, chat_ids: set[str]) -> None:
        now = _db_time()
        async with self._transaction() as conn:
            await conn.execute(
                "UPDATE chats SET bot_present=0, updated_at=? WHERE tenant_key=?",
                (now, tenant_key),
            )
            if chat_ids:
                placeholders = ",".join("?" for _ in chat_ids)
                await conn.execute(
                    f"""
                    UPDATE chats SET bot_present=1, disbanded=0, unsupported=0,
                        unsupported_reason='', updated_at=?
                    WHERE tenant_key=? AND chat_id IN ({placeholders})
                    """,
                    [now, tenant_key, *sorted(chat_ids)],
                )
            coverage_cursor = await conn.execute(
                """
                SELECT uc.id, c.external, c.disbanded, c.unsupported, c.bot_present
                FROM user_chats uc JOIN chats c ON c.id=uc.chat_pk
                WHERE c.tenant_key=?
                """,
                (tenant_key,),
            )
            for row in _rows(await coverage_cursor.fetchall()):
                if row["external"] or row["disbanded"] or row["unsupported"]:
                    coverage = CoverageStatus.UNSUPPORTED
                elif row["bot_present"]:
                    coverage = CoverageStatus.COVERED
                else:
                    coverage = CoverageStatus.BOT_MISSING
                await conn.execute(
                    "UPDATE user_chats SET coverage_status=? WHERE id=?",
                    (coverage.value, str(row["id"])),
                )

    async def set_bot_membership(
        self, tenant_key: str, chat_id: str, present: bool, name: str = ""
    ) -> Chat:
        now = _db_time()
        async with self._transaction() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO chats(id, tenant_key, chat_id, name, bot_present)
                VALUES (?,?,?,?,?)
                ON CONFLICT(tenant_key, chat_id) DO UPDATE SET
                    name=CASE WHEN excluded.name='' THEN chats.name ELSE excluded.name END,
                    bot_present=excluded.bot_present, disbanded=0,
                    unsupported=CASE WHEN excluded.bot_present=1 THEN 0 ELSE chats.unsupported END,
                    unsupported_reason=CASE WHEN excluded.bot_present=1
                                            THEN '' ELSE chats.unsupported_reason END,
                    updated_at=?
                RETURNING *
                """,
                (str(uuid4()), tenant_key, chat_id, name or chat_id, int(present), now),
            )
            row = _row(await cursor.fetchone())
            if not row:
                raise RuntimeError("failed to update bot membership")
            chat = self._chat(row)
            coverage_cursor = await conn.execute(
                """
                UPDATE user_chats SET coverage_status=? WHERE chat_pk=?
                RETURNING id, user_pk
                """,
                (chat.coverage_status.value, str(chat.id)),
            )
            await self._enqueue_coverage_and_users(
                conn,
                _rows(await coverage_cursor.fetchall()),
                int(datetime.now(UTC).timestamp() * 1_000_000),
            )
        return chat

    async def disband_chat(self, tenant_key: str, chat_id: str) -> None:
        async with self._transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE chats SET disbanded=1, bot_present=0, updated_at=?
                WHERE tenant_key=? AND chat_id=? RETURNING id
                """,
                (_db_time(), tenant_key, chat_id),
            )
            row = _row(await cursor.fetchone())
            if row:
                coverage_cursor = await conn.execute(
                    """
                    UPDATE user_chats SET coverage_status=? WHERE chat_pk=?
                    RETURNING id, user_pk
                    """,
                    (CoverageStatus.UNSUPPORTED.value, str(row["id"])),
                )
                await self._enqueue_coverage_and_users(
                    conn,
                    _rows(await coverage_cursor.fetchall()),
                    int(datetime.now(UTC).timestamp() * 1_000_000),
                )

    async def set_chat_unsupported(
        self, tenant_key: str, chat_id: str, unsupported: bool, reason: str
    ) -> Chat | None:
        async with self._transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE chats SET unsupported=?, unsupported_reason=?, updated_at=?
                WHERE tenant_key=? AND chat_id=? RETURNING *
                """,
                (
                    int(unsupported),
                    reason[:500] if unsupported else "",
                    _db_time(),
                    tenant_key,
                    chat_id,
                ),
            )
            row = _row(await cursor.fetchone())
            if not row:
                return None
            chat = self._chat(row)
            coverage_cursor = await conn.execute(
                """
                UPDATE user_chats SET coverage_status=? WHERE chat_pk=? AND active=1
                RETURNING id, user_pk
                """,
                (chat.coverage_status.value, str(chat.id)),
            )
            await self._enqueue_coverage_and_users(
                conn,
                _rows(await coverage_cursor.fetchall()),
                int(datetime.now(UTC).timestamp() * 1_000_000),
            )
        return chat

    async def purge_expired_content(self, before: datetime) -> int:
        now = _db_time()
        async with self._transaction() as conn:
            await conn.execute(
                """
                UPDATE incoming_events SET payload='{}', status='succeeded',
                    last_error='discarded by content retention policy', updated_at=?
                WHERE created_at < ? AND status <> 'processing' AND payload <> '{}'
                """,
                (now, _db_time(before)),
            )
            source_cursor = await conn.execute(
                """
                UPDATE source_messages SET content='', updated_at=?
                WHERE sent_at < ? AND content <> '' RETURNING id
                """,
                (now, _db_time(before)),
            )
            source_rows = _rows(await source_cursor.fetchall())
            for source_row in source_rows:
                item_cursor = await conn.execute(
                    """
                    UPDATE inbox_items SET version=version+1, updated_at=?
                    WHERE source_message_id=? RETURNING id, version
                    """,
                    (now, str(source_row["id"])),
                )
                for item_row in _rows(await item_cursor.fetchall()):
                    await self._enqueue_outbox_conn(
                        conn,
                        entity_type="inbox",
                        entity_id=_as_uuid(item_row["id"]),
                        table_key="inbox",
                        operation="upsert",
                        version=int(item_row["version"]),
                    )
            return len(source_rows)

    async def _enqueue_user_projections(
        self,
        conn: aiosqlite.Connection,
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
        conn: aiosqlite.Connection,
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
        placeholders = ",".join("?" for _ in user_ids)
        cursor = await conn.execute(
            f"SELECT * FROM users WHERE id IN ({placeholders})",
            [str(user_id) for user_id in user_ids],
        )
        for row in _rows(await cursor.fetchall()):
            await self._enqueue_user_projections(conn, self._user(row), version=projection_version)

    async def _enqueue_outbox_conn(
        self,
        conn: aiosqlite.Connection,
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
            ) VALUES (?,?,?,?,?,?, '{}')
            ON CONFLICT(idempotency_key) DO NOTHING
            """,
            (str(uuid4()), key, entity_type, str(entity_id), table_key, operation),
        )

    async def claim_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        stale = now - timedelta(minutes=5)
        async with self._transaction() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM outbox_jobs
                WHERE (status IN ('pending','failed') AND available_at <= ?)
                   OR (status='processing' AND updated_at < ?)
                ORDER BY created_at LIMIT ?
                """,
                (_db_time(now), _db_time(stale), limit),
            )
            rows = _rows(await cursor.fetchall())
            if rows:
                await conn.executemany(
                    """
                    UPDATE outbox_jobs SET status='processing', attempts=attempts+1,
                        updated_at=? WHERE id=?
                    """,
                    [(_db_time(now), str(row["id"])) for row in rows],
                )
        for row in rows:
            row["attempts"] = int(row["attempts"]) + 1
        return rows

    async def get_mapping(
        self, entity_type: str, entity_id: UUID, table_key: str
    ) -> dict[str, Any] | None:
        return await self._read_one(
            """
            SELECT * FROM bitable_mappings
            WHERE entity_type=? AND entity_id=? AND table_key=?
            """,
            (entity_type, str(entity_id), table_key),
        )

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
        now = _db_time()
        async with self._transaction() as conn:
            await conn.execute(
                """
                INSERT INTO bitable_mappings(
                    entity_type, entity_id, table_key, record_id, synced_version
                ) VALUES (?,?,?,?,?)
                ON CONFLICT(entity_type, entity_id, table_key) DO UPDATE SET
                    record_id=excluded.record_id,
                    synced_version=MAX(bitable_mappings.synced_version, excluded.synced_version),
                    updated_at=?
                """,
                (entity_type, str(entity_id), table_key, record_id, synced_version, now),
            )
            await conn.execute(
                "UPDATE outbox_jobs SET status='succeeded', updated_at=? WHERE id=?",
                (now, str(job_id)),
            )

    async def retry_outbox(self, job_id: UUID, error: str, attempts: int) -> None:
        delay = min(600, 2 ** min(attempts, 9))
        now = datetime.now(UTC)
        async with self._transaction() as conn:
            await conn.execute(
                """
                UPDATE outbox_jobs SET status='failed', last_error=?, available_at=?,
                    updated_at=? WHERE id=?
                """,
                (
                    error[:1000],
                    _db_time(now + timedelta(seconds=delay)),
                    _db_time(now),
                    str(job_id),
                ),
            )

    async def finish_superseded_outbox(self, job_ids: list[UUID]) -> None:
        if not job_ids:
            return
        now = _db_time()
        async with self._transaction() as conn:
            await conn.executemany(
                """
                UPDATE outbox_jobs SET status='succeeded',
                    last_error='superseded by a newer projection', updated_at=? WHERE id=?
                """,
                [(now, str(job_id)) for job_id in job_ids],
            )

    async def get_user_by_pk(self, user_pk: UUID) -> User | None:
        row = await self._read_one("SELECT * FROM users WHERE id=?", (str(user_pk),))
        return self._user(row) if row else None

    async def get_coverage_context(
        self, coverage_id: UUID
    ) -> tuple[User, Chat, CoverageStatus, datetime] | None:
        row = await self._read_one(
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
            WHERE uc.id=?
            """,
            (str(coverage_id),),
        )
        if not row:
            return None
        user = self._user(
            {
                "id": row["u_id"],
                "tenant_key": row["u_tenant_key"],
                "user_id": row["u_user_id"],
                "open_id": row["u_open_id"],
                "name": row["u_name"],
                "enabled": row["u_enabled"],
                "authorized": row["u_authorized"],
                "include_at_all": row["u_include_at_all"],
                "departed": row["u_departed"],
                "access_token_encrypted": row["access_token_encrypted"],
                "refresh_token_encrypted": row["refresh_token_encrypted"],
                "access_token_expires_at": row["access_token_expires_at"],
                "refresh_token_expires_at": row["refresh_token_expires_at"],
                "last_coverage_check_at": row["last_coverage_check_at"],
            }
        )
        chat = self._chat(
            {
                "id": row["c_id"],
                "tenant_key": row["c_tenant_key"],
                "chat_id": row["c_chat_id"],
                "name": row["c_name"],
                "external": row["c_external"],
                "bot_present": row["c_bot_present"],
                "disbanded": row["c_disbanded"],
                "unsupported": row["c_unsupported"],
                "unsupported_reason": row["c_unsupported_reason"],
                "last_checked_at": row["c_last_checked_at"],
            }
        )
        last_seen_at = _datetime(row["last_seen_at"])
        if last_seen_at is None:
            raise ValueError("coverage record has no last_seen_at")
        return user, chat, CoverageStatus(str(row["coverage_status"])), last_seen_at

    async def count_user_coverage(self, user_pk: UUID) -> tuple[int, int]:
        row = await self._read_one(
            """
            SELECT COUNT(*) AS total,
                SUM(CASE WHEN coverage_status=? THEN 1 ELSE 0 END) AS covered
            FROM user_chats uc JOIN chats c ON c.id=uc.chat_pk
            WHERE uc.user_pk=? AND uc.active=1 AND c.external=0
                AND c.disbanded=0 AND c.unsupported=0
            """,
            (CoverageStatus.COVERED.value, str(user_pk)),
        )
        if not row:
            return 0, 0
        return int(row["covered"] or 0), int(row["total"] or 0)

    async def reconcile_outbox(self) -> int:
        async with self._transaction() as conn:
            cursor = await conn.execute(
                """
                SELECT i.id, i.version FROM inbox_items i
                LEFT JOIN bitable_mappings m
                  ON m.entity_type='inbox' AND m.entity_id=i.id AND m.table_key='inbox'
                WHERE m.entity_id IS NULL OR m.synced_version < i.version LIMIT 1000
                """
            )
            rows = _rows(await cursor.fetchall())
            for row in rows:
                await self._enqueue_outbox_conn(
                    conn,
                    entity_type="inbox",
                    entity_id=_as_uuid(row["id"]),
                    table_key="inbox",
                    operation="upsert",
                    version=int(row["version"]),
                )
        return len(rows)

    async def inbox_reconciliation_state(self) -> dict[str, dict[str, Any]]:
        rows = await self._read_all(
            """
            SELECT m.record_id, i.id, i.status, i.note, i.version
            FROM bitable_mappings m JOIN inbox_items i ON i.id=m.entity_id
            WHERE m.entity_type='inbox' AND m.table_key='inbox'
            """
        )
        return {str(row["record_id"]): row for row in rows}

    async def health(self) -> bool:
        try:
            row = await self._read_one("SELECT 1 AS ok")
        except (aiosqlite.Error, RuntimeError):
            return False
        return bool(row and row["ok"] == 1)

    async def coverage_summary(self) -> list[dict[str, Any]]:
        rows = await self._read_all(
            """
            SELECT u.user_id, u.name, u.authorized,
                SUM(CASE WHEN c.id IS NOT NULL AND c.external=0 AND c.disbanded=0
                          AND c.unsupported=0 THEN 1 ELSE 0 END) AS target_groups,
                SUM(CASE WHEN uc.coverage_status=? AND c.external=0 AND c.disbanded=0
                          AND c.unsupported=0 THEN 1 ELSE 0 END) AS covered_groups,
                SUM(CASE WHEN uc.coverage_status=? AND c.external=0 AND c.disbanded=0
                          AND c.unsupported=0 THEN 1 ELSE 0 END) AS missing_groups,
                u.last_coverage_check_at
            FROM users u
            LEFT JOIN user_chats uc ON uc.user_pk=u.id AND uc.active=1
            LEFT JOIN chats c ON c.id=uc.chat_pk
            WHERE u.enabled=1 GROUP BY u.id ORDER BY u.name, u.user_id
            """,
            (CoverageStatus.COVERED.value, CoverageStatus.BOT_MISSING.value),
        )
        for row in rows:
            row["authorized"] = bool(row["authorized"])
            row["target_groups"] = int(row["target_groups"] or 0)
            row["covered_groups"] = int(row["covered_groups"] or 0)
            row["missing_groups"] = int(row["missing_groups"] or 0)
            row["last_coverage_check_at"] = _datetime(row["last_coverage_check_at"])
        return rows
