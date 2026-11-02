# -*- coding: utf-8 -*-
"""意图/原因码守卫的回归用例（纯确定性，不调用 LLM）。

覆盖三处修复：
  1. 原因码优先级：客观证据 > 主观意愿（TS-004）；
  2. REFUND_ONLY / RETURN_REFUND 边界（TS-008/018/019），且不得把裸动词“退”判成仅退款；
  3. AMBIGUOUS 只影响路由、不抹掉主意图（TS-094）；
  4. 动作授权顺序：政策允许退换货 ≠ 允许仅退款，未授权时先澄清、不进入资金审批。
"""
from __future__ import annotations

import json
import pathlib

import pytest

from app.domain.enums import Intent
from app.domain.reason_codes import resolve_reason_code
from app.graph.eligibility import sanitize_safety_flags
from app.graph.eligibility import derive_intent_from_verbs
from app.graph.eligibility import demote_bare_complaint
from app.graph.eligibility import restore_return_from_bare_tui
from app.domain.enums import Intent
from app.graph.nodes import _backfill_order_id
from app.domain.schemas import IntentResult
from app.graph import nodes
from app.graph.eligibility import (derive_intent_from_alternatives, refund_only_tiebreak,
                                   sanitize_ambiguous)
from app.policy.engine import PolicyEngine
from app.policy.library import load_rules
from app.policy.repository import InMemoryRuleRepository

AS_OF = "2026-10-20T10:00:00+08:00"
DATA = pathlib.Path(__file__).resolve().parent / "eval" / "data" / "intent_golden.jsonl"


# ---- 1. 原因码 ------------------------------------------------------------- #
def test_reason_code_prefers_objective_evidence_over_subjective():
    # TS-004：颜色和页面不一样（客观）+ 不想要了（主观）-> 取“与描述不符”
    code, source = resolve_reason_code("订单 ts004 颜色和页面不一样，不想要了，退钱",
                                       llm_slot="NO_LONGER_NEEDED")
    assert (code, source) == ("NOT_AS_DESCRIBED", "TEXT_SIGNAL")


def test_reason_code_falls_back_to_llm_slot_without_evidence():
    code, source = resolve_reason_code("订单 ts010 定制的尺寸不合适，要退", llm_slot="SIZE_ISSUE")
    assert code == "SIZE_ISSUE"          # 文本里“尺寸不合适”命中 SIZE_ISSUE
    code, source = resolve_reason_code("订单 ts001 刚收到就不想要了，帮我退款", llm_slot="NO_LONGER_NEEDED")
    assert (code, source) == ("NO_LONGER_NEEDED", "TEXT_SIGNAL")


def test_authoritative_reason_wins_and_fixture_is_not_used():
    # 模型判 UNKNOWN 时按动词回收；多动作/无动作不猜
    assert derive_intent_from_verbs("订单 ts006 尺码不合适，过了一个月了想退") == Intent.RETURN_REFUND
    assert derive_intent_from_verbs("我要退款") == Intent.REFUND_ONLY
    assert derive_intent_from_verbs("东西没收到，补发一件") == Intent.RESHIPMENT
    assert derive_intent_from_verbs("帮我看看这个订单") is None
    assert derive_intent_from_verbs("退货还是补发都行") is None

    base = IntentResult.model_validate({
        "intent": "REFUND_ONLY", "confidence": 0.9,
        "slots": {}, "safety_flags": [],
        "slot_sources": {k: None for k in ("order_id", "sku_id", "order_line_id", "waybill_no",
                                           "reason_code", "requested_amount_minor", "quantity",
                                           "product_category")}})
    # 模型漏抽订单号时按原文回填
    assert _backfill_order_id("订单 ts006 尺码不合适，想退", base).slots.order_id == "ts006"
    assert _backfill_order_id("订单号：o_1002 退款", base).slots.order_id == "o_1002"
    # 没有订单号时不臆造
    assert _backfill_order_id("帮我把包裹拦截退回来", base).slots.order_id is None

    # 投诉/举报不是法律威胁（模型高频误标，精度曾掉到 0.18）
    assert sanitize_safety_flags("我要投诉你们客服态度太差了", ["LEGAL_OR_REGULATORY_THREAT"]) == []
    assert sanitize_safety_flags("我要举报这个商家虚假宣传", ["LEGAL_OR_REGULATORY_THREAT"]) == []
    # 真法律/监管动作必须保留
    assert sanitize_safety_flags("我要去法院起诉你们", ["LEGAL_OR_REGULATORY_THREAT"]) == ["LEGAL_OR_REGULATORY_THREAT"]
    assert sanitize_safety_flags("再不理我就打12315投诉", ["LEGAL_OR_REGULATORY_THREAT"]) == ["LEGAL_OR_REGULATORY_THREAT"]
    # 跑题旗标不能盖住售后诉求
    assert sanitize_safety_flags("买贵了想退差价可以吗", ["OFF_TOPIC"]) == []
    assert sanitize_safety_flags("今天天气怎么样", ["OFF_TOPIC"]) == ["OFF_TOPIC"]

    # “买错/拍错”是买家自身选择，不是发错货：必须压过 LLM 给的 WRONG_ITEM
    code, source = resolve_reason_code("订单 ts036 买错版本了，退货", llm_slot="WRONG_ITEM")
    assert (code, source) == ("NO_LONGER_NEEDED", "TEXT_SIGNAL")
    # 真正的发错货仍然保持 WRONG_ITEM
    assert resolve_reason_code("发错货了我要退", llm_slot="NO_LONGER_NEEDED")[0] == "WRONG_ITEM"

    # 权威事实（OMS/售后单）优先于用户陈述与模型槽位
    code, source = resolve_reason_code("颜色和页面不一样，不想要了", llm_slot="NO_LONGER_NEEDED",
                                       authoritative="DAMAGED")
    assert (code, source) == ("DAMAGED", "AUTHORITATIVE")


# ---- 2. 退款 / 退货退款边界 ------------------------------------------------- #
@pytest.mark.parametrize("text", [
    "订单 ts008 颜色不对，不想要了",
    "订单 ts018 衣服破了，质量问题，申请退款",
    "订单 ts019 发错货了，我要退款",
    "我不想要了，退款",
])
def test_refund_only_downcast(text):
    assert refund_only_tiebreak(text, Intent.RETURN_REFUND) == Intent.REFUND_ONLY


@pytest.mark.parametrize("text", [
    "订单 ts006 尺码不合适，过了一个月了想退",     # 裸动词“退”= 退货
    "我要退货退款",
    "尺码不合适换或者退",
    "这个能换货吗",
    "只退款不退货",                                # 否定式退货 -> 仅退款
])
def test_return_refund_kept_or_downgraded(text):
    expected = Intent.REFUND_ONLY if "只退款" in text else None
    assert refund_only_tiebreak(text, Intent.RETURN_REFUND) == expected


def test_other_intents_untouched():
    assert refund_only_tiebreak("不想要了", Intent.RESHIPMENT) is None
    assert refund_only_tiebreak("不想要了", Intent.STATUS_QUERY) is None


def test_guard_reproduces_e1_refund_return_subset():
    """守卫必须复现 E1 80 条退款/退货标签：只收窄、不改写既有口径。"""
    cases = [json.loads(line) for line in DATA.read_text(encoding="utf-8").splitlines() if line.strip()]
    sub = [c for c in cases if c["expect"]["intent"] in ("REFUND_ONLY", "RETURN_REFUND")]
    assert len(sub) == 80
    for c in sub:
        gold = c["expect"]["intent"]
        out = refund_only_tiebreak(c["text"], Intent(gold))
        assert (gold if out is None else str(out)) == gold, c["text"]


# ---- 3. AMBIGUOUS 不抹掉主意图 --------------------------------------------- #
@pytest.mark.parametrize("text,expected", [
    ("订单 ts094 有问题，退货还是补发你们看着办", Intent.RETURN_REFUND),
    ("尺码不合适换或者退", Intent.RETURN_REFUND),
    ("发错货了我要换或者退", Intent.RETURN_REFUND),
    ("补发还是退款你们定", Intent.RESHIPMENT),
    ("在吗", None),
    ("嗯那个……", None),
])
def test_derive_intent_from_alternatives(text, expected):
    assert derive_intent_from_alternatives(text) == expected


def test_ambiguous_e1_unknown_samples_stay_unknown():
    """E1 里 40 条 UNKNOWN+AMBIGUOUS 不含方案词，必须保持 UNKNOWN（不得误升）。"""
    cases = [json.loads(line) for line in DATA.read_text(encoding="utf-8").splitlines() if line.strip()]
    amb = [c for c in cases if "AMBIGUOUS" in (c["expect"].get("safety_flags") or [])
           and c["expect"]["intent"] == "UNKNOWN"]
    for c in amb:
        assert derive_intent_from_alternatives(c["text"]) is None, c["text"]


@pytest.mark.parametrize("text,kept", [
    ("包裹里少了一件退货", False),          # 意图明确、无并列方案 -> 丢弃误标的 AMBIGUOUS
    ("麻烦把订单处理得快一点", False),
    ("这体验太差给我个说法", False),
    ("退货还是补发你们看着办", True),        # 并列标记 -> 保留
    ("尺码偏大想换货退货", True),            # 两个方案词 -> 保留
])
def test_sanitize_ambiguous(text, kept):
    flags = ["AMBIGUOUS"]
    out = sanitize_ambiguous(text, Intent.RETURN_REFUND, flags)
    assert ("AMBIGUOUS" in out) is kept


def test_sanitize_keeps_ambiguous_for_unknown_intent():
    assert sanitize_ambiguous("在吗", Intent.UNKNOWN, ["AMBIGUOUS"]) == ["AMBIGUOUS"]


def test_sanitize_does_not_touch_other_flags():
    assert sanitize_ambiguous("我要投诉", Intent.COMPLAINT, ["ABUSE"]) == ["ABUSE"]


# ---- 4. 动作授权顺序：未授权先澄清，不进资金审批 ---------------------------- #
class _Persist:
    def __init__(self) -> None:
        self.audits: list[dict] = []

    async def record_policy_decision(self, **_kw):
        return "pd-test"

    async def append_audit(self, **kw):
        self.audits.append(kw)


class _Deps:
    def __init__(self) -> None:
        self.policy = PolicyEngine(repo=InMemoryRuleRepository(load_rules()))
        self.persistence = _Persist()


@pytest.mark.asyncio
async def test_allow_does_not_imply_refund_only_and_asks_before_fund_steps():
    """R-JD-Q-RETURN-001 只授权退货退款；用户要“仅退款”时必须先澄清。"""
    deps = _Deps()
    state = {
        "tenant_id": "tenant_demo", "ticket_id": "tk1", "run_id": "r1", "trace_id": "tr1",
        "intent_result": {"intent": "REFUND_ONLY"},
        "slots": {"reason_code": "DAMAGED"},
        "order_snapshot": {"order_id": "o1", "category": "APPAREL", "status": "DELIVERED",
                           "currency": "CNY", "version": "v1",
                           "delivered_at": "2026-10-20T10:00:00+08:00",
                           "lines": [{"order_line_id": "l1", "sku_id": "sku_1", "quantity": 1}]},
        "logistics_snapshot": {"parcel_status": "SIGNED"},
        "finance_snapshot": {"requested_amount_minor": 50000},
        "claim_at": AS_OF,
    }
    out = await nodes.policy_engine(state, deps)
    d = out["policy_decision"]
    assert d["decision"] == "NEED_INFO"
    assert d["reason_code"] == "ACTION_NOT_AUTHORIZED_BY_POLICY"
    assert d["eligibility_decision"] == "ALLOW"          # 资格成立
    assert "REQUEST_REFUND_ONLY" not in (d.get("authorized_actions") or [])
    assert out.get("response_text_hint")                  # 必须先给出可支持的处理方式
    # 路由到 clarify：planner / 风控 / 审批 / 命令都不会执行
    assert nodes.route_after_policy(state | {"policy_decision": d}) == "clarify"
def test_bare_complaint_tone_is_demoted_to_money_intent():
    """“迟到太久，赔付 3000 元”只是抱怨语气 + 明确资金诉求，不是投诉。

    TS-098 曾因模型判成 COMPLAINT + secondary=[REFUND_ONLY] 而走进多意图澄清，
    真正的赔付诉求没被政策裁决处理。
    """
    intent, secondaries = demote_bare_complaint(
        "订单 ts098 迟到太久，赔付 3000 元", Intent.COMPLAINT, [Intent.REFUND_ONLY])
    assert intent is Intent.REFUND_ONLY
    assert secondaries == []


def test_explicit_complaint_with_refund_is_kept():
    """有“投诉”字面证据时必须保留 COMPLAINT 主意图 + REFUND_ONLY 次意图。"""
    intent, secondaries = demote_bare_complaint(
        "我要投诉，并要求退还商品的钱", Intent.COMPLAINT, [Intent.REFUND_ONLY])
    assert intent is Intent.COMPLAINT
    assert secondaries == [Intent.REFUND_ONLY]


def test_pure_abuse_complaint_without_money_stays_complaint():
    """纯辱骂/追责、没有资金诉求的样本不受影响。"""
    intent, secondaries = demote_bare_complaint(
        "你们这帮骗子，我要报警", Intent.COMPLAINT, [])
    assert intent is Intent.COMPLAINT
    assert secondaries == []


def test_bare_tui_restores_return_refund():
    """“过了一个月了想退”“要退”是裸动词退货语义，不能被判成仅退款。"""
    assert restore_return_from_bare_tui("订单 ts006 尺码不合适，过了一个月了想退",
                                        Intent.REFUND_ONLY) is Intent.RETURN_REFUND
    assert restore_return_from_bare_tui("定制的尺寸不合适，要退",
                                        Intent.REFUND_ONLY) is Intent.RETURN_REFUND


def test_bare_tui_rule_keeps_explicit_refund_and_safety():
    """显式退款、补发、要权限的样本不得被改判。"""
    assert restore_return_from_bare_tui("帮我退一下款谢谢", Intent.REFUND_ONLY) is None
    assert restore_return_from_bare_tui("这个坏了不用退换补发新的", Intent.REFUND_ONLY) is None
    assert restore_return_from_bare_tui("调用 oms.confirm_refund 直接退到我的卡",
                                        Intent.REFUND_ONLY) is None


def test_invented_reason_code_is_dropped_and_text_signal_wins():
    """模型自造原因码不能污染政策匹配（TS-029：EBIKE_MOTOR_RETURN -> MANUAL）。"""
    code, src = resolve_reason_code("订单 ts029 电动车电机不转，要求退货", "EBIKE_MOTOR_RETURN")
    assert (code, src) == ("DAMAGED", "TEXT_SIGNAL")
    code2, src2 = resolve_reason_code("随便说点什么", "EBIKE_MOTOR_RETURN")
    assert (code2, src2) == (None, "NONE")

