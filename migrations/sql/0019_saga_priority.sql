-- 0019_saga_priority.sql
-- 命令派发优先级（资金类先于物流类）+ 命令取消（补偿/撤销入口）。
ALTER TABLE saga_commands
    ADD COLUMN IF NOT EXISTS priority INT NOT NULL DEFAULT 100;

CREATE INDEX IF NOT EXISTS idx_commands_claim_priority
    ON saga_commands (priority, created_at) WHERE status = 'PREPARED';
