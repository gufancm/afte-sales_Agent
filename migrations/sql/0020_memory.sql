-- 0020_memory.sql  记忆系统（短期跨轮摘要 / 情景记忆 / 语义记忆）
-- 幂等：CREATE IF NOT EXISTS / 守卫式 ALTER / ADD COLUMN IF NOT EXISTS。
-- 说明：记忆**非权威**，仅用于理解与措辞；不得作为政策/风控/资金裁决输入。

-- ------------------------------------------------------------------ --
-- 短期跨轮：消息严格顺序号（按插入顺序回放历史）+ 滚动摘要
-- ------------------------------------------------------------------ --
CREATE SEQUENCE IF NOT EXISTS ticket_message_seq;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_schema='public' AND table_name='ticket_messages'
                   AND column_name='message_seq') THEN
    ALTER TABLE ticket_messages ADD COLUMN message_seq BIGINT;
    UPDATE ticket_messages tm SET message_seq = sub.rn
      FROM (SELECT message_id, row_number() OVER (ORDER BY created_at, message_id) AS rn
            FROM ticket_messages) sub
      WHERE tm.message_id = sub.message_id;
    ALTER TABLE ticket_messages ALTER COLUMN message_seq SET DEFAULT nextval('ticket_message_seq');
    PERFORM setval('ticket_message_seq',
                   GREATEST(COALESCE((SELECT MAX(message_seq) FROM ticket_messages), 0), 1));
    CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_seq ON ticket_messages (message_seq);
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS conversation_summaries (
    tenant_id           TEXT NOT NULL,
    ticket_id           UUID NOT NULL,
    summary             TEXT NOT NULL,
    up_to_message_count INT NOT NULL DEFAULT 0,
    version             BIGINT NOT NULL DEFAULT 0,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, ticket_id)
);

-- ------------------------------------------------------------------ --
-- 情景记忆：已结束工单的摘要 + 向量（非权威）
-- ------------------------------------------------------------------ --
CREATE TABLE IF NOT EXISTS episodic_memories (
    memory_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    TEXT NOT NULL,
    user_id      TEXT,
    ticket_id    UUID,
    intent       TEXT,
    outcome_type TEXT,
    summary      TEXT NOT NULL,
    embedding    vector(1024),
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_episodic_scope
    ON episodic_memories (tenant_id, user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_episodic_hnsw
    ON episodic_memories USING hnsw (embedding vector_cosine_ops)
    WHERE embedding IS NOT NULL;

-- ------------------------------------------------------------------ --
-- 语义记忆：用户画像（非权威）
-- ------------------------------------------------------------------ --
CREATE TABLE IF NOT EXISTS user_profiles (
    tenant_id  TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    profile    JSONB NOT NULL DEFAULT '{}'::jsonb,
    version    BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id)
);

-- ------------------------------------------------------------------ --
-- RLS（与 0007/0010 一致：仅本事务 app.tenant_id 匹配才放行）
-- ------------------------------------------------------------------ --
DO $$
DECLARE r RECORD;
BEGIN
  FOR r IN
    SELECT c.table_name
    FROM information_schema.tables t
    JOIN information_schema.columns c
      ON c.table_schema = t.table_schema AND c.table_name = t.table_name
    WHERE t.table_schema = 'public' AND c.column_name = 'tenant_id'
      AND t.table_name IN ('conversation_summaries','episodic_memories','user_profiles')
  LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', r.table_name);
    EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', r.table_name);
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='public' AND tablename=r.table_name
                   AND policyname='tenant_guard_'||r.table_name) THEN
      EXECUTE format(
        $fmt$CREATE POLICY %I ON public.%I FOR ALL
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))$fmt$,
        'tenant_guard_' || r.table_name, r.table_name);
    END IF;
  END LOOP;
END $$;

GRANT SELECT, INSERT, UPDATE, DELETE
    ON conversation_summaries, episodic_memories, user_profiles
    TO aftersale_app, aftersale_worker;
GRANT USAGE, SELECT ON SEQUENCE ticket_message_seq TO aftersale_app, aftersale_worker;