-- 0018_governance.sql
-- 审批治理（审批人角色 / 审批意见）与命令级对账（reconciliation_tasks.command_id）。
-- 命令租约过期回收复用 saga_commands.lease_until（0004 已有该列）。
ALTER TABLE approval_tasks
    ADD COLUMN IF NOT EXISTS reviewer_role TEXT,
    ADD COLUMN IF NOT EXISTS decision_comment TEXT;

ALTER TABLE reconciliation_tasks
    ADD COLUMN IF NOT EXISTS command_id UUID;

CREATE INDEX IF NOT EXISTS idx_recon_command ON reconciliation_tasks (tenant_id, command_id);