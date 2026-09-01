-- Idempotent baseline schema applied when the repository starts.
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY,
    tenant_key TEXT NOT NULL,
    user_id TEXT NOT NULL,
    open_id TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    authorized BOOLEAN NOT NULL DEFAULT FALSE,
    include_at_all BOOLEAN NOT NULL DEFAULT FALSE,
    departed BOOLEAN NOT NULL DEFAULT FALSE,
    access_token_encrypted TEXT NOT NULL DEFAULT '',
    refresh_token_encrypted TEXT NOT NULL DEFAULT '',
    access_token_expires_at TIMESTAMPTZ,
    refresh_token_expires_at TIMESTAMPTZ,
    last_coverage_check_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_key, user_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS users_open_id_uq
    ON users (tenant_key, open_id) WHERE open_id <> '';
CREATE INDEX IF NOT EXISTS users_active_idx
    ON users (tenant_key, enabled, authorized, departed);

CREATE TABLE IF NOT EXISTS chats (
    id UUID PRIMARY KEY,
    tenant_key TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    external BOOLEAN NOT NULL DEFAULT FALSE,
    bot_present BOOLEAN NOT NULL DEFAULT FALSE,
    disbanded BOOLEAN NOT NULL DEFAULT FALSE,
    unsupported BOOLEAN NOT NULL DEFAULT FALSE,
    unsupported_reason TEXT NOT NULL DEFAULT '',
    last_checked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_key, chat_id)
);

CREATE TABLE IF NOT EXISTS user_chats (
    id UUID NOT NULL UNIQUE,
    user_pk UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    chat_pk UUID NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    coverage_status TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_pk, chat_pk)
);
CREATE INDEX IF NOT EXISTS user_chats_chat_idx ON user_chats (chat_pk, coverage_status);

CREATE TABLE IF NOT EXISTS source_messages (
    id UUID PRIMARY KEY,
    tenant_key TEXT NOT NULL,
    message_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    chat_name TEXT NOT NULL DEFAULT '',
    sender_id TEXT NOT NULL DEFAULT '',
    sender_name TEXT NOT NULL DEFAULT '',
    message_type TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    sent_at TIMESTAMPTZ NOT NULL,
    root_id TEXT NOT NULL DEFAULT '',
    parent_id TEXT NOT NULL DEFAULT '',
    source_state TEXT NOT NULL DEFAULT '有效',
    recalled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_key, message_id)
);
CREATE INDEX IF NOT EXISTS source_messages_retention_idx
    ON source_messages (source_state, sent_at);

CREATE TABLE IF NOT EXISTS inbox_items (
    id UUID PRIMARY KEY,
    source_message_id UUID NOT NULL REFERENCES source_messages(id) ON DELETE CASCADE,
    target_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    mention_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '待处理',
    note TEXT NOT NULL DEFAULT '',
    handled_at TIMESTAMPTZ,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_message_id, target_user_id)
);
CREATE INDEX IF NOT EXISTS inbox_items_user_status_idx
    ON inbox_items (target_user_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS bitable_mappings (
    entity_type TEXT NOT NULL,
    entity_id UUID NOT NULL,
    table_key TEXT NOT NULL,
    record_id TEXT NOT NULL,
    synced_version INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (entity_type, entity_id, table_key),
    UNIQUE (table_key, record_id)
);

CREATE TABLE IF NOT EXISTS incoming_events (
    id UUID PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS incoming_events_claim_idx
    ON incoming_events (status, available_at, created_at);

CREATE TABLE IF NOT EXISTS outbox_jobs (
    id UUID PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    entity_type TEXT NOT NULL,
    entity_id UUID NOT NULL,
    table_key TEXT NOT NULL,
    operation TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS outbox_jobs_claim_idx
    ON outbox_jobs (status, available_at, created_at);
