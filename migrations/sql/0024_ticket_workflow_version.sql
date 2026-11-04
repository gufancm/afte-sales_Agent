-- 0024_ticket_workflow_version.sql
-- 工单级工作流版本：确保 V3 暂停工单在 V4 部署后仍按 V3 图恢复。
ALTER TABLE aftersale_tickets
    ADD COLUMN IF NOT EXISTS workflow_version TEXT NOT NULL DEFAULT 'v3'
    CHECK (workflow_version IN ('v3', 'v4'));
