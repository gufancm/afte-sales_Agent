CREATE TABLE IF NOT EXISTS saga_safety_finance (
    order_id TEXT PRIMARY KEY,
    refundable_minor BIGINT NOT NULL,
    refund_count BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS saga_safety_finance_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    result_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_saga_safety_finance_rrid
    ON saga_safety_finance_idempotency ((result_json->>'refund_request_id'));
