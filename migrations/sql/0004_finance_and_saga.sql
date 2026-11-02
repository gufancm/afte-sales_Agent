-- 0004_finance_and_saga.sql  计划/风险/审批 + 退款 + 命令/Outbox
CREATE TABLE IF NOT EXISTS action_plans (
    plan_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    ticket_id UUID NOT NULL REFERENCES aftersale_tickets(ticket_id),
    order_id TEXT,
    policy_decision_id UUID,
    plan_hash TEXT NOT NULL,
    plan_json JSONB NOT NULL,
    quote_id UUID,
    quote_hash TEXT,
    order_version TEXT,
    status TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT','VALIDATED','AUTHORIZED','REJECTED','EXECUTED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, plan_id)
);

CREATE TABLE IF NOT EXISTS risk_decisions (
    risk_decision_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    ticket_id UUID NOT NULL REFERENCES aftersale_tickets(ticket_id),
    plan_hash TEXT,
    quote_id UUID,
    decision TEXT NOT NULL CHECK (decision IN ('DENY','AUTO_APPROVE','REVIEW','MANUAL','NEED_INFO')),
    reason_codes TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS approval_tasks (
    approval_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    ticket_id UUID NOT NULL REFERENCES aftersale_tickets(ticket_id),
    plan_id UUID,
    plan_hash TEXT,
    quote_id UUID,
    amount_minor BIGINT,
    currency CHAR(3) DEFAULT 'CNY',
    policy_decision_id UUID,
    order_version TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','APPROVED','REJECTED','EXPIRED','CANCELLED')),
    reviewer_id TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, plan_hash)
);

CREATE TABLE IF NOT EXISTS refund_requests (
    refund_request_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    ticket_id UUID NOT NULL REFERENCES aftersale_tickets(ticket_id),
    order_id TEXT,
    approval_id UUID REFERENCES approval_tasks(approval_id),
    risk_decision_id UUID REFERENCES risk_decisions(risk_decision_id),
    policy_decision_id UUID,
    current_quote_id UUID,
    idempotency_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    amount_minor BIGINT NOT NULL DEFAULT 0 CHECK (amount_minor >= 0),
    currency CHAR(3) NOT NULL DEFAULT 'CNY',
    refund_method TEXT NOT NULL DEFAULT 'ORIGINAL_PAYMENT'
        CHECK (refund_method IN ('ORIGINAL_PAYMENT','STORE_CREDIT','BANK_TRANSFER','OTHER')),
    status TEXT NOT NULL DEFAULT 'CREATED' CHECK (status IN
        ('CREATED','QUOTED','AUTHORIZED','PREPARED','IN_FLIGHT','UNKNOWN','RECONCILING',
         'SUCCEEDED','SETTLED','FAILED_CONFIRMED','MANUAL_RECONCILE','CANCELLED')),
    provider_txn_id TEXT,
    failure_code TEXT,
    version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_refund_order ON refund_requests (tenant_id, order_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_refund_reconcile ON refund_requests (status, updated_at)
    WHERE status IN ('IN_FLIGHT','UNKNOWN','RECONCILING');

CREATE TABLE IF NOT EXISTS refund_quotes (
    quote_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    refund_request_id UUID NOT NULL REFERENCES refund_requests(refund_request_id),
    policy_decision_id UUID NOT NULL,
    order_version TEXT NOT NULL,
    policy_version TEXT NOT NULL DEFAULT '',
    amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
    currency CHAR(3) NOT NULL DEFAULT 'CNY',
    breakdown JSONB NOT NULL DEFAULT '{}'::jsonb,
    quote_hash TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (refund_request_id, quote_hash),
    UNIQUE (tenant_id, quote_id)
);
CREATE INDEX IF NOT EXISTS idx_quote_expiry ON refund_quotes (expires_at);

-- saga_commands：Agent DB 内的写命令（唯一外部写由 Saga Dispatcher 发出，§18.10）。
-- 携带派发所需的全部业务字段，Worker 领取后可直接调用 MCP 写工具。
CREATE TABLE IF NOT EXISTS saga_commands (
    command_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    ticket_id UUID NOT NULL,
    run_id UUID,
    operation_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    action TEXT NOT NULL,
    refund_request_id UUID,
    order_id TEXT,
    quote_id UUID,
    quote_hash TEXT,
    waybill_no TEXT,
    sku_ids TEXT[] NOT NULL DEFAULT '{}',
    authorization_ref TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'PREPARED' CHECK (status IN
        ('PREPARED','CLAIMED','IN_FLIGHT','CONFIRMED','FAILED_CONFIRMED','UNKNOWN','RECONCILING','CANCELLED')),
    attempt_count INT NOT NULL DEFAULT 0,
    lease_until TIMESTAMPTZ,
    next_retry_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_commands_claim ON saga_commands (status, next_retry_at)
    WHERE status IN ('PREPARED','CLAIMED','IN_FLIGHT','UNKNOWN');

CREATE TABLE IF NOT EXISTS outbox_events (
    event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    aggregate_id UUID,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','PUBLISHED','FAILED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox_events (status, created_at) WHERE status = 'PENDING';
