# -*- coding: utf-8 -*-
"""Policy Engine 机制级冲突与边界测试。

与 120 条业务金标互补：这里用**自造小规则集**精确验证裁决机制本身，
不依赖 25 条真实规则的具体阈值，因此规则改版后依然有效。

覆盖：
1. 同 stage / 同 priority 的 ALLOW vs DENY -> POLICY_CONFLICT（转人工，绝不放行）
2. stage 优先于 priority：低优先级 DENY 压过高优先级 ALLOW
3. 同 stage 内 priority 更高的 ALLOW 也会被 DENY 兜底不变量拦下
4. 例外 stage(10) 压过一般允许 stage(30)
5. blocked 规则（仅缺事实）压过通用 ALLOW -> NEED_INFO，不得放行
6. exists / not_exists 操作符（真实规则集从未使用，属未覆盖分支）
7. 规则有效期边界：生效前 1 秒 / 生效瞬间 / 失效瞬间 / 失效后 1 秒
8. 空规则集 -> NO_APPLICABLE_POLICY
9. 比较符逐个验证 gte/gt/lte/lt/between 的阈值语义
10. 金额 cap 边界：等于上限不放行阻断，超 1 分才触发 DENY/MANUAL
11. 缺用户可补事实 -> NEED_INFO；缺系统权威事实 -> MANUAL(AUTHORITATIVE_FACT_UNAVAILABLE)
"""
from __future__ import annotations

from app.domain.enums import Intent
from app.policy.dsl import Condition, Rule
from app.policy.engine import PolicyEngine
from app.policy.repository import InMemoryRuleRepository


def _rule(rule_id, when, decision, reason, *, priority=100, stage=None, clauses=("CL-T-1",),
          cap=None, cap_type="AUTO_EXEC_THRESHOLD", effective_from=None, effective_to=None,
          status="PUBLISHED"):
    return Rule(rule_id=rule_id, version="t-1", priority=priority, when=list(when),
                decision=decision, reason_code=reason, required_clause_ids=list(clauses),
                amount_cap_minor=cap, cap_type=cap_type, stage=stage,
                effective_from=effective_from, effective_to=effective_to, status=status)


def _engine(*rules):
    return PolicyEngine(repo=InMemoryRuleRepository(list(rules)))


def _facts(**claim):
    order = {"category": claim.pop("category", "APPAREL"), "status": claim.pop("status", "DELIVERED")}
    if "delivered_at" in claim:
        order["delivered_at"] = claim.pop("delivered_at")
    return {"order": order, "logistics": {}, "finance": {}, "claim": dict(claim)}


AS_OF = "2026-10-11T10:00:00+08:00"


# ---------------------------------------------------------------- 1. 同阶段冲突
def test_same_stage_same_priority_conflict_goes_manual():
    eng = _engine(
        _rule("R-ALLOW", [Condition("order.category", "eq", "APPAREL")], "ALLOW", "A", priority=100, stage=20),
        _rule("R-DENY", [Condition("order.category", "eq", "APPAREL")], "DENY", "D", priority=100, stage=20),
    )
    d = eng.decide(tenant_id="t", intent=Intent.RETURN_REFUND, facts=_facts(), as_of=AS_OF)
    assert d.decision == "MANUAL"
    assert d.reason_code == "POLICY_CONFLICT"
    assert d.trace["conflict"]["type"] == "stage_priority_specificity_tie"


# ---------------------------------------------------------------- 2. stage 优先
def test_stage_dominates_priority_deny_wins():
    eng = _engine(
        _rule("R-DENY", [Condition("order.category", "eq", "APPAREL")], "DENY", "D", priority=1, stage=20),
        _rule("R-ALLOW", [Condition("order.category", "eq", "APPAREL")], "ALLOW", "A", priority=9999, stage=30),
    )
    d = eng.decide(tenant_id="t", intent=Intent.RETURN_REFUND, facts=_facts(), as_of=AS_OF)
    assert d.decision == "DENY" and d.rule_ids == ["R-DENY"]


# ------------------------------------------- 3. 同 stage 高优先级 ALLOW 仍被 DENY 拦下
def test_deny_override_invariant_within_same_stage():
    eng = _engine(
        _rule("R-DENY", [Condition("order.category", "eq", "APPAREL")], "DENY", "D", priority=1, stage=20),
        _rule("R-ALLOW", [Condition("order.category", "eq", "APPAREL")], "ALLOW", "A", priority=9999, stage=20),
    )
    d = eng.decide(tenant_id="t", intent=Intent.RETURN_REFUND, facts=_facts(), as_of=AS_OF)
    assert d.decision == "DENY"
    assert d.trace["conflict"]["type"] == "deny_overrides_allow"
    assert d.trace["winner"]["forced_by"] == "deny_overrides_allow"


# ---------------------------------------------------------------- 4. 例外压过允许
def test_exception_stage_beats_general_allow():
    eng = _engine(
        _rule("R-EXC", [Condition("order.category", "eq", "FRESH")], "DENY", "E", priority=1, stage=10),
        _rule("R-ALLOW", [Condition("order.status", "eq", "DELIVERED")], "ALLOW", "A", priority=9999, stage=30),
    )
    d = eng.decide(tenant_id="t", intent=Intent.RETURN_REFUND, facts=_facts(category="FRESH"), as_of=AS_OF)
    assert d.decision == "DENY" and d.rule_ids == ["R-EXC"]


# --------------------------------------------- 5. blocked 压过通用允许（不得放行）
def test_blocked_rule_beats_general_allow():
    eng = _engine(
        _rule("R-NEED-REASON", [Condition("claim.reason_code", "eq", "DAMAGED"),
                                Condition("order.category", "eq", "APPAREL")],
              "DENY", "D", priority=100, stage=20),
        _rule("R-ALLOW", [Condition("order.status", "eq", "DELIVERED")], "ALLOW", "A", priority=100, stage=30),
    )
    d = eng.decide(tenant_id="t", intent=Intent.RETURN_REFUND, facts=_facts(), as_of=AS_OF)
    assert d.decision == "NEED_INFO"
    assert d.reason_code == "MISSING_FACT"
    assert d.missing_facts == ["claim.reason_code"]
    assert d.rule_ids == ["R-NEED-REASON"]
    assert d.required_clause_ids == []   # 澄清结论不引用条款，条款经 rule_id 反查
    assert d.trace["winner"]["blocked_by_missing_fact"] is True


# ---------------------------------------------------------------- 6. exists 分支
def test_exists_and_not_exists_operators():
    eng = _engine(
        _rule("R-HAS-AMOUNT", [Condition("finance.requested_amount_minor", "exists")], "ALLOW", "HAS", stage=20),
        _rule("R-NO-AMOUNT", [Condition("finance.requested_amount_minor", "not_exists")], "ALLOW", "NONE", stage=20),
    )
    f = _facts()
    f["finance"] = {"requested_amount_minor": 100}
    d = eng.decide(tenant_id="t", intent=Intent.RETURN_REFUND, facts=f, as_of=AS_OF)
    assert d.reason_code == "HAS"
    d = eng.decide(tenant_id="t", intent=Intent.RETURN_REFUND, facts=_facts(), as_of=AS_OF)
    assert d.reason_code == "NONE"


# ---------------------------------------------------------------- 7. 有效期边界
def test_effective_window_boundaries():
    rule = _rule("R-WIN", [Condition("order.category", "eq", "APPAREL")], "ALLOW", "W",
                 effective_from="2026-10-01T00:00:00+08:00",
                 effective_to="2026-10-31T23:59:59+08:00")
    eng = _engine(rule)
    cases = {
        "2026-09-30T23:59:59+08:00": "NO_APPLICABLE_POLICY",   # 生效前 1 秒
        "2026-10-01T00:00:00+08:00": "W",                       # 生效瞬间（含）
        "2026-10-31T23:59:59+08:00": "W",                       # 失效瞬间（含）
        "2026-11-01T00:00:00+08:00": "NO_APPLICABLE_POLICY",   # 失效后 1 秒
    }
    for as_of, expected in cases.items():
        d = eng.decide(tenant_id="t", intent=Intent.RETURN_REFUND, facts=_facts(), as_of=as_of)
        assert d.reason_code == expected, (as_of, d.reason_code)
        if expected == "NO_APPLICABLE_POLICY":
            assert d.decision == "MANUAL"


def test_retired_rule_never_loads():
    eng = _engine(_rule("R-OLD", [Condition("order.category", "eq", "APPAREL")], "ALLOW", "W", status="RETIRED"))
    d = eng.decide(tenant_id="t", intent=Intent.RETURN_REFUND, facts=_facts(), as_of=AS_OF)
    assert d.reason_code == "NO_APPLICABLE_POLICY"


# ---------------------------------------------------------------- 8. 空规则集
def test_empty_ruleset_is_manual_not_allow():
    d = _engine().decide(tenant_id="t", intent=Intent.RETURN_REFUND, facts=_facts(), as_of=AS_OF)
    assert d.decision == "MANUAL" and d.reason_code == "NO_APPLICABLE_POLICY"


# ---------------------------------------------------------------- 9. 比较符
def _decide_cmp(op, value, actual):
    eng = _engine(_rule("R-CMP", [Condition("claim.quantity", op, value)], "ALLOW", "OK"))
    return eng.decide(tenant_id="t", intent=Intent.RETURN_REFUND, facts=_facts(quantity=actual), as_of=AS_OF)


def test_comparison_operators_at_threshold():
    assert _decide_cmp("gte", 7, 7).reason_code == "OK"
    assert _decide_cmp("gte", 7, 6).reason_code == "NO_APPLICABLE_POLICY"
    assert _decide_cmp("gt", 7, 7).reason_code == "NO_APPLICABLE_POLICY"
    assert _decide_cmp("gt", 7, 8).reason_code == "OK"
    assert _decide_cmp("lte", 7, 7).reason_code == "OK"
    assert _decide_cmp("lte", 7, 8).reason_code == "NO_APPLICABLE_POLICY"
    assert _decide_cmp("lt", 7, 7).reason_code == "NO_APPLICABLE_POLICY"
    assert _decide_cmp("lt", 7, 6).reason_code == "OK"
    assert _decide_cmp("between", [7, 15], 7).reason_code == "OK"
    assert _decide_cmp("between", [7, 15], 15).reason_code == "OK"
    assert _decide_cmp("between", [7, 15], 16).reason_code == "NO_APPLICABLE_POLICY"


# ---------------------------------------------------------------- 10. 金额边界
def test_amount_cap_boundary_policy_limit():
    rule = _rule("R-CAP", [Condition("order.category", "eq", "APPAREL")], "ALLOW", "OK",
                 cap=2000, cap_type="POLICY_LIMIT")
    eng = _engine(rule)
    f = _facts()
    f["finance"] = {"requested_amount_minor": 2000}
    assert eng.decide(tenant_id="t", intent=Intent.RETURN_REFUND, facts=f, as_of=AS_OF).decision == "ALLOW"
    f["finance"] = {"requested_amount_minor": 2001}
    d = eng.decide(tenant_id="t", intent=Intent.RETURN_REFUND, facts=f, as_of=AS_OF)
    assert d.decision == "DENY" and d.reason_code == "AMOUNT_EXCEEDS_POLICY_LIMIT"


def test_amount_cap_boundary_auto_threshold():
    rule = _rule("R-AUTO", [Condition("order.category", "eq", "APPAREL")], "ALLOW", "OK",
                 cap=250000, cap_type="AUTO_EXEC_THRESHOLD")
    eng = _engine(rule)
    f = _facts()
    f["finance"] = {"requested_amount_minor": 250001}
    d = eng.decide(tenant_id="t", intent=Intent.RETURN_REFUND, facts=f, as_of=AS_OF)
    # 超自动执行阈值 ≠ 没有退款资格：仍保留 ALLOW 的资格信息，只是转人工
    assert d.decision == "MANUAL" and d.reason_code == "AMOUNT_EXCEEDS_AUTO_THRESHOLD"
    assert "OK" in d.reason_codes and d.requires_manual_approval is True


def test_missing_amount_blocks_execution_but_keeps_eligibility():
    rule = _rule("R-AUTO", [Condition("order.category", "eq", "APPAREL")], "ALLOW", "OK", cap=250000)
    d = _engine(rule).decide(tenant_id="t", intent=Intent.RETURN_REFUND, facts=_facts(), as_of=AS_OF)
    assert d.decision == "ALLOW"
    assert d.execution_blocked is True
    assert d.requires_manual_approval is True


# ------------------------------------------- 11. 缺事实的两条兜底路径必须分开
def test_missing_user_fact_is_need_info():
    eng = _engine(_rule("R-Q", [Condition("claim.reason_code", "eq", "DAMAGED")], "ALLOW", "OK"))
    d = eng.decide(tenant_id="t", intent=Intent.RETURN_REFUND, facts=_facts(), as_of=AS_OF)
    assert d.decision == "NEED_INFO" and d.reason_code == "MISSING_FACT"


def test_missing_authoritative_fact_is_manual():
    # 没有任何规则成为候选，且缺的是系统侧权威事实（签收时间）
    eng = _engine(_rule("R-Q", [Condition("claim.day_index_signed", "lte", 7)], "ALLOW", "OK"))
    d = eng.decide(tenant_id="t", intent=Intent.RETURN_REFUND,
                   facts=_facts(claim_at="2026-10-09T10:00:00+08:00"), as_of=AS_OF)
    assert d.decision == "MANUAL"
    assert d.reason_code == "AUTHORITATIVE_FACT_UNAVAILABLE"
    assert "order.delivered_at" in d.missing_facts

# ---------------------------------- 12. 无关缺失事实不得污染裁决（独立验证发现）
def test_irrelevant_missing_fact_does_not_change_decision():
    # 规则同时要求 status=DELIVERED 与签收天数；status 已失败时，缺签收时间与结论无关
    eng = _engine(_rule("R-NEED-TIME", [Condition("order.status", "eq", "DELIVERED"),
                                        Condition("claim.day_index_signed", "lte", 7)],
                        "ALLOW", "OK"))
    d = eng.decide(tenant_id="t", intent=Intent.RETURN_REFUND,
                   facts=_facts(status="PAID_UNSHIPPED", claim_at="2026-10-09T10:00:00+08:00"),
                   as_of=AS_OF)
    assert d.decision == "MANUAL"
    assert d.reason_code == "NO_APPLICABLE_POLICY"
    assert d.missing_facts == []
    assert "order.delivered_at" in d.trace["irrelevant_missing_facts"]
