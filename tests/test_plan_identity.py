# -*- coding: utf-8 -*-
"""计划身份、命令幂等与资格咨询的回归测试（不依赖 DB / LLM）。

针对第四次复核提出的三个工程风险：
  1. plan_hash 是否包含订单身份（不同订单不得共享指纹）；
  2. 命令幂等键是否包含操作类型（退款 vs 退货退款不得碰撞）；
  3. 报价过期 / 金额变化后是否仍强制复核；
  4. 资格咨询是否被确定性识别并跳过写路径。
"""
from __future__ import annotations

import pytest

from app.bootstrap import build_deps
from app.config import Settings
from app.domain.canonical import payload_hash
from app.domain.enums import Action, Intent, SpeechAct
from app.domain.schemas import ActionPlan, ActionProposal
from app.graph import nodes
from app.graph.eligibility import detect_eligibility_query, refund_only_tiebreak


def _deps():
    return build_deps(Settings(_env_file=None, app_env="test", database_url="memory://", llm_model=""))


def _plan(order_id: str = "o_1001") -> ActionPlan:
    return ActionPlan(order_id=order_id, actions=[
        ActionProposal(action=Action.REQUEST_REFUND_ONLY, order_id=order_id, sku_ids=["sku_1"])])


QUOTE_A = {"amount_minor": 1000, "currency": "CNY", "order_version": "v1",
           "quote_id": "q-aaa", "reservation_reference": "res-aaa"}
QUOTE_B = {"amount_minor": 1000, "currency": "CNY", "order_version": "v1",
           "quote_id": "q-bbb", "reservation_reference": "res-bbb"}


def test_plan_fingerprint_ignores_random_ids():
    """同一业务内容、不同随机报价/预占 ID -> 同一指纹（重试必须复用）。"""
    h1 = payload_hash(nodes._plan_fingerprint(_plan(), [{"order_line_id": "l1"}], QUOTE_A, "v1"))
    h2 = payload_hash(nodes._plan_fingerprint(_plan(), [{"order_line_id": "l1"}], QUOTE_B, "v1"))
    assert h1 == h2


def test_plan_fingerprint_is_order_scoped():
    """行项目结构、金额完全相同，但订单不同 -> 指纹必须不同。"""
    h1 = payload_hash(nodes._plan_fingerprint(_plan("o_1001"), [{"order_line_id": "l1"}], QUOTE_A, "v1"))
    h2 = payload_hash(nodes._plan_fingerprint(_plan("o_2002"), [{"order_line_id": "l1"}], QUOTE_A, "v1"))
    assert h1 != h2


def test_plan_fingerprint_tracks_amount_change():
    """金额变化必须改变指纹：否则旧审批会被拿去执行新金额。"""
    changed = {**QUOTE_A, "amount_minor": 1200}
    h1 = payload_hash(nodes._plan_fingerprint(_plan(), [{"order_line_id": "l1"}], QUOTE_A, "v1"))
    h2 = payload_hash(nodes._plan_fingerprint(_plan(), [{"order_line_id": "l1"}], changed, "v1"))
    assert h1 != h2


def test_command_key_distinguishes_operation_type():
    """退款与退货退款共用注册表模板，幂等键必须靠操作类型区分，不能互相吞掉。"""
    state = {"tenant_id": "t1", "ticket_id": "tk1", "plan_hash": "h1",
             "slots": {}, "order_snapshot": {"order_id": "o_1001"}}
    only_refund = nodes._command_idempotency_key("REQUEST_REFUND_ONLY", {"order_id": "o_1001"}, state)
    return_refund = nodes._command_idempotency_key("REQUEST_RETURN_REFUND", {"order_id": "o_1001"}, state)
    assert only_refund != return_refund


def test_command_key_is_tenant_and_plan_scoped():
    base = {"tenant_id": "t1", "ticket_id": "tk1", "plan_hash": "h1",
            "slots": {}, "order_snapshot": {"order_id": "o_1001"}}
    k = nodes._command_idempotency_key("REQUEST_REFUND_ONLY", {"order_id": "o_1001"}, base)
    assert k != nodes._command_idempotency_key(
        "REQUEST_REFUND_ONLY", {"order_id": "o_1001"}, {**base, "tenant_id": "t2"})
    assert k != nodes._command_idempotency_key(
        "REQUEST_REFUND_ONLY", {"order_id": "o_1001"}, {**base, "plan_hash": "h2"})


@pytest.mark.parametrize("text,expected", [
    ("订单 ts060 第16天了还能换货吗", (Intent.RESHIPMENT, "EXCHANGE")),
    ("买的东西还能换新吗", (Intent.RESHIPMENT, "EXCHANGE")),
    ("退货期限是多少天内", (Intent.RETURN_REFUND, "RETURN")),
    ("这种情况下可以退款吗", (Intent.REFUND_ONLY, "REFUND")),
])
def test_eligibility_query_detected(text, expected):
    det = detect_eligibility_query(text)
    assert det is not None, text
    assert (det.intent, det.subject) == expected


@pytest.mark.parametrize("text", [
    "帮我换货",                 # 明确执行
    "我要退货退款",             # 明确执行
    "换货还是退货，你们看着办",   # 并列方案 -> 澄清
    "尺码不合适换或者退",        # 并列方案 -> 澄清
    "退款多久到账",             # 进度类问句
    "能不能中途拦下来我不要了退钱",  # 拦截是具体动作
    "只退款可以吗货我不要了",      # 已做出决定
])
def test_non_eligibility_texts_not_misclassified(text):
    assert detect_eligibility_query(text) is None


def test_eligibility_skips_planner_and_commands():
    state = {"evidence_status": "OK",
             "intent_result": {"intent": "RESHIPMENT", "speech_act": SpeechAct.ELIGIBILITY_QUERY.value}}
    assert nodes.route_after_evidence(state) == "answer"
    # 正常写路径不受影响
    assert nodes.route_after_evidence({**state, "intent_result": {
        "intent": "RESHIPMENT", "speech_act": SpeechAct.DECIDE.value}}) == "plan"


def test_refund_only_tiebreak_stabilizes_boundary_wording():
    """“尺码不合适…直接退款”不得在 RETURN_REFUND / REFUND_ONLY 之间摆动。"""
    assert refund_only_tiebreak("订单 ts002 尺码不合适，第7天了还能退吗，直接退款",
                                Intent.RETURN_REFUND) == Intent.REFUND_ONLY
    # 明确提到“退货”时不覆盖
    assert refund_only_tiebreak("我要退货退款", Intent.RETURN_REFUND) is None


def test_eligibility_response_type_is_answer_not_succeeded():
    allow = {"policy_decision": {"decision": "ALLOW"},
             "intent_result": {"speech_act": SpeechAct.ELIGIBILITY_QUERY.value}}
    assert nodes.classify_response(allow) == "ANSWER"
    assert nodes.classify_response({**allow, "policy_decision": {"decision": "DENY"}}) == "DENIED"


@pytest.mark.asyncio
async def test_amount_or_reservation_change_after_approval_blocks_execution():
    """审批后金额/预占变化必须拦截，不能因为 plan_hash 相同就复用旧授权。"""
    deps = _deps()
    plan = _plan().model_dump(mode="json")
    base = {"action_plan": plan, "plan_hash": "h1", "quote_id": "q1", "quote_order_version": "v1",
            "policy_decision": {"decision": "ALLOW", "authorized_actions": ["REQUEST_REFUND_ONLY"]},
            "quote_amount_minor": 1000, "reservation_ref": "res-1", "reservation_amount_minor": 1000}
    mismatch = {**base, "reservation_amount_minor": 900}
    assert (await nodes.pre_execution_guard(mismatch, deps))["error_code"] == "RESERVATION_MISMATCH"


@pytest.mark.asyncio
async def test_expired_quote_requotes_then_escalates_to_manual():
    deps = _deps()
    plan = _plan().model_dump(mode="json")
    base = {"action_plan": plan, "plan_hash": "h1", "quote_id": "q1", "quote_order_version": "v1",
            "policy_decision": {"decision": "ALLOW", "authorized_actions": ["REQUEST_REFUND_ONLY"]},
            "quote_amount_minor": 1000, "reservation_ref": "res-1", "reservation_amount_minor": 1000,
            "quote_expires_at": "2000-01-01T00:00:00Z"}
    first = await nodes.pre_execution_guard(base, deps)
    assert first["error_code"] == "QUOTE_EXPIRED" and first["requote_count"] == 1
    assert nodes.route_after_guard(first) == "reprepare"

    exhausted = await nodes.pre_execution_guard({**base, "requote_count": deps.settings.max_requote_cycles}, deps)
    assert exhausted["error_code"] == "REQUOTE_EXCEEDED"
    assert nodes.route_after_guard(exhausted) == "manual"
