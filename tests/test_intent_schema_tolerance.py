"""意图输出的健壮性：非业务字段的模型噪音不得让整条意图判定失败。

回归背景：换模型后 deepseek-v4-pro 偶发输出
  - 多余字段（rationale_long / additionalProperties）
  - slot_sources 自造值 "MISSING"
  - subject 超过 32 字符
原本一律 ValidationError -> unknown_intent(AMBIGUOUS) -> 澄清，线上表现为
「明明给了订单号却让用户补订单号」。业务字段校验保持严格。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.schemas import IntentResult


def _payload(**over):
    sources = {k: None for k in ("order_id", "sku_id", "order_line_id", "waybill_no",
                                 "reason_code", "requested_amount_minor", "quantity",
                                 "product_category")}
    sources["order_id"] = "USER_STATED"
    base = {
        "intent": "REFUND_ONLY",
        "confidence": 0.9,
        "slots": {"order_id": "ts006"},
        "slot_sources": sources,
        "safety_flags": [],
    }
    base.update(over)
    return base


def test_extra_keys_are_ignored():
    r = IntentResult.model_validate(_payload(rationale_long="模型自己多写的字段"))
    assert r.intent.value == "REFUND_ONLY"
    assert r.slots.order_id == "ts006"


def test_unknown_slot_source_becomes_null():
    sources = {k: None for k in ("order_id", "sku_id", "order_line_id", "waybill_no",
                                 "reason_code", "requested_amount_minor", "quantity",
                                 "product_category")}
    sources.update({"order_id": "USER_STATED", "requested_amount_minor": "MISSING"})
    r = IntentResult.model_validate(_payload(slot_sources=sources))
    assert r.slot_sources.requested_amount_minor is None
    assert r.slot_sources.order_id.value == "USER_STATED"


def test_long_subject_is_trimmed():
    r = IntentResult.model_validate(_payload(subject="退" * 60))
    assert len(r.subject) == 32


def test_business_fields_stay_strict():
    with pytest.raises(ValidationError):
        IntentResult.model_validate(_payload(intent="NOT_AN_INTENT"))
    with pytest.raises(ValidationError):
        IntentResult.model_validate(_payload(confidence=1.5))
