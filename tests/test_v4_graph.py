# -*- coding: utf-8 -*-
"""V4 九节点工作流与 P0 安全边界回归测试。"""
from __future__ import annotations

import pytest
from functools import partial
from langgraph.graph import END, START, StateGraph

pytest.importorskip("langgraph")

from app.bootstrap import build_deps  # noqa: E402
from app.config import Settings  # noqa: E402
from app.graph.build import build_graph  # noqa: E402
from app.graph.state import AgentState  # noqa: E402
from app.graph.v4 import (decision_guard, policy_retrieval, route_after_guard,  # noqa: E402
                          safe_execution)
from app.graph.v4 import _matched_policy_rules  # noqa: E402
from app.policy.library import load_rules  # noqa: E402


def _deps(*, v4: bool = True):
    settings = Settings(_env_file=None, app_env="test", database_url="memory://", llm_model="",
                        workflow_v4_enabled=v4)
    return build_deps(settings)


def _state(**over):
    state = {"tenant_id": "tenant_demo", "user_id": "u_demo", "ticket_id": "tk1", "thread_id": "th1",
             "run_id": "r1", "trace_id": "tr1", "messages": [{"role": "user", "content": "我要退款"}],
             "slots": {"order_id": "o_1001", "quantity": 1},
             "intent_result": {"intent": "REFUND_ONLY"},
             "order_snapshot": {"order_id": "o_1001", "status": "PAID_UNSHIPPED", "version": "v1",
                                "category": "FRESH", "currency": "CNY",
                                "lines": [{"order_line_id": "l1", "sku_id": "sku_1", "quantity": 1}]},
             "finance_snapshot": {"order_id": "o_1001", "refundable_balance_minor": 9900,
                                  "currency": "CNY"},
             "facts_version": "f1",
             "retrieval_bundle": {"status": "OK", "policy_version": "p1",
                                  "items": [
                                      {"clause_id": "CL-JD590903538377428992-S3#2",
                                       "policy_version": "2026.10-jd", "is_exception": False,
                                       "supports_actions": ["REQUEST_REFUND_ONLY"],
                                       "policy_cap_minor": 9900},
                                      {"clause_id": "CL-JD590903538377428992-S3",
                                       "policy_version": "2026.10-jd", "is_exception": False,
                                       "supports_actions": ["REQUEST_REFUND_ONLY"],
                                       "policy_cap_minor": 9900},
                                  ],
                                  "as_of": "2026-11-04T00:00:00Z",
                                  "exception_metadata_status": "NO_EXCEPTIONS",
                                  "required_exception_ids": [],
                                  "resolved_exception_ids": []},
             "decision_proposal": {"decision": "ALLOW", "action": "REQUEST_REFUND_ONLY",
                                   "order_id": "o_1001", "amount_minor": 5000,
                "reason_code": "UNSHIPPED_CANCEL_REFUND",
                "evidence_ids": ["CL-JD590903538377428992-S3#2", "CL-JD590903538377428992-S3"],
                                   "missing_fields": []}}
    state.update(over)
    return state


def _proposal(**over):
    proposal = {"decision": "ALLOW", "action": "REQUEST_REFUND_ONLY", "order_id": "o_1001",
                "amount_minor": 5000, "reason_code": "QUALIFIED",
                "evidence_ids": ["CL-JD590903538377428992-S3#2", "CL-JD590903538377428992-S3"],
                "missing_fields": []}
    proposal.update(over)
    return proposal


def test_v4_graph_has_nine_nodes_and_keeps_v3_default():
    v4 = build_graph(_deps(v4=True)).get_graph().nodes
    names = {n for n in v4 if not n.startswith("__")}
    assert names == {"intent_guardrail", "context_aggregator", "policy_retrieval", "llm_decision",
                     "decision_guard", "hitl_review", "safe_execution", "result_verifier",
                     "response_composer"}
    v3 = build_graph(_deps(v4=False)).get_graph().nodes
    assert "decision_guard" not in {n for n in v3 if not n.startswith("__")}


@pytest.mark.asyncio
async def test_v4_state_schema_preserves_guard_and_execution_outputs(cap_rule):
    deps = _deps()
    state = _state()
    graph = StateGraph(AgentState)
    graph.add_node("decision_guard", partial(decision_guard, deps=deps))
    graph.add_node("safe_execution", partial(safe_execution, deps=deps))
    graph.add_edge(START, "decision_guard")
    graph.add_conditional_edges("decision_guard", route_after_guard,
                                {"execute": "safe_execution", "hitl": "safe_execution", "respond": END})
    graph.add_edge("safe_execution", END)
    result = await graph.compile().ainvoke(state)
    assert result["validated_decision"]["validation_status"] == "VERIFIED"
    assert result["validated_decision"]["decision_id"]
    assert result["risk_result"]["authorization"] in ("AUTO", "REVIEW")
    assert result["submit_status"] == "BLOCKED"
    assert result["error_code"] == "V4_AUTHORIZATION_REQUIRED"
    assert AgentState.__annotations__["validated_decision"]
    assert AgentState.__annotations__["risk_result"]
    assert AgentState.__annotations__["submit_status"]


@pytest.mark.asyncio
async def test_policy_retrieval_does_not_require_policy_decision():
    deps = _deps()
    state = _state(messages=[{"role": "user", "content": "生鲜坏了想退款"}], policy_decision=None)
    out = await policy_retrieval(state, deps)
    assert "policy_decision" not in out
    assert out["evidence_status"] in ("OK", "INSUFFICIENT")
    assert out["retrieval_bundle"]["items"]
    assert all(item.get("policy_version") for item in out["retrieval_bundle"]["items"])


@pytest.mark.asyncio
async def test_policy_retrieval_exception_creates_policy_review_task():
    deps = _deps()
    original = deps.retriever.discover_ex

    async def fail(*args, **kwargs):
        raise TimeoutError("rag timeout")

    deps.retriever.discover_ex = fail  # type: ignore[method-assign]
    state = _state(messages=[{"role": "user", "content": "我要退款"}])
    out = await policy_retrieval(state, deps)
    assert out["evidence_status"] == "ERROR"
    assert out["manual_review_task_id"]
    assert out["public_status"] == "PROCESSING"
    assert deps.persistence.commands == []
    deps.retriever.discover_ex = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_invalid_action_is_rejected_without_command():
    deps = _deps()
    out = await decision_guard(_state(decision_proposal=_proposal(action="ESCALATE_MANUAL")), deps)
    assert out["validated_decision"]["validation_status"] == "REJECTED"
    assert "ACTION_NOT_ALLOWED" in out["validated_decision"]["failure_reasons"]
    assert not deps.persistence.commands


@pytest.mark.asyncio
async def test_excess_amount_is_rejected():
    deps = _deps()
    out = await decision_guard(_state(decision_proposal=_proposal(amount_minor=29900)), deps)
    assert out["validated_decision"]["validation_status"] == "REJECTED"
    assert "AMOUNT_EXCEEDS_REFUNDABLE" in out["validated_decision"]["failure_reasons"]


@pytest.mark.asyncio
async def test_fabricated_evidence_goes_manual_and_creates_idempotent_task():
    deps = _deps()
    state = _state(decision_proposal=_proposal(evidence_ids=["fake-clause"]))
    first = await decision_guard(state, deps)
    second = await decision_guard(state, deps)
    assert first["validated_decision"]["validation_status"] == "MANUAL"
    assert "EVIDENCE_NOT_FOUND" in first["validated_decision"]["failure_reasons"]
    assert first["manual_review_task_id"] == second["manual_review_task_id"]
    assert len(deps.persistence.policy_review_tasks) == 1
    assert first["public_status"] == "PROCESSING"


@pytest.mark.asyncio
async def test_need_info_and_rejected_do_not_create_policy_review():
    deps = _deps()
    need_info = await decision_guard(_state(decision_proposal=_proposal(
        decision="NEED_INFO", action=None, missing_fields=["order_id"])), deps)
    rejected = await decision_guard(_state(decision_proposal=_proposal(
        decision="DENY", action=None, reason_code="POLICY_NOT_ALLOWED")), deps)
    assert need_info["validated_decision"]["validation_status"] == "NEED_INFO"
    assert rejected["validated_decision"]["validation_status"] == "REJECTED"
    assert deps.persistence.policy_review_tasks == []


@pytest.mark.asyncio
async def test_policy_without_exceptions_is_not_blocked(cap_rule):
    deps = _deps()
    state = _state()
    state["retrieval_bundle"]["exception_metadata_status"] = "NO_EXCEPTIONS"
    state["retrieval_bundle"]["required_exception_ids"] = []
    state["retrieval_bundle"]["resolved_exception_ids"] = []
    out = await decision_guard(state, deps)
    assert out["validated_decision"]["validation_status"] == "VERIFIED"
    assert out["validated_decision"]["matched_scope_ids"] == ["TEST-CAP-RULE"]
    assert out["validated_decision"]["auto_approval_cap_minor"] == 100000


@pytest.fixture
def cap_rule(monkeypatch):
    rules = [rule for rule in load_rules()
             if rule.rule_id == "R-JD-CANCEL-UNSHIPPED-001"]
    patched = rules[0].__class__(
        **{**rules[0].__dict__, "amount_cap_minor": 9900, "rule_id": "TEST-CAP-RULE"})
    monkeypatch.setattr("app.graph.v4.load_rules", lambda: [patched])
    return patched


@pytest.fixture
def return_only_rule(monkeypatch):
    rules = [rule for rule in load_rules()
             if rule.rule_id == "R-JD-CANCEL-UNSHIPPED-001"]
    patched = rules[0].__class__(
        **{**rules[0].__dict__, "allowed_actions": ("REQUEST_RETURN_REFUND",),
           "rule_id": "TEST-RETURN-RULE"})
    monkeypatch.setattr("app.graph.v4.load_rules", lambda: [patched])
    return patched


@pytest.fixture
def low_cap_rule(monkeypatch):
    rules = [rule for rule in load_rules()
             if rule.rule_id == "R-JD-CANCEL-UNSHIPPED-001"]
    patched = rules[0].__class__(
        **{**rules[0].__dict__, "amount_cap_minor": 1000,
           "rule_id": "TEST-LOW-CAP-RULE"})
    monkeypatch.setattr("app.graph.v4.load_rules", lambda: [patched])
    return patched


def test_policy_rule_scope_requires_tenant_action_and_facts():
    state = _state()
    rule = next(rule for rule in load_rules() if rule.rule_id == "R-JD-CANCEL-UNSHIPPED-001")
    assert _matched_policy_rules(state, state["decision_proposal"], state["retrieval_bundle"]) == [rule]

    wrong_tenant = dict(state, tenant_id="tenant_other")
    assert _matched_policy_rules(wrong_tenant, state["decision_proposal"], state["retrieval_bundle"]) == []

    wrong_action = {**state, "decision_proposal": {**state["decision_proposal"],
                                                    "action": "REQUEST_RETURN_REFUND"}}
    assert _matched_policy_rules(wrong_action, wrong_action["decision_proposal"], state["retrieval_bundle"]) == []

    expired = {**state, "retrieval_bundle": {**state["retrieval_bundle"],
                                              "as_of": "2025-12-31T23:59:59Z"}}
    assert _matched_policy_rules(expired, state["decision_proposal"], expired["retrieval_bundle"]) == []

    stale = dict(state)
    stale["order_snapshot"] = {**state["order_snapshot"], "status": "DELIVERED"}
    assert _matched_policy_rules(stale, state["decision_proposal"], state["retrieval_bundle"]) == []

    state["slots"]["reason_code"] = "NO_LONGER_NEEDED"
    state["order_snapshot"]["status"] = "DELIVERED"
    state["order_snapshot"]["category"] = "FRESH"
    state["order_snapshot"]["delivered_at"] = "2026-11-01T00:00:00Z"
    state["claim_at"] = "2026-11-02T00:00:00Z"
    state["retrieval_bundle"]["items"] = [
        {"clause_id": "CL-JD4268-A3", "policy_version": "2026.10-jd", "is_exception": False},
        {"clause_id": "CL-JD4268-S4", "policy_version": "2026.10-jd", "is_exception": False},
    ]
    state["decision_proposal"] = {**state["decision_proposal"], "action": "REQUEST_RETURN_REFUND",
                                  "evidence_ids": ["CL-JD4268-A3", "CL-JD4268-S4"]}
    assert [matched.rule_id for matched in _matched_policy_rules(
        state, state["decision_proposal"], state["retrieval_bundle"])] == ["R-JD-NR-WINDOW-OK-001"]


@pytest.mark.asyncio
async def test_policy_supports_return_but_not_refund_only(return_only_rule):
    deps = _deps()
    state = _state(decision_proposal=_proposal(action="REQUEST_REFUND_ONLY"))
    state["retrieval_bundle"]["exception_metadata_status"] = "NO_EXCEPTIONS"
    state["retrieval_bundle"]["required_exception_ids"] = []
    state["retrieval_bundle"]["resolved_exception_ids"] = []
    out = await decision_guard(state, deps)
    assert out["validated_decision"]["validation_status"] == "REJECTED"
    assert "POLICY_ACTION_NOT_SUPPORTED" in out["validated_decision"]["failure_reasons"]
    assert deps.persistence.commands == []


@pytest.mark.asyncio
async def test_policy_cap_lower_than_balance_blocks(low_cap_rule):
    deps = _deps()
    state = _state(decision_proposal=_proposal(amount_minor=5000))
    state["finance_snapshot"]["refundable_balance_minor"] = 29900
    state["retrieval_bundle"]["exception_metadata_status"] = "NO_EXCEPTIONS"
    state["retrieval_bundle"]["required_exception_ids"] = []
    state["retrieval_bundle"]["resolved_exception_ids"] = []
    out = await decision_guard(state, deps)
    assert out["validated_decision"]["validation_status"] == "REJECTED"
    assert out["validated_decision"]["failure_reasons"] == ["OVER_POLICY_CAP"]


@pytest.mark.asyncio
async def test_v4_llm_disabled_does_not_emit_write_command():
    deps = _deps()
    graph = build_graph(deps)
    result = await graph.ainvoke(_state())
    assert result.get("response_status") in ("WAITING_USER", "COMPLETED")
    assert deps.persistence.commands == []


def test_risk_uses_validated_decision_not_legacy_policy():
    from app.risk_rules import evaluate_risk
    from app.domain.enums import RiskDecision

    state = {"validated_decision": {"validated_amount_minor": 5000,
                                    "amount_cap_minor": 9900},
             "finance_snapshot": {"refundable_balance_minor": 9900,
                                  "reserved_amount_minor": 0}}
    outcome = evaluate_risk("REQUEST_REFUND_ONLY", state)
    assert outcome.decision == RiskDecision.AUTO_APPROVE


@pytest.mark.asyncio
async def test_safe_execution_requires_v4_validation_and_approval():
    deps = _deps()
    proposal = _proposal()
    blocked = await safe_execution(_state(decision_proposal=proposal), deps)
    assert blocked["submit_status"] == "BLOCKED"
    assert blocked["error_code"] == "V4_VALIDATION_REQUIRED"
    assert deps.persistence.commands == []

    approved_state = _state(
        decision_proposal=proposal,
        validated_decision={"validation_status": "VERIFIED", "decision_id": "vd1",
                            "policy_cap_minor": 9900, "amount_cap_minor": 9900,
                            "refundable_balance_minor": 9900},
        validated_actions=["REQUEST_REFUND_ONLY"], risk_result={"authorization": "AUTO"},
    )
    approved_state["decision_proposal"]["sku_ids"] = ["sku_1"]
    approved = await safe_execution(approved_state, deps)
    assert approved["submit_status"] == "PERSISTED"
    assert len(approved["command_ids"]) == 1
    assert len(deps.persistence.commands) == 1
