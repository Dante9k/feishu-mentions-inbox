PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    tenant_key TEXT NOT NULL,
    user_id TEXT NOT NULL,
    open_id TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    authorized INTEGER NOT NULL DEFAULT 0 CHECK (authorized IN (0, 1)),
    include_at_all INTEGER NOT NULL DEFAULT 0 CHECK (include_at_all IN (0, 1)),
    departed INTEGER NOT NULL DEFAULT 0 CHECK (departed IN (0, 1)),
    access_token_encrypted TEXT NOT NULL DEFAULT '',
    refresh_token_encrypted TEXT NOT NULL DEFAULT '',
    access_token_expires_at TEXT,
    refresh_token_expires_at TEXT,
    last_coverage_check_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (tenant_key, user_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS users_open_id_uq
    ON users (tenant_key, open_id) WHERE open_id <> '';
CREATE INDEX IF NOT EXISTS users_active_idx
    ON users (tenant_key, enabled, authorized, departed);

CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY,
    tenant_key TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    external INTEGER NOT NULL DEFAULT 0 CHECK (external IN (0, 1)),
    bot_present INTEGER NOT NULL DEFAULT 0 CHECK (bot_present IN (0, 1)),
    disbanded INTEGER NOT NULL DEFAULT 0 CHECK (disbanded IN (0, 1)),
    unsupported INTEGER NOT NULL DEFAULT 0 CHECK (unsupported IN (0, 1)),
    unsupported_reason TEXT NOT NULL DEFAULT '',
    last_checked_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (tenant_key, chat_id)
);

CREATE TABLE IF NOT EXISTS user_chats (
    id TEXT NOT NULL UNIQUE,
    user_pk TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    chat_pk TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    coverage_status TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    last_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (user_pk, chat_pk)
);
CREATE INDEX IF NOT EXISTS user_chats_chat_idx ON user_chats (chat_pk, coverage_status);

CREATE TABLE IF NOT EXISTS source_messages (
    id TEXT PRIMARY KEY,
    tenant_key TEXT NOT NULL,
    message_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    chat_name TEXT NOT NULL DEFAULT '',
    sender_id TEXT NOT NULL DEFAULT '',
    sender_name TEXT NOT NULL DEFAULT '',
    message_type TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    sent_at TEXT NOT NULL,
    root_id TEXT NOT NULL DEFAULT '',
    parent_id TEXT NOT NULL DEFAULT '',
    source_state TEXT NOT NULL DEFAULT '有效',
    recalled_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (tenant_key, message_id)
);
CREATE INDEX IF NOT EXISTS source_messages_retention_idx
    ON source_messages (source_state, sent_at);

CREATE TABLE IF NOT EXISTS inbox_items (
    id TEXT PRIMARY KEY,
    source_message_id TEXT NOT NULL REFERENCES source_messages(id) ON DELETE CASCADE,
    target_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    mention_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '待处理',
    note TEXT NOT NULL DEFAULT '',
    handled_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (source_message_id, target_user_id)
);
CREATE INDEX IF NOT EXISTS inbox_items_user_status_idx
    ON inbox_items (target_user_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS bitable_mappings (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    table_key TEXT NOT NULL,
    record_id TEXT NOT NULL,
    synced_version INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (entity_type, entity_id, table_key),
    UNIQUE (table_key, record_id)
);

CREATE TABLE IF NOT EXISTS incoming_events (
    id TEXT PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS incoming_events_claim_idx
    ON incoming_events (status, available_at, created_at);

CREATE TABLE IF NOT EXISTS outbox_jobs (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    table_key TEXT NOT NULL,
    operation TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS outbox_jobs_claim_idx
    ON outbox_jobs (status, available_at, created_at);
