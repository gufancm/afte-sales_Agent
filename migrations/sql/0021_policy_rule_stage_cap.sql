-- 0021_policy_rule_stage_cap.sql
-- Policy Engine v2：显式裁决阶段 + 金额上限类型
--   stage   : 0=事实守卫 10=强制限制/特殊例外/必须人工 20=明确拒绝 30=一般允许 40=兜底人工
--   cap_type: POLICY_LIMIT（政策业务上限，超限拒绝） / AUTO_EXEC_THRESHOLD（自动执行阈值，超限转人工）
ALTER TABLE policy_rules ADD COLUMN IF NOT EXISTS stage INT;
ALTER TABLE policy_rules ADD COLUMN IF NOT EXISTS cap_type TEXT
    NOT NULL DEFAULT 'AUTO_EXEC_THRESHOLD'
    CHECK (cap_type IN ('POLICY_LIMIT','AUTO_EXEC_THRESHOLD'));

CREATE INDEX IF NOT EXISTS idx_policy_rules_stage ON policy_rules (tenant_id, stage, priority DESC);