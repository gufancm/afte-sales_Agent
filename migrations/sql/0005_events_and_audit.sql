-- 0005_events_and_audit.sql  SSE 事件 / LLM 审计 / 审计 / Inbox / 对账 / 死信
CREATE TABLE IF NOT EXISTS ticket_events (
    event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    ticket_id UUID NOT NULL REFERENCES aftersale_tickets(ticket_id),
    ticket_sequence BIGINT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, ticket_id, ticket_sequence)
);
CREATE INDEX IF NOT EXISTS idx_ticket_events_cursor ON ticket_events (tenant_id, ticket_id, ticket_sequence);

CREATE TABLE IF NOT EXISTS llm_calls (
    call_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    ticket_id UUID,
    run_id UUID,
    trace_id TEXT,
    node TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL DEFAULT '',
    request_hash TEXT,
    response_hash TEXT,
    input_tokens INT,
    output_tokens INT,
    latency_ms INT,
    status TEXT NOT NULL CHECK (status IN ('SUCCESS','SCHEMA_ERROR','TIMEOUT','ERROR')),
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_ticket ON llm_calls (tenant_id, ticket_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_calls_node ON llm_calls (tenant_id, node, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_audit_events (
    event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    ticket_id UUID,
    trace_id TEXT,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_ticket ON agent_audit_events (tenant_id, ticket_id, created_at);

CREATE TABLE IF NOT EXISTS inbox_events (
    event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    source TEXT NOT NULL,
    dedup_key TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    processed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, source, dedup_key)
);

CREATE TABLE IF NOT EXISTS reconciliation_tasks (
    task_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    ticket_id UUID,
    refund_request_id UUID,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','RUNNING','DONE','MANUAL_RECONCILE','CANCELLED')),
    attempt_count INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 10,
    next_run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deadline TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_recon_due ON reconciliation_tasks (status, next_run_at) WHERE status IN ('PENDING','RUNNING');

CREATE TABLE IF NOT EXISTS dead_letter_events (
    event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    source TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);