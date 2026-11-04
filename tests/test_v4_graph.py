# -*- coding: utf-8 -*-
"""V4 九节点工作流与安全边界回归测试。"""
from __future__ import annotations

import pytest

pytest.importorskip("langgraph")

from app.bootstrap import build_deps  # noqa: E402
from app.config import Settings  # noqa: E402
from app.graph.build import build_graph  # noqa: E402
from app.graph.v4 import decision_guard  # noqa: E402


def _deps(*, v4: bool = True):
    settings = Settings(_env_file=None, app_env="test", database_url="memory://", llm_model="",
                        workflow_v4_enabled=v4)
    return build_deps(settings)


def _state(**over):
    state = {"tenant_id": "t1", "user_id": "u_demo", "ticket_id": "tk1", "thread_id": "th1",
             "run_id": "r1", "trace_id": "tr1", "messages": [{"role": "user", "content": "我要退款"}],
             "slots": {"order_id": "o_1001", "quantity": 1},
             "policy_decision": {"decision": "ALLOW", "reason_code": "OK", "policy_version": "v1",
                                 "authorized_actions": ["REQUEST_REFUND_ONLY", "REQUEST_RETURN_REFUND"]},
             "evidence_status": "OK", "retrieval_bundle": {"policy_version": "v1", "items": []},
             "facts_version": "f1", "action_plan": {"actions": [], "needs_more_info": True}}
    state.update(over)
    return state


def test_v4_graph_has_nine_nodes_and_keeps_v3_default():
    v4 = build_graph(_deps(v4=True)).get_graph().nodes
    names = {n for n in v4 if not n.startswith("__")}
    assert names == {"intent_guardrail", "context_aggregator", "policy_retrieval", "llm_decision",
                     "decision_guard", "hitl_review", "safe_execution", "result_verifier",
                     "response_composer"}

    v3 = build_graph(_deps(v4=False)).get_graph().nodes
    assert "decision_guard" not in {n for n in v3 if not n.startswith("__")}


@pytest.mark.asyncio
async def test_failed_validation_creates_policy_review_not_command():
    deps = _deps()
    state = _state(policy_decision={"decision": "DENY", "reason_code": "POLICY_NOT_ALLOWED",
                                    "authorized_actions": []})
    out = await decision_guard(state, deps)
    assert out["validation_result"]["status"] == "REJECTED"
    assert out["manual_review_task_id"]
    assert deps.persistence.commands == []
    assert deps.persistence.policy_review_tasks


@pytest.mark.asyncio
async def test_policy_review_task_is_idempotent():
    deps = _deps()
    state = _state(policy_decision={"decision": "DENY", "reason_code": "POLICY_NOT_ALLOWED",
                                    "authorized_actions": []})
    first = await decision_guard(state, deps)
    second = await decision_guard(state, deps)
    assert first["manual_review_task_id"] == second["manual_review_task_id"]
    assert len(deps.persistence.policy_review_tasks) == 1


@pytest.mark.asyncio
async def test_v4_llm_disabled_does_not_emit_write_command():
    deps = _deps()
    graph = build_graph(deps)
    result = await graph.ainvoke(_state())
    assert result.get("response_status") in ("WAITING_USER", "COMPLETED")
    assert deps.persistence.commands == []
