# -*- coding: utf-8 -*-
"""V4 九节点工作流装配：复用 V3 安全服务，保留 V3 图兼容。"""
from __future__ import annotations

from functools import partial

from langgraph.graph import END, START, StateGraph

from app.graph import nodes
from app.graph.deps import Deps
from app.graph.retry import build_node_retry_policy
from app.graph.state import AgentState

RESPOND = "response_composer"


async def policy_retrieval(state, deps: Deps) -> dict:
    """独立检索节点：不依赖 PolicyDecision.required_clause_ids。"""
    update = await nodes.policy_evidence(state, deps)
    return {"retrieval_bundle": update.get("evidence_bundle") or {}, **update}


async def llm_decision(state, deps: Deps) -> dict:
    update = await nodes.action_planner(state, deps)
    proposal = {
        "decision": "NEED_INFO",
        "action": None,
        "order_id": (state.get("slots") or {}).get("order_id"),
        "amount_minor": (state.get("slots") or {}).get("requested_amount_minor"),
        "evidence_ids": [i.get("clause_id") for i in
                         ((state.get("retrieval_bundle") or {}).get("items") or [])],
    }
    plan = update.get("action_plan") or {}
    if plan.get("actions"):
        proposal["action"] = plan["actions"][0].get("action")
        proposal["decision"] = "ALLOW"
    elif plan.get("needs_more_info"):
        proposal["clarification_question"] = "、".join(plan.get("missing_information") or [])
    return {**update, "decision_proposal": proposal}


async def decision_guard(state, deps: Deps) -> dict:
    """确定性验证优先，其次风控；验证失败绝不进入 SafeExecution。"""
    plan = state.get("action_plan") or {}
    validation = {
        "status": "NEED_INFO",
        "validated_actions": [],
        "validated_amount_minor": None,
        "policy_version": (state.get("retrieval_bundle") or {}).get("policy_version"),
        "facts_version": state.get("facts_version"),
        "validated_evidence_ids": [],
        "reason_codes": [],
    }
    decision = state.get("policy_decision") or {}
    evidence_ok = state.get("evidence_status") == "OK"
    if state.get("error_code"):
        validation["status"] = "MANUAL"
        validation["reason_codes"] = [state["error_code"]]
    elif decision.get("decision") == "ALLOW" and evidence_ok and plan.get("actions"):
        validation["status"] = "VERIFIED"
        validation["validated_actions"] = decision.get("authorized_actions") or []
        validation["validated_amount_minor"] = state.get("quote_amount_minor")
        validation["validated_evidence_ids"] = [
            i.get("clause_id") for i in ((state.get("retrieval_bundle") or {}).get("items") or [])
        ]
    else:
        validation["status"] = {"DENY": "REJECTED", "MANUAL": "MANUAL"}.get(
            decision.get("decision"), "NEED_INFO")
        validation["reason_codes"] = [decision.get("reason_code") or "VALIDATION_FAILED"]

    out = {"validation_result": validation,
           "validated_actions": validation["validated_actions"],
           "validated_amount_minor": validation["validated_amount_minor"],
           "validated_evidence_ids": validation["validated_evidence_ids"]}
    if validation["status"] != "VERIFIED":
        task_id = await deps.persistence.ensure_policy_review_task(
            tenant_id=state["tenant_id"], ticket_id=state["ticket_id"],
            thread_id=state.get("thread_id"),
            policy_version=(state.get("retrieval_bundle") or {}).get("policy_version"),
            reason_code=validation["reason_codes"][0] if validation["reason_codes"] else "POLICY_REVIEW",
            retrieval_bundle=state.get("retrieval_bundle") or {},
            facts_snapshot={"order": state.get("order_snapshot"),
                            "finance": state.get("finance_snapshot")})
        out["manual_review_task_id"] = task_id
        return out

    prepared = await nodes.action_preparation(state, deps)
    out.update(prepared)
    if prepared.get("error_code"):
        out["validation_result"] = {**validation, "status": "MANUAL",
                                   "reason_codes": [prepared["error_code"]]}
        out["validated_actions"] = []
        out["validated_amount_minor"] = None
        return out
    risk = await nodes.risk_authorization({**state, **prepared}, deps)
    out.update(risk)
    out["risk_authorization"] = {
        "authorization": {"AUTO_APPROVE": "AUTO", "REVIEW": "REVIEW", "DENY": "DENY",
                          "MANUAL": "REVIEW", "NEED_INFO": "REVIEW"}[risk.get("risk_decision")],
        "risk_rule_version": "risk-rules-v1",
        "reason_codes": risk.get("risk_reason_codes") or [],
    }
    return out


async def safe_execution(state, deps: Deps) -> dict:
    """准备、最终复核与幂等命令提交合并在一个图节点。"""
    guarded = await nodes.pre_execution_guard(state, deps)
    if guarded.get("error_code"):
        return {**guarded, "submit_status": "BLOCKED", "command_ids": []}
    update = await nodes.command_submitter(state, deps)
    return {**update, "submit_status": "PERSISTED" if update.get("command_ids") else "BLOCKED"}


def route_after_retrieval(state) -> str:
    status = state.get("evidence_status")
    if status in ("INSUFFICIENT", "CONFLICT", "STALE"):
        return "manual"
    if (state.get("intent_result") or {}).get("speech_act") == "ELIGIBILITY_QUERY":
        return "respond"
    return "decision"


def route_after_guard(state) -> str:
    result = state.get("validation_result") or {}
    risk = (state.get("risk_authorization") or {}).get("authorization")
    if result.get("status") != "VERIFIED":
        return "respond"
    if risk == "DENY":
        return "respond"
    if risk == "REVIEW":
        return "hitl"
    return "execute"


def route_after_execution(state) -> str:
    if state.get("submit_status") == "PERSISTED":
        return "verify"
    return "respond"


def build_v4_graph(deps: Deps, checkpointer=None):
    """注册 9 个 V4 图节点；底层服务复用 V3 实现。"""
    g = StateGraph(AgentState)

    def add(name, fn):
        if name in ("policy_retrieval", "llm_decision"):
            retry = build_node_retry_policy(deps.settings)
        else:
            retry = None
        g.add_node(name, partial(fn, deps=deps), retry_policy=retry)

    add("intent_guardrail", nodes.intent_guardrail)
    add("context_aggregator", nodes.context_aggregator)
    add("policy_retrieval", policy_retrieval)
    add("llm_decision", llm_decision)
    add("decision_guard", decision_guard)
    add("hitl_review", nodes.hitl_review)
    add("safe_execution", safe_execution)
    add("result_verifier", nodes.result_verifier)
    add(RESPOND, nodes.response_composer)

    g.add_edge(START, "intent_guardrail")
    g.add_conditional_edges("intent_guardrail", nodes.route_after_intent,
                            {"aggregate": "context_aggregator", "clarify": RESPOND,
                             "deny": RESPOND, "manual": RESPOND, "readonly": "context_aggregator"})
    g.add_conditional_edges("context_aggregator", nodes.route_after_context,
                            {"policy": "policy_retrieval", "respond": RESPOND, "clarify": RESPOND,
                             "deny": RESPOND, "manual": RESPOND})
    g.add_conditional_edges("policy_retrieval", route_after_retrieval,
                            {"decision": "llm_decision", "respond": RESPOND, "manual": RESPOND})
    g.add_edge("llm_decision", "decision_guard")
    g.add_conditional_edges("decision_guard", route_after_guard,
                            {"execute": "safe_execution", "hitl": "hitl_review", "respond": RESPOND})
    g.add_conditional_edges("hitl_review", nodes.route_after_hitl,
                            {"guard": "safe_execution", "deny": RESPOND})
    g.add_conditional_edges("safe_execution", route_after_execution,
                            {"verify": "result_verifier", "respond": RESPOND})
    g.add_edge("result_verifier", RESPOND)
    g.add_edge(RESPOND, END)
    return g.compile(checkpointer=checkpointer)
