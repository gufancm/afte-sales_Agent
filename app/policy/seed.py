"""默认政策规则（示例，覆盖 §17.3 的仅退款/退货退款/在途拦截）。生产从 policy_rules 表读取。"""
from __future__ import annotations

from app.policy.dsl import Condition, Rule


def default_rules() -> list[Rule]:
    return [
        Rule(
            rule_id="R-REFUND-FRESH-001", version="2026.10", priority=100,
            when=[Condition("claim.reason_code", "in", ["DAMAGED", "SPOILED", "FRESH_DAMAGED"]),
                  Condition("order.category", "eq", "FRESH")],
            decision="ALLOW", reason_code="FRESH_DAMAGED_WITHIN_WINDOW",
            required_clause_ids=["CL-FRESH-A", "CL-FRESH-EX1"],
            amount_cap_minor=20000, scope={"category": "FRESH"},
            effective_from="2026-01-01T00:00:00Z",
        ),
        Rule(
            rule_id="R-RETURN-APPAREL-001", version="2026.10", priority=90,
            when=[Condition("claim.reason_code", "in", ["NO_LONGER_NEEDED", "SIZE_ISSUE"]),
                  Condition("order.category", "eq", "APPAREL")],
            decision="ALLOW", reason_code="APPAREL_RETURN_WINDOW",
            required_clause_ids=["CL-APPAREL-A"], amount_cap_minor=50000,
            scope={"category": "APPAREL"}, effective_from="2026-01-01T00:00:00Z",
        ),
        Rule(
            rule_id="R-INTERCEPT-001", version="2026.10", priority=90,
            when=[Condition("logistics.parcel_status", "eq", "IN_TRANSIT")],
            decision="ALLOW", reason_code="IN_TRANSIT_INTERCEPT",
            required_clause_ids=["CL-INTERCEPT-A"], amount_cap_minor=None,
            effective_from="2026-01-01T00:00:00Z",
        ),
    ]


def default_clauses() -> list:
    from app.rag.retrieval import PolicyChunk
    return [
        PolicyChunk("CL-FRESH-A", "doc_fresh", "2026.10", "生鲜类商品签收后 24 小时内可申请仅退款。"),
        PolicyChunk("CL-FRESH-EX1", "doc_fresh", "2026.10", "例外：已开封且非质量问题不予仅退款。",
                    clause_group_id="GRP-FRESH", is_exception=True),
        PolicyChunk("CL-APPAREL-A", "doc_apparel", "2026.10", "服饰类支持 7 天无理由退货退款。"),
        PolicyChunk("CL-INTERCEPT-A", "doc_intercept", "2026.10", "在途包裹可申请拦截，拦截成功后按退款流程处理。"),
    ]