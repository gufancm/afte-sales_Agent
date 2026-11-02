-- 0015_policy_decision_trace.sql 政策裁决 trace：载入/命中规则、条件取值、冲突、胜出规则
-- 用于「为什么放行/拒绝/转人工」的可解释性与申诉取证；policy_engine 节点写入。
ALTER TABLE policy_decisions ADD COLUMN IF NOT EXISTS trace JSONB NOT NULL DEFAULT '{}'::jsonb;