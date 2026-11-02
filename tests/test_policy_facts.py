# -*- coding: utf-8 -*-
"""政策事实层、裁决阶段与边界规则单测（零 LLM / 零 DB）。"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.domain.enums import Intent, normalize_category  # noqa: E402
from app.policy.dsl import Condition, Rule  # noqa: E402
from app.policy.engine import PolicyEngine  # noqa: E402
from app.policy.facts import (BASIS_NEXT_DAY, BASIS_SIGN_DATE, claim_time_facts,  # noqa: E402
                              day_index)
from app.policy.library import load_rules  # noqa: E402
from app.policy.repository import InMemoryRuleRepository  # noqa: E402

AS_OF = "2026-10-20T12:00:00+08:00"
DELIVERED = "2026-10-18T10:00:00+08:00"   # 第 3 天（SIGN_DATE）


def _engine(rules=None) -> PolicyEngine:
    return PolicyEngine(repo=InMemoryRuleRepository(rules if rules is not None else load_rules()))


def _facts(*, delivered_at=DELIVERED, claim_at=AS_OF, category="APPAREL",
           reason="NO_LONGER_NEEDED", status="DELIVERED", amount=None,
           parcel="UNKNOWN", intent=Intent.RETURN_REFUND) -> dict:
    finance = {"currency": "CNY"}
    if amount is not None:
        finance["requested_amount_minor"] = amount
    return {
        "order": {"category": category, "status": status, "currency": "CNY",
                  "delivered_at": delivered_at},
        "logistics": {"parcel_status": parcel},
        "finance": finance,
        "claim": {"reason_code": reason, **claim_time_facts(delivered_at=delivered_at, claim_at=claim_at)},
        "intent": str(intent),
    }


# ---------- 品类归一化 ----------
def test_normalize_category_known_and_alias():
    assert normalize_category("FRESH") == "FRESH"
    assert normalize_category("  apparel ") == "APPAREL"
    assert normalize_category("生鲜") == "FRESH"
    assert normalize_category("图书") == "BOOK_MEDIA"


def test_normalize_category_unknown_falls_back():
    assert normalize_category("新品类X") == "UNKNOWN"
    assert normalize_category(None) == "UNKNOWN"
    assert normalize_category("") == "UNKNOWN"


# ---------- 时间口径 ----------
def test_day_index_two_bases():
    # 10-01 23:50 签收，10-08 23:55 申请
    d, c = "2026-10-01T23:50:00+08:00", "2026-10-08T23:55:00+08:00"
    assert day_index(d, c, BASIS_SIGN_DATE) == 8      # 签收日算第 1 天 -> 第 8 天，已超 7 天
    assert day_index(d, c, BASIS_NEXT_DAY) == 7       # 次日 0 点算第 1 天 -> 第 7 天，未超 15 天


def test_day_index_boundaries():
    # 签收日算第 1 天：10-14 是第 7 天（仍在窗口内），10-13 是第 8 天（已超）
    assert day_index("2026-10-14T12:00:00+08:00", AS_OF) == 7
    assert day_index("2026-10-14T23:59:00+08:00", AS_OF) == 7
    assert day_index("2026-10-13T23:59:00+08:00", AS_OF) == 8
    assert day_index("2026-10-20T08:00:00+08:00", AS_OF) == 1


def test_day_index_invalid_is_none():
    assert day_index(None, AS_OF) is None
    assert day_index("", AS_OF) is None
    assert day_index("not-a-date", AS_OF) is None
    assert day_index("2026-10-21T12:00:00+08:00", AS_OF) is None   # 申请早于签收
    assert day_index("2026-10-14T04:00:00+00:00", AS_OF) == 7      # UTC 04:00 = 东八区 12:00


# ---------- 7 天 / 15 天边界 ----------
def test_within_7_days_allows_no_reason_return():
    d = _engine().decide(tenant_id="t", intent=Intent.RETURN_REFUND,
                         facts=_facts(claim_at="2026-10-20T12:00:00+08:00"), as_of=AS_OF)
    assert d.decision == "ALLOW"
    assert d.reason_code == "NO_REASON_WITHIN_7D"
    assert "CL-JD4268-A3" in d.required_clause_ids


def test_day_8_denies_no_reason_return():
    d = _engine().decide(tenant_id="t", intent=Intent.RETURN_REFUND,
                         facts=_facts(delivered_at="2026-10-13T10:00:00+08:00"), as_of=AS_OF)
    assert d.decision == "DENY"
    assert d.reason_code == "NO_REASON_WINDOW_EXPIRED"


def test_missing_claim_time_goes_manual_not_need_info():
    """申请时点是平台记录的权威事实，缺失不能让用户补填（否则可改写时效裁决）。"""
    d = _engine().decide(tenant_id="t", intent=Intent.RETURN_REFUND,
                         facts=_facts(claim_at=None), as_of=AS_OF)
    assert d.decision == "MANUAL"
    assert d.reason_code == "AUTHORITATIVE_FACT_UNAVAILABLE"
    assert "claim.claim_at" in d.missing_facts


def test_missing_delivered_at_goes_manual():
    """签收时间是系统权威事实，缺失不应让用户“随便填一个”，直接转人工。"""
    d = _engine().decide(tenant_id="t", intent=Intent.RETURN_REFUND,
                         facts=_facts(delivered_at=None), as_of=AS_OF)
    assert d.decision == "MANUAL"
    assert d.reason_code == "AUTHORITATIVE_FACT_UNAVAILABLE"
    assert "order.delivered_at" in d.missing_facts


def test_bakery_uses_next_day_basis():
    ok = _engine().decide(tenant_id="t", intent=Intent.RESHIPMENT,
                          facts=_facts(category="BAKERY", reason="DATE_DISSATISFIED",
                                       delivered_at="2026-10-05T10:00:00+08:00",
                                       claim_at="2026-10-20T10:00:00+08:00"), as_of=AS_OF)
    assert ok.decision == "ALLOW" and ok.reason_code == "BAKERY_EXCHANGE_WITHIN_15D"
    late = _engine().decide(tenant_id="t", intent=Intent.RESHIPMENT,
                            facts=_facts(category="BAKERY", reason="DATE_DISSATISFIED",
                                         delivered_at="2026-10-04T10:00:00+08:00",
                                         claim_at="2026-10-20T10:00:00+08:00"), as_of=AS_OF)
    assert late.decision == "DENY" and late.reason_code == "BAKERY_EXCHANGE_WINDOW_EXPIRED"


# ---------- 品类例外 / 兜底 ----------
def test_unknown_category_goes_manual_even_with_matching_allow_rule():
    d = _engine().decide(tenant_id="t", intent=Intent.RETURN_REFUND,
                         facts=_facts(category="UNKNOWN"), as_of=AS_OF)
    assert d.decision == "MANUAL"
    assert d.reason_code == "CATEGORY_UNKNOWN_REVIEW"


def test_fresh_exemption_beats_window_rule():
    d = _engine().decide(tenant_id="t", intent=Intent.RETURN_REFUND,
                         facts=_facts(category="FRESH"), as_of=AS_OF)
    assert d.decision == "DENY"
    assert d.reason_code == "NO_REASON_FRESH_EXEMPT"


# ---------- 冲突：DENY 永远压过 ALLOW ----------
def _allow_all(priority: int) -> Rule:
    return Rule(rule_id="R-TEST-ALLOW-999", version="test", priority=priority,
                when=[Condition("order.status", "eq", "DELIVERED")],
                decision="ALLOW", reason_code="TEST_ALLOW")


def test_deny_beats_allow_regardless_of_priority():
    rules = [Rule(rule_id="R-TEST-DENY-001", version="test", priority=1,
                  when=[Condition("order.status", "eq", "DELIVERED")],
                  decision="DENY", reason_code="TEST_DENY"),
             _allow_all(priority=9999)]
    d = _engine(rules).decide(tenant_id="t", intent=Intent.RETURN_REFUND,
                              facts=_facts(), as_of=AS_OF)
    assert d.decision == "DENY" and d.reason_code == "TEST_DENY"


def test_same_stage_conflict_with_different_outcomes_goes_manual():
    rules = [Rule(rule_id="R-TEST-A", version="test", priority=10, stage=20,
                  when=[Condition("order.status", "eq", "DELIVERED")],
                  decision="DENY", reason_code="A"),
             Rule(rule_id="R-TEST-B", version="test", priority=10, stage=20,
                  when=[Condition("order.status", "eq", "DELIVERED")],
                  decision="ALLOW", reason_code="B")]
    d = _engine(rules).decide(tenant_id="t", intent=Intent.UNKNOWN, facts=_facts(), as_of=AS_OF)
    assert d.decision == "MANUAL" and d.reason_code == "POLICY_CONFLICT"


# ---------- 金额语义 ----------
def test_amount_over_auto_threshold_goes_manual_but_keeps_eligibility():
    facts = _facts(reason="LATE_DELIVERY", amount=2500)
    d = _engine().decide(tenant_id="t", intent=Intent.COMPLAINT, facts=facts, as_of=AS_OF)
    assert d.decision == "MANUAL"
    assert d.reason_code == "AMOUNT_EXCEEDS_AUTO_THRESHOLD"
    assert d.amount_cap is not None and d.amount_cap.cap_minor == 2000
    assert d.amount_cap.cap_type == "AUTO_EXEC_THRESHOLD"
    assert d.requires_manual_approval is True
    # 业务资格仍在：原规则命中的理由保留在 reason_codes
    assert "LATE_DELIVERY_COMPENSATION" in d.reason_codes


def test_amount_within_cap_allows():
    d = _engine().decide(tenant_id="t", intent=Intent.COMPLAINT,
                         facts=_facts(reason="LATE_DELIVERY", amount=2000), as_of=AS_OF)
    assert d.decision == "ALLOW"


def test_policy_limit_over_cap_denies():
    rules = [Rule(rule_id="R-TEST-CAP", version="test", priority=10,
                  when=[Condition("claim.reason_code", "eq", "LATE_DELIVERY")],
                  decision="ALLOW", reason_code="CAP_ALLOW", amount_cap_minor=100,
                  cap_type="POLICY_LIMIT")]
    d = _engine(rules).decide(tenant_id="t", intent=Intent.COMPLAINT,
                              facts=_facts(reason="LATE_DELIVERY", amount=101), as_of=AS_OF)
    assert d.decision == "DENY" and d.reason_code == "AMOUNT_EXCEEDS_POLICY_LIMIT"


def test_missing_amount_blocks_execution_not_eligibility():
    d = _engine().decide(tenant_id="t", intent=Intent.COMPLAINT,
                         facts=_facts(reason="LATE_DELIVERY"), as_of=AS_OF)
    assert d.decision == "ALLOW"
    assert d.execution_blocked is True
    assert d.requires_manual_approval is True
    assert d.amount_check["status"] == "UNKNOWN"


# ---------- 规则集指纹 ----------
def test_ruleset_hash_is_content_addressed():
    base = load_rules()
    changed = load_rules()
    idx = next(i for i, r in enumerate(changed) if r.rule_id == "R-JD-NR-WINDOW-OK-001")
    r = changed[idx]
    changed[idx] = Rule(rule_id=r.rule_id, version=r.version, priority=r.priority + 1,
                        when=r.when, decision=r.decision, reason_code=r.reason_code,
                        required_clause_ids=r.required_clause_ids, stage=r.stage)
    d1 = _engine(base).decide(tenant_id="t", intent=Intent.RETURN_REFUND, facts=_facts(), as_of=AS_OF)
    d2 = _engine(changed).decide(tenant_id="t", intent=Intent.RETURN_REFUND, facts=_facts(), as_of=AS_OF)
    assert d1.ruleset_hash and len(d1.ruleset_hash) == 64
    assert d1.ruleset_hash != d2.ruleset_hash          # 内容变了，指纹必须变
    assert d1.ruleset_hash == _engine(base).decide(
        tenant_id="t", intent=Intent.RETURN_REFUND, facts=_facts(category="FRESH"), as_of=AS_OF).ruleset_hash


def test_trace_reproducibility_fields():
    d = _engine().decide(tenant_id="t", intent=Intent.RETURN_REFUND, facts=_facts(), as_of=AS_OF)
    assert d.ruleset_version == "2026.10-jd"
    assert d.facts_snapshot_id and len(d.facts_snapshot_id) == 64
    assert d.evaluated_at == AS_OF
    assert d.time_basis == "claim_at"
    assert d.trace["ruleset_hash"] == d.ruleset_hash