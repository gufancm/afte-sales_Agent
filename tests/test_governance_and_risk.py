# -*- coding: utf-8 -*-
"""治理与风控收口（Batch 1-5）回归：审批角色门 / 事实漂移 / 生效风控维度 /
命令优先级与取消 / 补发权威核验 / 回复多语言与多渠道。
"""
from __future__ import annotations

import pytest

pytest.importorskip("langgraph")

from app.bootstrap import build_deps  # noqa: E402
from app.config import Settings  # noqa: E402
from app.domain.action_registry import priority_of  # noqa: E402
from app.domain.schemas import ActionPlan, ActionProposal  # noqa: E402
from app.domain.enums import Action  # noqa: E402
from app.graph import nodes  # noqa: E402
from app.risk_rules import evaluate_risk  # noqa: E402


ORDER = {"order_id": "o_1001", "status": "PAID_UNSHIPPED", "version": "v1", "category": "FRESH",
         "currency": "CNY", "lines": [{"order_line_id": "l1", "sku_id": "sku_1", "quantity": 1}]}
FINANCE = {"order_id": "o_1001", "refundable_balance_minor": 20000, "reserved_amount_minor": 0,
           "currency": "CNY"}


def _deps():
    return build_deps(Settings(_env_file=None, app_env="test", database_url="memory://", llm_model=""))


def _state(**over):
    st = {"tenant_id": "tenant_demo", "user_id": "u_demo", "ticket_id": "tk1", "thread_id": "th1",
          "run_id": "r1", "trace_id": "tr1", "slots": {"order_id": "o_1001", "quantity": 1},
          "order_snapshot": dict(ORDER), "finance_snapshot": dict(FINANCE)}
    st.update(over)
    return st


# ---------- 1) 审批角色门 ----------
@pytest.mark.asyncio
async def test_decide_approval_enforces_reviewer_role():
    deps = _deps()
    a = await deps.persistence.ensure_approval_task(tenant_id="tenant_demo", ticket_id="tk1", plan_id="p1",
                                                    plan_hash="ph1", quote_id=None, expected_version=None)
    aid = a["approval_id"]
    with pytest.raises(PermissionError):
        await deps.persistence.decide_approval(tenant_id="tenant_demo", approval_id=aid, decision="APPROVED",
                                               reviewer_id="u1", reviewer_role="ops", allowed_roles=["finance"])
    await deps.persistence.decide_approval(tenant_id="tenant_demo", approval_id=aid, decision="APPROVED",
                                           reviewer_id="u2", reviewer_role="finance", comment="ok",
                                           allowed_roles=["finance"])
    v = await deps.persistence.load_and_verify_approval(tenant_id="tenant_demo", approval_id=aid)
    assert v["status"] == "APPROVED" and v["plan_hash"] == "ph1" and v["expired"] is False


# ---------- 2) 执行前事实漂移 ----------
def test_guard_common_detects_facts_version_drift():
    ok = _state(plan_hash="ph1", quote_order_version="v1", plan_frozen_facts_version="fv1",
                facts_version="fv1")
    assert nodes._guard_common(ok) == {}
    drifted = _state(plan_hash="ph1", quote_order_version="v1", plan_frozen_facts_version="fv1",
                     facts_version="fv2")
    assert nodes._guard_common(drifted)["error_code"] == "STATE_DRIFT"


# ---------- 3) 生效风控维度（退款） ----------
def _refund_state(**over):
    base = dict(quote_amount_minor=1000,
                policy_decision={"decision": "ALLOW", "amount_cap": {"cap_minor": 20000}},
                risk_facts={"refund_count_30d": 0, "refund_amount_30d_minor": 0, "blacklisted": False})
    base.update(over)
    return _state(**base)


PARAMS = {"max_refund_count_30d": 5, "max_refund_amount_30d_minor": 500000, "blacklist_deny": True}


def test_risk_refund_requires_stats_when_enforced():
    missing = _state(quote_amount_minor=1000, policy_decision={"decision": "ALLOW",
                                                               "amount_cap": {"cap_minor": 20000}})
    assert evaluate_risk("REQUEST_REFUND_ONLY", missing, PARAMS).decision.value == "MANUAL"


def test_risk_refund_blacklist_and_frequency():
    assert evaluate_risk("REQUEST_REFUND_ONLY", _refund_state(
        risk_facts={"refund_count_30d": 0, "refund_amount_30d_minor": 0, "blacklisted": True}),
        PARAMS).decision.value == "DENY"
    assert evaluate_risk("REQUEST_REFUND_ONLY", _refund_state(
        risk_facts={"refund_count_30d": 9, "refund_amount_30d_minor": 0, "blacklisted": False}),
        PARAMS).decision.value == "REVIEW"
    assert evaluate_risk("REQUEST_REFUND_ONLY", _refund_state(), PARAMS).decision.value == "AUTO_APPROVE"


# ---------- 4) 命令优先级 / 取消 / outbox relay ----------
def test_action_priority_finance_before_logistics():
    assert priority_of("REQUEST_REFUND_ONLY") < priority_of("INTERCEPT_PARCEL") < priority_of("EXPEDITE_DELIVERY")


@pytest.mark.asyncio
async def test_cancel_command_only_when_prepared():
    deps = _deps()
    cid = await deps.persistence.enqueue_command(tenant_id="t1", ticket_id="tk1", run_id="r1",
                                                 action=Action.INTERCEPT_PARCEL.value, order_id="o_1001",
                                                 refund_request_id=None, quote_id=None, quote_hash=None,
                                                 waybill_no="wb_1", sku_ids=[], plan_id="p1",
                                                 authorization_ref="plan:p1", payload_hash="ph", payload={})
    assert await deps.persistence.cancel_command(command_id=cid, tenant_id="t1", reason="USER_CANCEL") is True
    assert deps.persistence.commands[0]["status"] == "CANCELLED"
    await deps.persistence.mark_outbox_published(aggregate_id=cid)
    assert cid in deps.persistence.outbox_published


# ---------- 5) 补发权威核验 ----------
@pytest.mark.asyncio
async def test_result_verifier_confirms_reshipment_via_authority():
    deps = _deps()
    cid = await deps.persistence.enqueue_command(tenant_id="tenant_demo", ticket_id="tk1", run_id="r1",
                                                 action=Action.CREATE_RESHIPMENT.value, order_id="o_1001",
                                                 refund_request_id=None, quote_id=None, quote_hash=None,
                                                 waybill_no=None, sku_ids=["sku_1"], plan_id="p1",
                                                 authorization_ref="plan:p1", payload_hash="ph", payload={})
    await deps.persistence.mark_command_result(command_id=cid, tenant_id="tenant_demo",
                                               result={"status": "SUCCEEDED"})
    deps.tools.orders.seed_reshipment_status("o_1001", "SHIPPED")
    out = await nodes.result_verifier(_state(command_ids=[cid]), deps)
    assert out["response_status"] == "COMPLETED"

    deps.tools.orders.seed_reshipment_status("o_1001", "UNKNOWN")
    out2 = await nodes.result_verifier(_state(command_ids=[cid]), deps)
    assert out2["response_status"] in ("WAITING_EXTERNAL", "COMPLETED")


# ---------- 6) 回复多语言 / 多渠道 / ETA ----------
@pytest.mark.asyncio
async def test_response_composer_i18n_and_channels():
    deps = _deps()
    s = _state(policy_decision={"decision": "NEED_INFO"}, locale="en-US", channels=["app", "sms"],
               action_plan=ActionPlan(order_id="o_1001",
                                      actions=[ActionProposal(action=Action.REQUEST_REFUND_ONLY,
                                                              order_id="o_1001", sku_ids=["sku_1"])],
                                      missing_information=["order number"]).model_dump(mode="json"))
    out = await nodes.response_composer(s, deps)
    assert out["response_type"] == "NEED_INFO"
    assert "order number" in out["final_response"]
    dispatched = [a for a in deps.persistence.audits if a["event"] == "MESSAGE_DISPATCHED"]
    assert {a["payload"]["channel"] for a in dispatched} == {"app", "sms"}


@pytest.mark.asyncio
async def test_response_composer_injects_settlement_eta():
    deps = _deps()
    s = _state(response_status="WAITING_EXTERNAL", quote_amount_minor=1000, quote_currency="CNY",
               settlement_eta="1-3 天")
    out = await nodes.response_composer(s, deps)
    assert out["response_type"] == "PROCESSING"
    assert "预计到账" in out["final_response"] and "10.00" in out["final_response"]
