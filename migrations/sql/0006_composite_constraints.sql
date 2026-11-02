-- 0006_composite_constraints.sql  跨表组合外键（在相关唯一键齐备后创建，§18.2）
ALTER TABLE action_plans
    ADD CONSTRAINT fk_plans_quote FOREIGN KEY (tenant_id, quote_id)
    REFERENCES refund_quotes (tenant_id, quote_id) NOT VALID;
ALTER TABLE refund_requests
    ADD CONSTRAINT fk_refund_quote FOREIGN KEY (tenant_id, current_quote_id)
    REFERENCES refund_quotes (tenant_id, quote_id) NOT VALID;
ALTER TABLE risk_decisions
    ADD CONSTRAINT fk_risk_quote FOREIGN KEY (tenant_id, quote_id)
    REFERENCES refund_quotes (tenant_id, quote_id) NOT VALID;
ALTER TABLE approval_tasks
    ADD CONSTRAINT fk_approval_quote FOREIGN KEY (tenant_id, quote_id)
    REFERENCES refund_quotes (tenant_id, quote_id) NOT VALID;
-- 报价变更必须写新行；不覆盖历史（DB 层不强绑，由应用与 UNIQUE(refund_request_id,quote_hash) 保证）