# -*- coding: utf-8 -*-
"""资金安全业务验收：合法闭环、非法阻断、异常恢复。"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone

from app.bootstrap import build_deps
from app.config import Settings
from app.saga.dispatcher import SagaDispatcher


def _deps():
    settings = Settings(_env_file=None, app_env="test", database_url="memory://", llm_model="")
    return build_deps(settings)


def _command(**over):
    command = {
        "command_id": "cmd-1", "tenant_id": "tenant_demo", "ticket_id": "tk1", "run_id": "r1",
        "action": "REQUEST_REFUND_ONLY", "refund_request_id": "rr-1", "order_id": "o_1001",
        "quote_id": "q1", "quote_hash": "qh", "authorization_ref": "approval:a1",
        "idempotency_key": "refund:tenant_demo:tk1:o_1001", "payload_hash": "ph", "payload": {},
    }
    command.update(over)
    return command


@pytest.mark.asyncio
async def test_legal_refund_completes_outbox_worker_and_authority_check():
    deps = _deps()
    quote = await deps.tools.finance.calculate_refund_amount(
        tenant_id="tenant_demo", refund_request_id="rr-1", order_id="o_1001",
        items=[{"order_line_id": "l1", "sku_id": "sku_1", "quantity": 1}],
        policy_decision_id="pd1", order_version="v1")
    command = _command(quote_id=quote["quote_id"], quote_hash=quote["quote_hash"])
    cid = await deps.persistence.enqueue_command(
        tenant_id=command["tenant_id"], ticket_id=command["ticket_id"], run_id=command["run_id"],
        action=command["action"], order_id=command["order_id"],
        refund_request_id=command["refund_request_id"], quote_id=command["quote_id"],
        quote_hash=command["quote_hash"], plan_id="p1",
        authorization_ref=command["authorization_ref"],
        idempotency_key=command["idempotency_key"], payload_hash=command["payload_hash"],
        payload=command["payload"])
    assert cid
    dispatcher = SagaDispatcher(orders=deps.tools.orders, logistics=deps.tools.logistics,
                                finance=deps.tools.finance)
    claimed = await deps.persistence.claim_next_command()
    assert claimed["command_id"] == cid
    result = await dispatcher.dispatch(claimed)
    status = await deps.persistence.mark_command_result(command_id=cid, tenant_id="tenant_demo",
                                                        result=result)
    assert status == "CONFIRMED"
    authority = await deps.finance.get_refund_status(tenant_id="tenant_demo",
                                                     refund_request_id="rr-1")
    assert authority["status"] == "SUCCEEDED"
    assert await deps.persistence.mark_outbox_published(aggregate_id=cid) is None
    assert cid in deps.persistence.outbox_published


@pytest.mark.asyncio
async def test_duplicate_command_and_worker_restart_do_not_double_refund():
    deps = _deps()
    quote = await deps.tools.finance.calculate_refund_amount(
        tenant_id="tenant_demo", refund_request_id="rr-1", order_id="o_1001",
        items=[{"order_line_id": "l1", "sku_id": "sku_1", "quantity": 1}],
        policy_decision_id="pd1", order_version="v1")
    command = _command(quote_id=quote["quote_id"], quote_hash=quote["quote_hash"])
    dispatcher = SagaDispatcher(orders=deps.tools.orders, logistics=deps.tools.logistics,
                                finance=deps.tools.finance)
    first = await dispatcher.dispatch(command)
    second = await dispatcher.dispatch(command)
    assert first == second
    assert first["provider_txn_id"] == second["provider_txn_id"]
    balance = await deps.finance.get_refundable_balance(tenant_id="tenant_demo", order_id="o_1001")
    assert balance.refundable_balance_minor == 20000 - quote["amount_minor"]
    assert balance.reserved_amount_minor == 0


@pytest.mark.asyncio
async def test_expired_lease_requeues_exactly_once_and_keeps_idempotency():
    deps = _deps()
    cid = await deps.persistence.enqueue_command(
        tenant_id="tenant_demo", ticket_id="tk1", run_id="r1", action="REQUEST_REFUND_ONLY",
        order_id="o_1001", refund_request_id="rr-1", quote_id="q1", quote_hash="qh", plan_id="p1",
        authorization_ref="approval:a1", idempotency_key="refund:tenant_demo:tk1:o_1001",
        payload_hash="ph", payload={})
    claimed = await deps.persistence.claim_next_command()
    assert claimed["command_id"] == cid
    for command in deps.persistence.commands:
        if command["command_id"] == cid:
            command["lease_until"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    reclaimed = await deps.persistence.reclaim_expired_commands(max_attempts=3)
    assert [record["command_id"] for record in reclaimed] == [cid]
    assert deps.persistence.commands[0]["status"] == "PREPARED"
    assert deps.persistence.commands[0]["attempt_count"] == 1
    requeued = await deps.persistence.claim_next_command()
    assert requeued["command_id"] == cid
    assert requeued["idempotency_key"] == "refund:tenant_demo:tk1:o_1001"


@pytest.mark.asyncio
async def test_execution_exception_marks_unknown_and_reconciles_not_success():
    deps = _deps()
    dispatcher = SagaDispatcher(orders=deps.tools.orders, logistics=deps.tools.logistics,
                                finance=deps.tools.finance)
    async def network_failure(**kwargs):
        raise ConnectionError("network interrupted")
    original = dispatcher.finance.execute_refund
    dispatcher.finance.execute_refund = network_failure  # type: ignore[method-assign]
    command = _command()
    result = {"status": "UNKNOWN", "error": "network interrupted"}
    status = await deps.persistence.mark_command_result(command_id="cmd-unknown", tenant_id="tenant_demo",
                                                        result=result)
    assert status == "UNKNOWN"
    assert deps.persistence.reconciliations
    assert not any(record.get("status") == "CONFIRMED" for record in deps.persistence.commands)
    dispatcher.finance.execute_refund = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_excessive_amount_and_order_mismatch_are_rejected_before_command():
    from app.graph import nodes
    from app.domain.enums import Action
    from app.domain.schemas import ActionPlan, ActionProposal

    deps = _deps()
    base = {"tenant_id": "tenant_demo", "ticket_id": "tk1", "run_id": "r1", "trace_id": "tr1",
            "slots": {"order_id": "o_1001", "quantity": 1},
            "plan_hash": "ph1", "quote_order_version": "v1", "plan_frozen_facts_version": "fv1",
            "facts_version": "fv1",
            "order_snapshot": {"order_id": "o_1001", "status": "PAID_UNSHIPPED", "version": "v1",
                               "lines": [{"sku_id": "sku_1", "quantity": 1}]},
            "policy_decision": {"decision": "ALLOW", "authorized_actions": ["REQUEST_REFUND_ONLY"]},
            "validated_actions": ["REQUEST_REFUND_ONLY"]}
    excessive = await nodes.pre_execution_guard({**base, "action_plan": ActionPlan(
        order_id="o_1001", actions=[ActionProposal(action=Action.REQUEST_REFUND_ONLY,
                                                    order_id="o_1001", sku_ids=["sku_1"])]).model_dump(mode="json"),
        "quote_id": "q1", "quote_amount_minor": 999999, "reservation_ref": "res",
        "reservation_amount_minor": 999999}, deps)
    assert excessive["error_code"] == "BALANCE_INSUFFICIENT"
    mismatch = await nodes.command_submitter({**base, "action_plan": ActionPlan(
        order_id="o_other", actions=[ActionProposal(action=Action.REQUEST_REFUND_ONLY,
                                                    order_id="o_other", sku_ids=["sku_1"])]).model_dump(mode="json"),
        "risk_by_action": {}}, deps)
    assert mismatch["command_ids"] == []
    assert deps.persistence.commands == []
