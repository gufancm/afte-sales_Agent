-- 0007_rls.sql  行级安全（§10.8/§19.2）。仅在明确事务内设置 app.tenant_id 才放行。
DO $$
DECLARE r RECORD;
BEGIN
  FOR r IN
    SELECT c.table_name
    FROM information_schema.tables t
    JOIN information_schema.columns c ON c.table_schema = t.table_schema AND c.table_name = t.table_name
    WHERE t.table_schema = 'public'
      AND c.column_name = 'tenant_id'
      AND t.table_name IN (
        'aftersale_tickets','ticket_messages','ticket_runs','action_plans','risk_decisions',
        'approval_tasks','refund_requests','refund_quotes','saga_commands','outbox_events',
        'ticket_events','llm_calls','agent_audit_events','inbox_events','reconciliation_tasks',
        'dead_letter_events','policy_documents','policy_rules','policy_decisions','policy_chunks')
  LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', r.table_name);
    EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', r.table_name);
    EXECUTE format(
      'CREATE POLICY %I ON public.%I FOR ALL '
      'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')) '
      'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), ''''))',
      'tenant_guard_' || r.table_name, r.table_name);
  END LOOP;
END $$;