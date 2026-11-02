-- 0022_approval_ticket_scope.sql
-- 审批幂等必须按工单隔离。
-- 旧唯一键 (tenant_id, plan_hash) 会在“不同工单、相同业务内容”时复用上一张工单的审批，
-- 甚至直接拿到 APPROVED 状态，等于绕过人工审批执行资金动作。
-- 幂等范围：同一工单内同一计划指纹只建一条审批；跨工单必须重新审批。
ALTER TABLE approval_tasks
    DROP CONSTRAINT IF EXISTS approval_tasks_tenant_id_plan_hash_key;
ALTER TABLE approval_tasks
    DROP CONSTRAINT IF EXISTS uq_approval_plan;
CREATE UNIQUE INDEX IF NOT EXISTS uq_approval_ticket_plan
    ON approval_tasks (tenant_id, ticket_id, plan_hash);
