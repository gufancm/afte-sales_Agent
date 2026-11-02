-- 0010_parent_child.sql  Parent-Child Retrieval 扩展（LLD v4 §5.5）
-- 可重复执行（幂等）：CREATE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS / 守卫约束与策略。

CREATE TABLE IF NOT EXISTS policy_parents (
    parent_id         TEXT NOT NULL,
    tenant_id         TEXT NOT NULL,
    doc_id            TEXT NOT NULL,
    version           TEXT NOT NULL,
    level             TEXT NOT NULL CHECK (level IN ('CHAPTER','CLAUSE')),
    logical_clause_id TEXT,
    heading_path      TEXT[] NOT NULL DEFAULT '{}',
    order_index       INT NOT NULL DEFAULT 0,
    is_exception      BOOLEAN NOT NULL DEFAULT FALSE,
    display_text      TEXT NOT NULL,
    token_count       INT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, parent_id)
);
CREATE INDEX IF NOT EXISTS idx_parents_doc ON policy_parents (tenant_id, doc_id, version);
CREATE INDEX IF NOT EXISTS idx_parents_logical ON policy_parents (tenant_id, logical_clause_id);

ALTER TABLE policy_chunks
    ADD COLUMN IF NOT EXISTS parent_id         TEXT,
    ADD COLUMN IF NOT EXISTS logical_clause_id TEXT,
    ADD COLUMN IF NOT EXISTS heading_path      TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS token_count       INT;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_chunk_parent') THEN
    ALTER TABLE policy_chunks
      ADD CONSTRAINT fk_chunk_parent FOREIGN KEY (tenant_id, parent_id)
      REFERENCES policy_parents (tenant_id, parent_id) NOT VALID;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_chunks_parent ON policy_chunks (tenant_id, parent_id);

CREATE TABLE IF NOT EXISTS policy_clause_links (
    link_id               TEXT NOT NULL,
    tenant_id             TEXT NOT NULL,
    doc_id                TEXT NOT NULL,
    version               TEXT NOT NULL,
    from_logical_clause_id TEXT NOT NULL,
    to_logical_clause_id   TEXT NOT NULL,
    to_doc_id              TEXT,
    link_type              TEXT NOT NULL CHECK (link_type IN ('EXCEPTION','REFERENCE','TABLE','CROSS_DOC')),
    note                   TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, link_id)
);
CREATE INDEX IF NOT EXISTS idx_clause_links_from ON policy_clause_links (tenant_id, doc_id, from_logical_clause_id);

CREATE TABLE IF NOT EXISTS policy_rule_clause_map (
    tenant_id          TEXT NOT NULL,
    rule_id            TEXT NOT NULL,
    logical_clause_id  TEXT NOT NULL,
    version            TEXT NOT NULL,
    resolved_parent_id TEXT,
    status             TEXT NOT NULL DEFAULT 'UNRESOLVED'
                       CHECK (status IN ('UNRESOLVED','RESOLVED','MISSING')),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, rule_id, logical_clause_id, version)
);

DO $$
DECLARE r RECORD;
BEGIN
  FOR r IN
    SELECT c.table_name
    FROM information_schema.tables t
    JOIN information_schema.columns c ON c.table_schema = t.table_schema AND c.table_name = t.table_name
    WHERE t.table_schema = 'public'
      AND c.column_name = 'tenant_id'
      AND t.table_name IN ('policy_parents','policy_clause_links','policy_rule_clause_map')
  LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', r.table_name);
    EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', r.table_name);
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='public' AND tablename=r.table_name
                   AND policyname='tenant_guard_'||r.table_name) THEN
      EXECUTE format(
        'CREATE POLICY %I ON public.%I FOR ALL '
        'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')) '
        'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), ''''))',
        'tenant_guard_' || r.table_name, r.table_name);
    END IF;
  END LOOP;
END $$;

GRANT SELECT, INSERT, UPDATE, DELETE ON policy_parents, policy_clause_links, policy_rule_clause_map
    TO aftersale_app, aftersale_worker;