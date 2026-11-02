-- 0017_refund_reservation.sql
-- 退款报价预占（hold）：报价与预占原子产生，执行前复核与 Saga 执行都绑定该凭证。
-- 真实资金系统对应 authorize/hold 语义；过期未消费的预占由资金侧回收。
ALTER TABLE refund_quotes
    ADD COLUMN IF NOT EXISTS reservation_reference TEXT,
    ADD COLUMN IF NOT EXISTS reservation_expires_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_quote_reservation ON refund_quotes (tenant_id, reservation_reference);