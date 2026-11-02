import pytest
from pydantic import ValidationError

from app.domain.enums import Intent, SafetyFlag
from app.domain.schemas import ActionPlan, ActionProposal, IntentResult, SlotSources, validate_plan_against_intent


def _slot_sources():
    return SlotSources(order_id=None, sku_id=None, order_line_id=None, waybill_no=None,
                       reason_code=None, requested_amount_minor=None, quantity=None, product_category=None)


def test_confidence_bounds():
    with pytest.raises(ValidationError):
        IntentResult(intent=Intent.REFUND_ONLY, confidence=1.5,
                     slots={}, slot_sources=_slot_sources())


def test_unknown_adds_ambiguous_flag():
    r = IntentResult(intent=Intent.UNKNOWN, confidence=0.2, slots={}, slot_sources=_slot_sources())
    assert SafetyFlag.AMBIGUOUS in r.safety_flags


def test_unknown_with_security_flag_does_not_add_ambiguous():
    """UNKNOWN 已带明确安全旗标时不再叠加 AMBIGUOUS，避免安全样本被误判为多标。"""
    r = IntentResult(intent=Intent.UNKNOWN, confidence=0.3, slots={}, slot_sources=_slot_sources(),
                     safety_flags=[SafetyFlag.PROMPT_INJECTION])
    assert r.safety_flags == [SafetyFlag.PROMPT_INJECTION]


def test_ambiguous_allowed_with_concrete_intent():
    """执行方案未定（换/退未定）时，允许保留主意图并同时标记 AMBIGUOUS。"""
    r = IntentResult(intent=Intent.RETURN_REFUND, confidence=0.9, slots={},
                     slot_sources=_slot_sources(), safety_flags=[SafetyFlag.AMBIGUOUS])
    assert r.safety_flags == [SafetyFlag.AMBIGUOUS]
    assert r.intent == Intent.RETURN_REFUND


def test_plan_intent_whitelist():
    plan = ActionPlan(order_id="o_1", actions=[ActionProposal(action="CREATE_RESHIPMENT", order_id="o_1")])
    with pytest.raises(ValueError):
        validate_plan_against_intent(plan, Intent.REFUND_ONLY)
    validate_plan_against_intent(plan, Intent.RESHIPMENT)  # 不抛