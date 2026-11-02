# -*- coding: utf-8 -*-
"""多动作授权缺口回归：风控 / 执行前复核 / 命令提交必须覆盖**全部**外部写动作。

历史缺陷：`_primary_action()` 只取第一个外部写动作，而 `command_submitter` 遍历全部写动作；
计划里出现第二个写动作时它会绕过风控与执行前复核（资金/物流漂移漏检）。
"""
from __future__ import annotations

import pytest

pytest.importorskip("langgraph")

from app.bootstrap import build_deps  # noqa: E402
from app.config import Settings  # noqa: E402
from app.domain.enums import INTENT_ALLOWED_ACTIONS, Action  # noqa: E402
from app.domain.schemas import ActionPlan, ActionProposal  # noqa: E402
from app.graph import nodes  # noqa: E402
from app.risk_rules import evaluate_risk  # noqa: E402

ORDER = {"order_id": "o_1001", "status": "PAID_UNSHIPPED", "version": "v1", "category": "FRESH",
         "currency": "CNY", "lines": [{"order_line_id": "l1", "sku_id": "sku_1", "quantity": 1}]}
FINANCE = {"order_id": "o_1001", "refundable_balance_minor": 20000, "reserved_amount_minor": 0,
           "currency": "CNY"}


def _deps():
    return build_deps(Settings(_env_file=None, app_env="test", database_url="memory://", llm_model=""))


def _state(plan, **over):
    st = {"tenant_id": "t1", "user_id": "u_demo", "ticket_id": "tk1", "thread_id": "th1",
          "run_id": "r1", "trace_id": "tr1", "messages": [{"role": "user", "content": "组合诉求"}],
          "slots": {"order_id": "o_1001", "quantity": 1}, "plan_hash": "ph1",
          "order_snapshot": dict(ORDER), "finance_snapshot": dict(FINANCE),
          "risk_facts": {"refund_count_30d": 0, "refund_amount_30d_minor": 0, "blacklisted": False},
          "policy_decision": {"decision": "ALLOW", "amount_cap": None,
                              "authorized_actions": ["REQUEST_REFUND_ONLY", "REQUEST_RETURN_REFUND",
                                                     "CREATE_RESHIPMENT", "INTERCEPT_PARCEL"]},
          "action_plan": plan.model_dump(mode="json")}
    st.update(over)
    return st


def _refund_and_intercept():
    return ActionPlan(order_id="o_1001", actions=[
        ActionProposal(action=Action.REQUEST_REFUND_ONLY, order_id="o_1001", sku_ids=["sku_1"]),
        ActionProposal(action=Action.INTERCEPT_PARCEL, order_id="o_1001", waybill_no="wb_1"),
    ])


# --------------------------------------------------------------------------- #
# 1) 风控：逐动作评估 + 取最严结论
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_risk_covers_every_write_action_and_takes_strictest():
    deps = _deps()
    # 退款：无金额上限 -> REVIEW；拦截：在途 -> AUTO_APPROVE。整体必须取 REVIEW（最严）。
    state = _state(_refund_and_intercept(), quote_amount_minor=1000,
                   logistics_snapshot={"waybill_no": "wb_1", "parcel_status": "IN_TRANSIT"},
                   policy_decision={"decision": "ALLOW", "amount_cap": None})
    out = await nodes.risk_authorization(state, deps)

    assert out["risk_by_action"] == {"REQUEST_REFUND_ONLY": "REVIEW", "INTERCEPT_PARCEL": "AUTO_APPROVE"}
    assert out["risk_decision"] == "REVIEW"
    by_action = deps.persistence.audits[-1]["payload"]["by_action"]
    assert set(by_action) == {"REQUEST_REFUND_ONLY", "INTERCEPT_PARCEL"}
    assert all(":" in c for c in out["risk_reason_codes"])  # reason_codes 带 ACTION: 前缀


@pytest.mark.asyncio
async def test_risk_denies_whole_plan_when_any_action_is_denied():
    deps = _deps()
    # 拦截：已签收 -> DENY；退款：在额度内 -> AUTO_APPROVE。整体必须 DENY。
    state = _state(_refund_and_intercept(), quote_amount_minor=1000,
                   logistics_snapshot={"waybill_no": "wb_2", "parcel_status": "SIGNED"},
                   policy_decision={"decision": "ALLOW", "amount_cap": {"cap_minor": 20000}})
    out = await nodes.risk_authorization(state, deps)

    assert out["risk_by_action"]["INTERCEPT_PARCEL"] == "DENY"
    assert out["risk_by_action"]["REQUEST_REFUND_ONLY"] == "AUTO_APPROVE"
    assert out["risk_decision"] == "DENY"


def test_evaluate_risk_still_manual_for_unknown_action():
    assert evaluate_risk(None, {}).decision.value == "MANUAL"


# --------------------------------------------------------------------------- #
# 2) 执行前复核：逐动作（第二个写动作不再被放过）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_guard_checks_every_write_action_not_just_the_first():
    deps = _deps()
    plan = ActionPlan(order_id="o_1001", actions=[
        ActionProposal(action=Action.REQUEST_REFUND_ONLY, order_id="o_1001", sku_ids=["sku_1"]),
        ActionProposal(action=Action.CREATE_RESHIPMENT, order_id="o_1001", sku_ids=["sku_zzz"]),
    ])
    # 退款侧全部满足（已报价、未过期、余额充足、风控放行），补发侧 SKU 不在订单里
    state = _state(plan, quote_id="q1", quote_amount_minor=1000, quote_order_version="v1",
                   quote_expires_at="2999-01-01T00:00:00Z", risk_decision="AUTO_APPROVE",
                   reservation_ref="res-1", reservation_amount_minor=1000)
    out = await nodes.pre_execution_guard(state, deps)
    assert out["error_code"] == "SKU_NOT_IN_ORDER"  # 旧实现只看退款 -> 会误判通过


@pytest.mark.asyncio
async def test_guard_blocks_unknown_write_action():
    deps = _deps()
    plan = ActionPlan(order_id="o_1001", actions=[
        ActionProposal(action=Action.CLOSE_TICKET, order_id="o_1001")])
    out = await nodes.pre_execution_guard(_state(plan), deps)
    assert out["error_code"] == "UNSUPPORTED_ACTION"


# --------------------------------------------------------------------------- #
# 3) 命令提交：纵深防御（未被风控覆盖即 fail-closed）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_command_submitter_fails_closed_without_risk_coverage():
    deps = _deps()
    plan = ActionPlan(order_id="o_1001", actions=[
        ActionProposal(action=Action.INTERCEPT_PARCEL, order_id="o_1001", waybill_no="wb_1")])

    blocked = await nodes.command_submitter(_state(plan, plan_id="p1"), deps)
    assert blocked["error_code"] == "ACTION_NOT_RISK_AUTHORIZED"
    assert blocked["command_ids"] == [] and deps.persistence.commands == []
    assert deps.persistence.audits[-1]["event"] == "COMMAND_SUBMIT_BLOCKED"

    covered = await nodes.command_submitter(
        _state(plan, plan_id="p1", risk_by_action={"INTERCEPT_PARCEL": "AUTO_APPROVE"}), deps)
    assert len(covered["command_ids"]) == 1 and len(deps.persistence.commands) == 1


# --------------------------------------------------------------------------- #
# 4) 现状约束：每个意图仍最多 1 个外部写动作（多写动作能力打开前的不变量）
# --------------------------------------------------------------------------- #
def test_each_intent_allows_at_most_one_write_action():
    for intent, allowed in INTENT_ALLOWED_ACTIONS.items():
        writes = sorted(a.value for a in allowed if a.value in nodes.EXTERNAL_WRITE_ACTIONS)
        assert len(writes) <= 1, (intent, writes)
