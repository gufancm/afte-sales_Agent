-- 0003_policy_and_rag.sql
CREATE TABLE IF NOT EXISTS policy_documents (
    doc_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    title TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PUBLISHED' CHECK (status IN ('DRAFT','PUBLISHED','SUPERSEDED','RETIRED')),
    effective_from TIMESTAMPTZ,
    effective_to TIMESTAMPTZ,
    source_uri TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS policy_rules (
    rule_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    version TEXT NOT NULL,
    priority INT NOT NULL DEFAULT 0,
    when_json JSONB NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('ALLOW','DENY','MANUAL','NEED_INFO')),
    reason_code TEXT NOT NULL,
    required_clause_ids TEXT[] NOT NULL DEFAULT '{}',
    amount_cap_minor BIGINT,
    scope JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'PUBLISHED' CHECK (status IN ('PUBLISHED','SUPERSEDED','RETIRED')),
    effective_from TIMESTAMPTZ,
    effective_to TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS policy_decisions (
    policy_decision_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    ticket_id UUID,
    decision TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    rule_ids TEXT[] NOT NULL DEFAULT '{}',
    required_clause_ids TEXT[] NOT NULL DEFAULT '{}',
    amount_cap_minor BIGINT,
    policy_version TEXT,
    time_basis TEXT,
    as_of TIMESTAMPTZ,
    scope JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_policy_decisions_ticket ON policy_decisions (tenant_id, ticket_id, created_at DESC);

CREATE TABLE IF NOT EXISTS policy_chunks (
    chunk_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    version TEXT NOT NULL,
    clause_id TEXT NOT NULL,
    clause_group_id TEXT,
    is_exception BOOLEAN NOT NULL DEFAULT false,
    text TEXT NOT NULL,
    embedding_model TEXT,
    embedding vector(1024),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, clause_id, version)
);
CREATE INDEX IF NOT EXISTS idx_chunks_clause ON policy_chunks (tenant_id, clause_id);