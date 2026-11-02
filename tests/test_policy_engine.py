"""确定性政策引擎：以 policy/rules.json（真实京东语料）为规则源验证裁决。"""
from app.domain.enums import Intent
from app.policy.engine import PolicyEngine
from app.policy.library import load_clauses, load_rules
from app.policy.repository import InMemoryRuleRepository


def _engine():
    return PolicyEngine(repo=InMemoryRuleRepository(load_rules()))


DELIVERED_AT = "2026-10-18T10:00:00+08:00"
CLAIM_AT = "2026-10-20T10:00:00+08:00"


def _facts(reason, category, status="DELIVERED", parcel="UNKNOWN", intent="RETURN_REFUND"):
    return {"order": {"category": category, "status": status, "delivered_at": DELIVERED_AT},
            "logistics": {"parcel_status": parcel},
            "finance": {},
            # 时间事实由横截面统一提供（政策时效判断以 claim_at 为准）
            "claim": {"reason_code": reason, "claim_at": CLAIM_AT},
            "intent": intent}


def test_corpus_rules_load_and_all_reference_clauses():
    rules = load_rules()
    assert len(rules) >= 15
    # 唯一例外：未知品类是 fail-closed 守卫规则，没有可引用的条款（靠 reason_code 表达结论）
    no_clause_ok = {"R-JD-CATEGORY-UNKNOWN-001"}
    assert all(r.required_clause_ids for r in rules if r.rule_id not in no_clause_ok)
    assert next(r for r in rules if r.rule_id in no_clause_ok).reason_code == "CATEGORY_UNKNOWN_REVIEW"
    assert not any(r.rule_id.startswith("R-FRESH") or r.rule_id == "R-REFUND-FRESH-001" for r in rules)


def test_corpus_clauses_loaded_with_logical_ids():
    chunks = load_clauses()
    assert len(chunks) > 100
    by_id = {c.clause_id: c for c in chunks}
    assert "CL-JD4268-T1" in by_id
    assert by_id["CL-JD4268-S5#3"].logical_clause_id


def test_fresh_no_reason_denied_by_exemption_clause():
    d = _engine().decide(tenant_id="t1", intent=Intent.RETURN_REFUND,
                         facts=_facts("NO_LONGER_NEEDED", "FRESH"), scope={"category": "FRESH"})
    assert d.decision == "DENY"
    assert d.reason_code == "NO_REASON_FRESH_EXEMPT"
    assert d.required_clause_ids == ["CL-JD4268-T1"]


def test_apparel_no_reason_allowed():
    d = _engine().decide(tenant_id="t1", intent=Intent.RETURN_REFUND,
                         facts=_facts("NO_LONGER_NEEDED", "APPAREL"), scope={"category": "APPAREL"})
    assert d.decision == "ALLOW"
    # 时间事实齐全时命中更具体的窗口规则（7 天内允许），而不是通用允许
    assert d.reason_code == "NO_REASON_WITHIN_7D"
    assert "CL-JD4268-A3" in d.required_clause_ids


def test_quality_claim_allowed():
    d = _engine().decide(tenant_id="t1", intent=Intent.REFUND_ONLY,
                         facts=_facts("DAMAGED", "APPAREL", intent="REFUND_ONLY"), scope={"category": "APPAREL"})
    assert d.decision == "ALLOW"
    assert d.reason_code == "QUALITY_RETURN_WINDOW"


def test_intercept_after_delivery_denied():
    d = _engine().decide(tenant_id="t1", intent=Intent.INTERCEPT_REFUND,
                         facts=_facts("NO_LONGER_NEEDED", "APPAREL", parcel="DELIVERED",
                                      intent="INTERCEPT_REFUND"), scope={})
    assert d.decision == "DENY"
    assert d.reason_code == "INTERCEPT_ALREADY_DELIVERED"


def test_unmatched_reason_falls_to_manual():
    d = _engine().decide(tenant_id="t1", intent=Intent.RETURN_REFUND,
                         facts=_facts("NO_LONGER_NEEDED", "APPAREL", status="PAID_SHIPPED"), scope={})
    assert d.decision == "MANUAL"
    assert d.reason_code == "NO_APPLICABLE_POLICY"


def test_empty_facts_needs_info_or_manual():
    d = _engine().decide(tenant_id="t1", intent=Intent.REFUND_ONLY, facts={}, scope={})
    assert d.decision in ("NEED_INFO", "MANUAL")