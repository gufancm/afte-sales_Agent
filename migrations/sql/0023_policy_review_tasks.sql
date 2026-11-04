-- 0023_policy_review_tasks.sql
-- V4 后台政策核实任务：与风险审批 HITL 明确分离。
CREATE TABLE IF NOT EXISTS policy_review_tasks (
    task_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    ticket_id UUID,
    thread_id TEXT,
    workflow_version TEXT NOT NULL DEFAULT 'v4',
    policy_version TEXT,
    reason_code TEXT NOT NULL,
    retrieval_bundle JSONB NOT NULL DEFAULT '{}'::jsonb,
    facts_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING','RUNNING','DONE','REJECTED','CANCELLED')),
    resolution TEXT,
    reviewer_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, ticket_id, reason_code, policy_version)
);
CREATE INDEX IF NOT EXISTS idx_policy_review_pending
    ON policy_review_tasks (tenant_id, status, created_at);
