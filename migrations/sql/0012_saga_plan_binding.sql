-- 0012_saga_plan_binding.sql
-- 把冻结计划 plan_id 绑定到 Saga 命令，使 Risk / HITL / Guard / Saga 引用同一计划标识（§7.4/§9.1）。
-- 幂等：ADD COLUMN IF NOT EXISTS。
ALTER TABLE saga_commands ADD COLUMN IF NOT EXISTS plan_id UUID REFERENCES action_plans(plan_id);
CREATE INDEX IF NOT EXISTS idx_commands_plan ON saga_commands (plan_id) WHERE plan_id IS NOT NULL;