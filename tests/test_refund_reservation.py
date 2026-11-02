# -*- coding: utf-8 -*-
"""退款报价预占（hold）与 requote 上限回归。

覆盖本轮落地的两个差距：
  ① 报价即预占：Finance 只读报价同时冻结一笔可用余额 hold（`reservation_reference`）；
     额度不足则不预占（fail-closed）；执行时消费 hold；过期由资金侧回收。
  ② requote 有上限：报价/预占过期 -> 回 action_preparation 重报价，超过 `max_requote_cycles` 转人工。
"""
from __future__ import annotations

import pytest

pytest.importorskip("langgraph")

from app.bootstrap import build_deps  # noqa: E402
from app.config import Settings  # noqa: E402
from app.domain.enums import Action, RiskDecision  # noqa: E402
from app.domain.schemas import ActionPlan, ActionProposal  # noqa: E402
from app.graph import nodes  # noqa: E402

ORDER_SNAPSHOT = {"order_id": "o_1001", "status": "PAID_UNSHIPPED", "version": "v1", "category": "FRESH",
                  "currency": "CNY", "lines": [{"order_line_id": "l1", "sku_id": "sku_1", "quantity": 1}]}


def _deps(**over):
    settings = Settings(_env_file=None, app_env="test", database_url="memory://", llm_model="", **over)
    return build_deps(settings)


def _state(**over):
    state = {"tenant_id": "tenant_demo", "user_id": "u_demo", "ticket_id": "tk1", "thread_id": "th1",
             "run_id": "r1", "trace_id": "tr1", "messages": [{"role": "user", "content": "退款"}],
             "slots": {"order_id": "o_1001", "quantity": 1}, "order_snapshot": dict(ORDER_SNAPSHOT),
             "policy_decision": {"decision": "ALLOW", "policy_decision_id": "pd1",
                                 "amount_cap": {"cap_minor": 20000},
                                 "authorized_actions": ["REQUEST_REFUND_ONLY"]}}
    state.update(over)
    return state


def _plan(*actions) -> dict:
    return ActionPlan(order_id="o_1001", actions=list(actions)).model_dump(mode="json")


# --------------------------------------------------------------------------- #
# ① 报价即预占
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_quote_freezes_hold_and_reports_available():
    deps = _deps()  # o_1001 可退 20000
    raw = deps.tools.finance
    quote = await deps.finance.calculate_refund_amount(
        tenant_id="tenant_demo", refund_request_id="rr-1", order_id="o_1001",
        items=[{"order_line_id": "l1", "sku_id": "sku_1", "quantity": 1}],
        policy_decision_id="pd1", order_version="v1")

    assert quote["reservation_reference"]
    assert quote["reservation_expires_at"]
    assert quote["available_balance_minor"] == 20000 - quote["amount_minor"]
    bal = await deps.finance.get_refundable_balance(tenant_id="tenant_demo", order_id="o_1001")
    assert bal.reserved_amount_minor == quote["amount_minor"]  # 预占已计入 reserved
    assert bal.refundable_balance_minor == 20000


@pytest.mark.asyncio
async def test_second_hold_sees_reduced_available_and_fails_closed():
    deps = _deps()
    await deps.finance.calculate_refund_amount(
        tenant_id="tenant_demo", refund_request_id="rr-1", order_id="o_1001",
        items=[{"order_line_id": "l1", "sku_id": "sku_1", "quantity": 20}],  # 20*1000=20000，占满
        policy_decision_id="pd1", order_version="v1")
    second = await deps.finance.calculate_refund_amount(
        tenant_id="tenant_demo", refund_request_id="rr-2", order_id="o_1001",
        items=[{"order_line_id": "l1", "sku_id": "sku_1", "quantity": 1}],
        policy_decision_id="pd1", order_version="v1")
    assert second["reservation_reference"] is None  # 额度不足 -> 不预占
    assert second["breakdown"]["reason"] == "INSUFFICIENT_AVAILABLE_BALANCE"


@pytest.mark.asyncio
async def test_execute_refund_consumes_hold():
    deps = _deps()
    quote = await deps.finance.calculate_refund_amount(
        tenant_id="tenant_demo", refund_request_id="rr-1", order_id="o_1001",
        items=[{"order_line_id": "l1", "sku_id": "sku_1", "quantity": 1}],
        policy_decision_id="pd1", order_version="v1")
    res = await deps.tools.finance.execute_refund(
        tenant_id="tenant_demo", refund_request_id="rr-1", quote_id=quote["quote_id"],
        quote_hash=quote["quote_hash"], authorization_ref="plan:p1", idempotency_key="idem-1",
        payload_hash="ph")
    assert res["status"] == "SUCCEEDED"
    assert res["reservation_reference"] == quote["reservation_reference"]
    bal = await deps.finance.get_refundable_balance(tenant_id="tenant_demo", order_id="o_1001")
    assert bal.refundable_balance_minor == 20000 - quote["amount_minor"]  # 已实扣
    assert bal.reserved_amount_minor == 0                                  # hold 已释放


@pytest.mark.asyncio
async def test_execute_refund_rejects_quote_hash_mismatch():
    deps = _deps()
    quote = await deps.finance.calculate_refund_amount(
        tenant_id="tenant_demo", refund_request_id="rr-1", order_id="o_1001",
        items=[{"order_line_id": "l1", "sku_id": "sku_1", "quantity": 1}],
        policy_decision_id="pd1", order_version="v1")
    with pytest.raises(ValueError):
        await deps.tools.finance.execute_refund(
            tenant_id="tenant_demo", refund_request_id="rr-1", quote_id=quote["quote_id"],
            quote_hash="sha256:tampered", authorization_ref="plan:p1", idempotency_key="idem-x",
            payload_hash="ph")


@pytest.mark.asyncio
async def test_action_preparation_binds_and_reports_hold():
    deps = _deps()
    plan = ActionPlan(order_id="o_1001", actions=[
        ActionProposal(action=Action.REQUEST_REFUND_ONLY, order_id="o_1001", sku_ids=["sku_1"])])
    state = _state(action_plan=plan.model_dump(mode="json"), plan_id="p1")
    out = await nodes.action_preparation(state, deps)
    assert out["reservation_ref"] and out["reservation_expires_at"]
    assert out["reservation_amount_minor"] == out["quote_amount_minor"]
    events = [a["event"] for a in deps.persistence.audits]
    assert "REFUND_RESERVED" in events and "PLAN_FROZEN" in events


@pytest.mark.asyncio
async def test_action_preparation_fails_closed_when_no_available_balance():
    deps = _deps()
    deps.tools.finance.seed("o_1001", 0)  # 无可退余额
    plan = ActionPlan(order_id="o_1001", actions=[
        ActionProposal(action=Action.REQUEST_REFUND_ONLY, order_id="o_1001", sku_ids=["sku_1"])])
    out = await nodes.action_preparation(_state(action_plan=plan.model_dump(mode="json"), plan_id="p1"), deps)
    assert out["error_code"] == "BALANCE_INSUFFICIENT"
    assert "REFUND_RESERVATION_FAILED" in [a["event"] for a in deps.persistence.audits]


# --------------------------------------------------------------------------- #
# ② 执行前复核：hold 校验 + requote 上限
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_guard_requires_reservation():
    deps = _deps()
    plan = _plan(ActionProposal(action=Action.REQUEST_REFUND_ONLY, order_id="o_1001", sku_ids=["sku_1"]))
    state = _state(action_plan=plan, plan_hash="h1", quote_id="q1", quote_amount_minor=1000,
                   risk_decision=RiskDecision.AUTO_APPROVE.value)
    assert (await nodes.pre_execution_guard(state, deps))["error_code"] == "RESERVATION_MISSING"


@pytest.mark.asyncio
async def test_guard_detects_reservation_expiry_and_routes_reprepare():
    deps = _deps()
    plan = _plan(ActionProposal(action=Action.REQUEST_REFUND_ONLY, order_id="o_1001", sku_ids=["sku_1"]))
    state = _state(action_plan=plan, plan_hash="h1", quote_id="q1", quote_amount_minor=1000,
                   quote_order_version="v1", reservation_ref="res-1", reservation_amount_minor=1000,
                   quote_expires_at="2999-01-01T00:00:00Z",
                   reservation_expires_at="2000-01-01T00:00:00Z",
                   risk_decision=RiskDecision.AUTO_APPROVE.value)
    out = await nodes.pre_execution_guard(state, deps)
    assert out["error_code"] == "RESERVATION_EXPIRED"
    assert out["requote_count"] == 1
    assert nodes.route_after_guard(out) == "reprepare"


@pytest.mark.asyncio
async def test_requote_is_bounded_then_manual():
    deps = _deps(max_requote_cycles=1)
    plan = _plan(ActionProposal(action=Action.REQUEST_REFUND_ONLY, order_id="o_1001", sku_ids=["sku_1"]))
    base = {"action_plan": plan, "plan_hash": "h1", "quote_id": "q1", "quote_amount_minor": 1000,
            "quote_order_version": "v1", "reservation_ref": "res-1", "reservation_amount_minor": 1000,
            "quote_expires_at": "2000-01-01T00:00:00Z", "risk_decision": RiskDecision.AUTO_APPROVE.value}

    first = await nodes.pre_execution_guard(_state(**base), deps)
    assert first["error_code"] == "QUOTE_EXPIRED" and first["requote_count"] == 1
    assert nodes.route_after_guard(first) == "reprepare"

    exceeded = await nodes.pre_execution_guard(_state(**{**base, "requote_count": 1}), deps)
    assert exceeded["error_code"] == "REQUOTE_EXCEEDED"
    assert nodes.route_after_guard(exceeded) == "manual"