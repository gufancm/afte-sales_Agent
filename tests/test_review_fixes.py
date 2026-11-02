# -*- coding: utf-8 -*-
"""针对评审意见 6 项修正的回归测试（不依赖 DB）。

覆盖：
  - 风控按动作取规则，未知/非退款动作不得默认 AUTO_APPROVE
  - PreExecutionGuard 对非退款动作也复核，且时间比较走 datetime
  - ActionPreparation 用 OMS 订单行 ID（不得用 evidence_ids 冒充 order_line_id）
  - ActionPreparation 回写 plan_id
  - ResultVerifier 按 command_id 核验并幂等创建对账任务
  - ResponseComposer 只使用与命中规则绑定的版本化文案
  - 图结构：12 个节点，且不再包含对账节点
"""
from __future__ import annotations

import pytest

pytest.importorskip("langgraph")

from app.bootstrap import build_deps  # noqa: E402
from app.config import Settings  # noqa: E402
from app.domain.enums import Action, RiskDecision  # noqa: E402
from app.domain.schemas import ActionPlan, ActionProposal  # noqa: E402
from app.graph import nodes  # noqa: E402
from app.graph.build import build_graph  # noqa: E402
from app.mcp.ports import LogisticsSnapshot  # noqa: E402
from app.risk_rules import evaluate_risk  # noqa: E402

ORDER_SNAPSHOT = {"order_id": "o_1001", "status": "PAID_UNSHIPPED", "version": "v1", "category": "FRESH",
                  "currency": "CNY", "lines": [{"order_line_id": "l1", "sku_id": "sku_1", "quantity": 1}]}
FINANCE_SNAPSHOT = {"order_id": "o_1001", "refundable_balance_minor": 20000,
                    "reserved_amount_minor": 0, "currency": "CNY"}


def _deps():
    settings = Settings(_env_file=None, app_env="test", database_url="memory://", llm_model="")
    return build_deps(settings)


def _state(**over):
    state = {"tenant_id": "t1", "user_id": "u_demo", "ticket_id": "tk1", "thread_id": "th1",
             "run_id": "r1", "trace_id": "tr1", "messages": [{"role": "user", "content": "我要退货"}],
             "slots": {"order_id": "o_1001", "quantity": 1},
             "policy_decision": {"decision": "ALLOW", "authorized_actions": [
                 "REQUEST_REFUND_ONLY", "REQUEST_RETURN_REFUND", "CREATE_RESHIPMENT", "INTERCEPT_PARCEL"]},
             "order_snapshot": dict(ORDER_SNAPSHOT), "finance_snapshot": dict(FINANCE_SNAPSHOT)}
    state.update(over)
    return state


def _plan(*actions) -> dict:
    return ActionPlan(order_id="o_1001", actions=list(actions)).model_dump(mode="json")


class _RecordingFinance:
    """记录 Finance 收到的报价明细，用于断言订单行 ID 的来源。"""

    def __init__(self, inner):
        self.inner = inner
        self.items: list[dict] | None = None

    async def calculate_refund_amount(self, *, items, **kw):
        self.items = items
        return await self.inner.calculate_refund_amount(items=items, **kw)

    async def get_refundable_balance(self, **kw):
        return await self.inner.get_refundable_balance(**kw)

    async def get_refund_status(self, **kw):
        return await self.inner.get_refund_status(**kw)

    async def get_reconciliation_result(self, **kw):
        return await self.inner.get_reconciliation_result(**kw)


# --------------------------------------------------------------------------- #
# 1) 风控：缺规则/缺事实 -> MANUAL
# --------------------------------------------------------------------------- #
def test_risk_unknown_action_defaults_to_manual():
    for action in (None, "ESCALATE_MANUAL", "CLOSE_TICKET"):
        outcome = evaluate_risk(action, _state())
        assert outcome.decision == RiskDecision.MANUAL, action
        assert outcome.reason_codes


def test_risk_intercept_requires_waybill_and_status():
    missing_waybill = _state(logistics_snapshot={"waybill_no": None, "parcel_status": "IN_TRANSIT"})
    assert evaluate_risk("INTERCEPT_PARCEL", missing_waybill).decision == RiskDecision.MANUAL

    unknown_status = _state(logistics_snapshot={"waybill_no": "wb_1", "parcel_status": "UNKNOWN"})
    assert evaluate_risk("INTERCEPT_PARCEL", unknown_status).decision == RiskDecision.MANUAL

    delivered = _state(logistics_snapshot={"waybill_no": "wb_1", "parcel_status": "SIGNED"})
    assert evaluate_risk("INTERCEPT_PARCEL", delivered).decision == RiskDecision.DENY

    ok = _state(logistics_snapshot={"waybill_no": "wb_1", "parcel_status": "IN_TRANSIT"})
    assert evaluate_risk("INTERCEPT_PARCEL", ok).decision == RiskDecision.AUTO_APPROVE


def test_risk_refund_without_quote_is_manual_not_auto_approve():
    outcome = evaluate_risk("REQUEST_REFUND_ONLY", _state(quote_amount_minor=None))
    assert outcome.decision == RiskDecision.MANUAL
    assert "REFUND_QUOTE_MISSING" in outcome.reason_codes


def test_risk_reshipment_quantity_guard():
    plan = _plan(ActionProposal(action=Action.CREATE_RESHIPMENT, order_id="o_1001", sku_ids=["sku_1"]))
    over_qty = _state(action_plan=plan, slots={"order_id": "o_1001", "quantity": 3})
    assert evaluate_risk("CREATE_RESHIPMENT", over_qty).decision == RiskDecision.DENY

    unknown_sku = _state(action_plan=_plan(ActionProposal(action=Action.CREATE_RESHIPMENT, order_id="o_1001",
                                                          sku_ids=["sku_9"])))
    assert evaluate_risk("CREATE_RESHIPMENT", unknown_sku).decision == RiskDecision.DENY

    ok = _state(action_plan=plan)
    assert evaluate_risk("CREATE_RESHIPMENT", ok).decision == RiskDecision.REVIEW


# --------------------------------------------------------------------------- #
# 2) PreExecutionGuard：非退款动作也复核；时间用 datetime 比较
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_guard_checks_non_refund_actions():
    deps = _deps()
    plan = _plan(ActionProposal(action=Action.INTERCEPT_PARCEL, order_id="o_1001", waybill_no="wb_1"))

    no_waybill = _state(action_plan=plan, plan_hash="h1", logistics_snapshot={"waybill_no": None})
    assert (await nodes.pre_execution_guard(no_waybill, deps))["error_code"] == "WAYBILL_MISSING"

    delivered = _state(action_plan=plan, plan_hash="h1",
                       logistics_snapshot={"waybill_no": "wb_2", "parcel_status": "SIGNED"})
    deps.tools.logistics.seed(LogisticsSnapshot(waybill_no="wb_2", parcel_status="SIGNED", version="t2"))
    assert (await nodes.pre_execution_guard(delivered, deps))["error_code"] == "PARCEL_ALREADY_DELIVERED"

    ok = _state(action_plan=plan, plan_hash="h1",
                logistics_snapshot={"waybill_no": "wb_1", "parcel_status": "IN_TRANSIT"})
    assert (await nodes.pre_execution_guard(ok, deps))["error_code"] is None


@pytest.mark.asyncio
async def test_guard_requires_frozen_plan_and_detects_state_drift():
    deps = _deps()
    plan = _plan(ActionProposal(action=Action.REQUEST_REFUND_ONLY, order_id="o_1001", sku_ids=["sku_1"]))
    assert (await nodes.pre_execution_guard(_state(action_plan=plan), deps))["error_code"] == "PLAN_NOT_FROZEN"

    drifted = _state(action_plan=plan, plan_hash="h1", quote_order_version="v0",
                     quote_id="q1", quote_expires_at="2999-01-01T00:00:00Z", quote_amount_minor=1000)
    assert (await nodes.pre_execution_guard(drifted, deps))["error_code"] == "STATE_DRIFT"


@pytest.mark.asyncio
async def test_guard_quote_expiry_uses_datetime():
    deps = _deps()
    plan = _plan(ActionProposal(action=Action.REQUEST_REFUND_ONLY, order_id="o_1001", sku_ids=["sku_1"]))
    base = {"action_plan": plan, "plan_hash": "h1", "quote_id": "q1", "quote_amount_minor": 1000,
            "quote_order_version": "v1", "risk_decision": RiskDecision.AUTO_APPROVE.value,
            "reservation_ref": "res-1", "reservation_amount_minor": 1000}

    expired = _state(**{**base, "quote_expires_at": "2000-01-01T00:00:00Z"})
    assert (await nodes.pre_execution_guard(expired, deps))["error_code"] == "QUOTE_EXPIRED"

    malformed = _state(**{**base, "quote_expires_at": "not-a-timestamp"})
    assert (await nodes.pre_execution_guard(malformed, deps))["error_code"] is None


# --------------------------------------------------------------------------- #
# 3) ActionPreparation：订单行 ID 来自 OMS；plan_id 回写
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_action_preparation_uses_oms_order_line_id_and_returns_plan_id():
    deps = _deps()
    recorder = _RecordingFinance(deps.finance)
    deps.finance = recorder  # type: ignore[assignment]
    plan = _plan(ActionProposal(action=Action.REQUEST_REFUND_ONLY, order_id="o_1001", sku_ids=["sku_1"],
                                evidence_ids=["ev-1", "ev-2"]))
    state = _state(action_plan=plan, policy_decision={"policy_decision_id": "pd1", "decision": "ALLOW",
                                                      "amount_cap": {"cap_minor": 20000},
                                                      "authorized_actions": ["REQUEST_RETURN_REFUND"]})
    update = await nodes.action_preparation(state, deps)

    assert recorder.items == [{"order_line_id": "l1", "sku_id": "sku_1", "quantity": 1}]
    assert all("ev-1" not in str(v) for v in (recorder.items or [{}])[0].values())
    assert update["plan_id"] and update["plan_id"] in deps.persistence.plans
    assert update["quote_id"] and update["refund_request_id"]


@pytest.mark.asyncio
async def test_action_preparation_rejects_unmappable_sku():
    deps = _deps()
    plan = _plan(ActionProposal(action=Action.REQUEST_REFUND_ONLY, order_id="o_1001", sku_ids=["sku_9"]))
    state = _state(action_plan=plan, policy_decision={"decision": "ALLOW",
                                                      "authorized_actions": ["REQUEST_RETURN_REFUND"]})
    update = await nodes.action_preparation(state, deps)
    assert update["error_code"] == "SKU_NOT_IN_ORDER"
    assert deps.persistence.refund_requests == {}


# --------------------------------------------------------------------------- #
# 4) ResultVerifier：按 command_id 核验 + 幂等对账
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_result_verifier_creates_idempotent_reconciliation():
    deps = _deps()
    cid = await deps.persistence.enqueue_command(tenant_id="t1", ticket_id="tk1", run_id="r1",
                                                action=Action.REQUEST_REFUND_ONLY.value, order_id="o_1001",
                                                refund_request_id=None, quote_id=None, quote_hash=None,
                                                waybill_no=None, sku_ids=[], plan_id="p1",
                                                authorization_ref="plan:p1", payload_hash="ph", payload={})
    state = _state(command_ids=[cid])
    first = await nodes.result_verifier(state, deps)
    second = await nodes.result_verifier(state, deps)
    assert first["response_status"] == "WAITING_EXTERNAL"
    assert first["reconciliation_task_id"] == second["reconciliation_task_id"]
    assert len(deps.persistence.reconciliations) == 1


@pytest.mark.asyncio
async def test_result_verifier_flags_failed_command():
    deps = _deps()
    cid = await deps.persistence.enqueue_command(tenant_id="t1", ticket_id="tk1", run_id="r1",
                                                action=Action.INTERCEPT_PARCEL.value, order_id="o_1001",
                                                refund_request_id=None, quote_id=None, quote_hash=None,
                                                waybill_no="wb_1", sku_ids=[], plan_id="p1",
                                                authorization_ref="plan:p1", payload_hash="ph", payload={})
    await deps.persistence.mark_command_result(command_id=cid, tenant_id="t1", result={"status": "UNSUPPORTED"})
    out = await nodes.result_verifier(_state(command_ids=[cid]), deps)
    assert out["response_status"] == "FAILED"


# --------------------------------------------------------------------------- #
# 5) ResponseComposer：文案必须与命中的规则绑定
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_response_composer_uses_rule_bound_template_only():
    deps = _deps()
    bound = _state(policy_decision={"decision": "DENY", "reason_code": "NO_REASON_FRESH_EXEMPT",
                                    "rule_ids": ["R-JD-NR-DENY-FRESH-001"], "policy_version": "2026.10-jd"})
    out = await nodes.response_composer(bound, deps)
    assert out["response_type"] == "DENIED"
    assert "生鲜" in out["final_response"]

    unbound = _state(policy_decision={"decision": "DENY", "reason_code": "NO_REASON_FRESH_EXEMPT",
                                      "rule_ids": ["R-SOMETHING-ELSE"]})
    out2 = await nodes.response_composer(unbound, deps)
    assert out2["response_type"] == "DENIED"
    assert "生鲜" not in out2["final_response"]
    responded = [a for a in deps.persistence.audits if a["event"] == "RESPONDED"]
    assert responded[-1]["payload"]["policy_template"] == {}


# --------------------------------------------------------------------------- #
# 6) 图结构
# --------------------------------------------------------------------------- #
def test_graph_has_twelve_nodes_and_no_reconciliation_node():
    graph = build_graph(_deps())
    names = {n for n in graph.get_graph().nodes if not n.startswith("__")}
    assert len(names) == 12
    assert "reconciliation_scheduler" not in names
    assert "mcp_executor" not in names
    assert {"policy_evidence", "response_composer", "command_submitter"} <= names

def test_ticket_status_mirrors_run_terminal_state():
    """工单状态必须跟随 run 终态，否则运营索引永远停在 NEW（live 冒烟发现）。"""
    from app.api.routes import _ticket_status_for

    assert _ticket_status_for("NEED_INFO", "WAITING_USER") == "NEED_INFO"
    assert _ticket_status_for("DENIED", "COMPLETED") == "REJECTED"
    assert _ticket_status_for("MANUAL", "COMPLETED") == "MANUAL"
    assert _ticket_status_for("PROCESSING", "WAITING_EXTERNAL") == "PROCESSING"
    assert _ticket_status_for("ANSWER", "COMPLETED") == "CLOSED"
    assert _ticket_status_for("SUCCEEDED", "COMPLETED") == "COMPLETED"
    # 审批挂起优先于 response_type
    assert _ticket_status_for("MANUAL", "WAITING_APPROVAL") == "PENDING_APPROVAL"
    # 未知终态不得乱写，交由上层保持原状态
    assert _ticket_status_for(None, "COMPLETED") is None
