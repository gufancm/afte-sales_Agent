"""Worker 派发闭环 + HITL interrupt/Command(resume) 用例（内存实现）。"""
from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.bootstrap import build_deps
from app.config import Settings
from app.domain.enums import Action, Intent, SlotSource
from app.domain.schemas import (
    ActionPlan,
    ActionProposal,
    ExtractedSlots,
    IntentResult,
    PolicyDecision,
    SlotSources,
)
from app.graph.build import build_graph
from app.saga.dispatcher import SagaDispatcher


class StubLLM:
    def __init__(self, order_id: str = "o_1001", sku_id: str = "sku_1", reason_code: str = "DAMAGED") -> None:
        self.order_id = order_id
        self.sku_id = sku_id
        self.reason_code = reason_code

    async def structured(self, *, messages, schema_model, prompt_version, temperature=0.0):
        if schema_model is IntentResult:
            return (
                IntentResult(
                    intent=Intent.RETURN_REFUND, confidence=0.95,
                    slots=ExtractedSlots(order_id=self.order_id, reason_code=self.reason_code, quantity=1),
                    slot_sources=SlotSources(order_id=SlotSource.USER_STATED, sku_id=None, order_line_id=None,
                                             waybill_no=None, reason_code=SlotSource.USER_STATED,
                                             requested_amount_minor=None, quantity=SlotSource.USER_STATED,
                                             product_category=None),
                ),
                {"request_hash": "x", "response_hash": "y", "usage": {}},
            )
        return (
            ActionPlan(order_id=self.order_id,
                       actions=[ActionProposal(action=Action.REQUEST_RETURN_REFUND, order_id=self.order_id,
                                               sku_ids=[self.sku_id], reason_code=self.reason_code)],
                       rationale="政策允许仅退款"),
            {"request_hash": "x", "response_hash": "y", "usage": {}},
        )


class _NoCapPolicy:
    """返回 ALLOW 但无金额上限 -> 触发风险 REVIEW -> HITL。"""

    def decide(self, *, tenant_id, intent, facts, scope, as_of=None):
        return PolicyDecision(decision="ALLOW", reason_code="TEST_NO_CAP", required_clause_ids=[],
                              amount_cap=None, authorized_actions=["REQUEST_RETURN_REFUND"])


def _deps():
    settings = Settings(_env_file=None, app_env="test", database_url="memory://", llm_model="stub")
    deps = build_deps(settings)
    deps.llm = StubLLM()  # type: ignore[assignment]
    return deps


def _state():
    # 用例验证的是 Worker/HITL 机制，需要 RETURN_REFUND 才会产生退货写命令；
    # 因此这里用显式“退货退款”表述。只说“想退款”按 E1 口径属 REFUND_ONLY（先澄清替代方案）。
    return {"tenant_id": "tenant_demo", "user_id": "u_demo", "ticket_id": "tk1",
            "thread_id": "th1", "run_id": "r1", "trace_id": "tr1",
            "messages": [{"role": "user", "content": "生鲜坏了，我要退货退款"}]}


@pytest.mark.asyncio
async def test_worker_dispatches_refund_command():
    deps = _deps()
    # o_1002 = 服饰/已签收：质量问题 -> R-JD-Q-RETURN-001 ALLOW。
    # 真实语料未给退款金额上限 -> 风控 REVIEW -> HITL；审批通过后生成退款命令，再由 Worker 派发。
    deps.llm = StubLLM(order_id="o_1002", sku_id="sku_a1")
    graph = build_graph(deps, checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "tk1:r1"}}

    r1 = await graph.ainvoke(_state(), config=cfg)
    assert "__interrupt__" in r1, "未定义金额上限的退款应触发人工审批"
    approval_id = r1["__interrupt__"][0].value["approval_id"]
    await deps.persistence.decide_approval(tenant_id="tenant_demo", approval_id=approval_id,
                                           decision="APPROVED", reviewer_id="rev1")
    result = await graph.ainvoke(Command(resume={"approval_id": approval_id, "decision": "APPROVED"}), config=cfg)
    assert result.get("response_status") == "WAITING_EXTERNAL"
    assert len(deps.persistence.commands) == 1

    dispatcher = SagaDispatcher(orders=deps.tools.orders, logistics=deps.tools.logistics, finance=deps.tools.finance)
    command = await deps.persistence.claim_next_command()
    assert command and command["refund_request_id"]
    res = await dispatcher.dispatch(command)
    status = await deps.persistence.mark_command_result(
        command_id=command["command_id"], tenant_id=command["tenant_id"], result=res)
    assert status == "CONFIRMED"
    fin = await deps.finance.get_refund_status(tenant_id="tenant_demo",
                                               refund_request_id=command["refund_request_id"])
    assert fin["status"] == "SUCCEEDED"


@pytest.mark.asyncio
async def test_hitl_interrupt_then_resume():
    deps = _deps()
    deps.policy = _NoCapPolicy()  # type: ignore[assignment]
    graph = build_graph(deps, checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "tk1:r1"}}

    r1 = await graph.ainvoke(_state(), config=cfg)
    assert "__interrupt__" in r1, "无上限金额应触发 HITL interrupt"
    approval_id = r1["__interrupt__"][0].value["approval_id"]

    await deps.persistence.decide_approval(tenant_id="tenant_demo", approval_id=approval_id,
                                           decision="APPROVED", reviewer_id="rev1")
    r2 = await graph.ainvoke(Command(resume={"approval_id": approval_id, "decision": "APPROVED"}), config=cfg)
    assert r2.get("approval_status") == "APPROVED"
    assert r2.get("response_status") == "WAITING_EXTERNAL"
    assert len(deps.persistence.commands) == 1


@pytest.mark.asyncio
async def test_hitl_reject_stops_execution():
    deps = _deps()
    deps.policy = _NoCapPolicy()  # type: ignore[assignment]
    graph = build_graph(deps, checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "tk1:r2"}}

    r1 = await graph.ainvoke(_state(), config=cfg)
    approval_id = r1["__interrupt__"][0].value["approval_id"]
    await deps.persistence.decide_approval(tenant_id="tenant_demo", approval_id=approval_id,
                                           decision="REJECTED", reviewer_id="rev1")
    r2 = await graph.ainvoke(Command(resume={"approval_id": approval_id, "decision": "REJECTED"}), config=cfg)
    assert r2.get("approval_status") != "APPROVED"
    assert deps.persistence.commands == [], "拒绝后不得产生任何写命令"


@pytest.mark.asyncio
async def test_ensure_approval_task_reports_created_once():
    """幂等：同一 (tenant, plan_hash) 只新建一次，之后 created=False。"""
    deps = _deps()
    kw = dict(tenant_id="tenant_demo", ticket_id="tk1", plan_id=None, plan_hash="ph-1",
              quote_id=None, expected_version=None)
    first = await deps.persistence.ensure_approval_task(**kw)
    second = await deps.persistence.ensure_approval_task(**kw)
    assert first["created"] is True and second["created"] is False
    assert first["approval_id"] == second["approval_id"]
    assert len(deps.persistence.approvals) == 1


@pytest.mark.asyncio
async def test_hitl_resume_does_not_duplicate_approval_required_audit():
    """interrupt() 恢复会重放节点代码；APPROVAL_REQUIRED 不应重复落库。"""
    deps = _deps()
    deps.llm = StubLLM(order_id="o_1002", sku_id="sku_a1")  # type: ignore[assignment]
    graph = build_graph(deps, checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "tk-dup:r1"}}

    r1 = await graph.ainvoke(_state(), config=cfg)
    assert "__interrupt__" in r1
    approval_id = r1["__interrupt__"][0].value["approval_id"]
    assert len([a for a in deps.persistence.audits if a["event"] == "APPROVAL_REQUIRED"]) == 1

    await deps.persistence.decide_approval(tenant_id="tenant_demo", approval_id=approval_id,
                                           decision="APPROVED", reviewer_id="rev1")
    await graph.ainvoke(Command(resume={"approval_id": approval_id, "decision": "APPROVED"}), config=cfg)

    required = [a for a in deps.persistence.audits if a["event"] == "APPROVAL_REQUIRED"]
    assert len(required) == 1, "恢复重放不应重复写 APPROVAL_REQUIRED"
    assert len([a for a in deps.persistence.audits if a["event"] == "APPROVAL_RESOLVED"]) == 1
