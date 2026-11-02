-- 0014_tool_calls.sql 外部工具（只读 MCP）调用审计：谁/哪个工单节点/耗时/状态/重试
CREATE TABLE IF NOT EXISTS tool_calls (
    call_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    ticket_id UUID,
    run_id UUID,
    trace_id TEXT,
    node TEXT,
    tool TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('SUCCESS','ERROR')),
    error_code TEXT,
    attempts INT,
    latency_ms INT,
    critical BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_ticket ON tool_calls (tenant_id, ticket_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_calls_tool ON tool_calls (tenant_id, tool, created_at DESC);

-- 与其它业务表一致：强制行级安全，仅在事务内设置 app.tenant_id 才放行
DO $$
BEGIN
  IF to_regclass('public.tool_calls') IS NOT NULL THEN
    EXECUTE 'ALTER TABLE public.tool_calls ENABLE ROW LEVEL SECURITY';
    EXECUTE 'ALTER TABLE public.tool_calls FORCE ROW LEVEL SECURITY';
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'tool_calls' AND policyname = 'tenant_guard_tool_calls') THEN
      EXECUTE 'CREATE POLICY tenant_guard_tool_calls ON public.tool_calls FOR ALL '
              'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')) '
              'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), ''''))';
    END IF;
  END IF;
END $$;