-- 0002_base_tables.sql  租户 / 工单 / 消息 / 运行
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','SUSPENDED','CLOSED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS aftersale_tickets (
    ticket_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
    user_id TEXT NOT NULL,
    order_id TEXT NULL,                         -- 允许 NULL：NEED_INFO 工单（§19.1）
    event_sequence BIGINT NOT NULL DEFAULT 0,   -- 单工单有序游标（§18.6）
    intent TEXT NOT NULL DEFAULT 'UNKNOWN',
    status TEXT NOT NULL DEFAULT 'NEW' CHECK (status IN
        ('NEW','NEED_INFO','POLICY_REVIEW','PENDING_APPROVAL','PROCESSING',
         'PENDING_RECONCILE','COMPLETED','REJECTED','MANUAL','CLOSED')),
    active_run_id UUID,
    version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, ticket_id)
);
CREATE INDEX IF NOT EXISTS idx_tickets_owner ON aftersale_tickets (tenant_id, user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tickets_order ON aftersale_tickets (tenant_id, order_id);

CREATE TABLE IF NOT EXISTS ticket_messages (
    message_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    ticket_id UUID NOT NULL REFERENCES aftersale_tickets(ticket_id),
    thread_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user','assistant','system','tool','reviewer')),
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    client_message_id TEXT,
    idempotency_key TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_messages_ticket ON ticket_messages (tenant_id, ticket_id, created_at);

CREATE TABLE IF NOT EXISTS ticket_runs (
    run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    ticket_id UUID NOT NULL REFERENCES aftersale_tickets(ticket_id),
    thread_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'RUNNING' CHECK (status IN
        ('QUEUED','RUNNING','WAITING_USER','WAITING_APPROVAL','WAITING_EXTERNAL','COMPLETED','FAILED','CANCELLED')),
    version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_runs_active ON ticket_runs (tenant_id, status) WHERE status IN ('QUEUED','RUNNING','WAITING_EXTERNAL');