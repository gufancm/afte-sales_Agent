# -*- coding: utf-8 -*-
"""V4 九节点工作流：独立检索、候选提案、确定性验证与安全执行。"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import partial

from langgraph.graph import END, START, StateGraph

from app.domain.schemas import DecisionProposal
from app.graph import nodes
from app.graph.deps import Deps
from app.graph.retry import build_node_retry_policy
from app.graph.state import AgentState

RESPOND = "response_composer"
PROMPT_VERSION_DECISION = "decision-v4.0"
WRITE_ACTIONS = {"REQUEST_REFUND_ONLY", "REQUEST_RETURN_REFUND", "INTERCEPT_PARCEL",
                 "CREATE_RESHIPMENT", "EXPEDITE_DELIVERY"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def policy_retrieval(state, deps: Deps) -> dict:
    """独立混合检索；不读取 policy_decision 或 required_clause_ids。"""
    query = " ".join(str(m.get("content", "")) for m in state.get("messages", [])[-4:])
    filters = {"category": (state.get("order_snapshot") or {}).get("category")}
    try:
        chunks, diagnostics = await deps.retriever.discover_ex(
            tenant_id=state["tenant_id"], query=query or "售后", filters=filters, history=[])
    except Exception as exc:
        return {"retrieval_bundle": {"status": "ERROR", "items": [],
                                     "retrieval_diagnostics": {"error": str(exc)[:200]}},
                "evidence_status": "ERROR", "error_code": "POLICY_RETRIEVAL_FAILED"}

    fetched = {chunk.clause_id: chunk for chunk in chunks}
    as_of = state.get("claim_at") or _now()
    for group_id in {chunk.clause_group_id for chunk in chunks if chunk.clause_group_id}:
        for chunk in await deps.retriever.clauses.fetch_group(state["tenant_id"], group_id, as_of):
            fetched.setdefault(chunk.clause_id, chunk)

    versions = sorted({chunk.version for chunk in fetched.values() if chunk.version})
    orphan_exceptions = [chunk.clause_id for chunk in fetched.values()
                         if chunk.is_exception and not chunk.clause_group_id]
    status = ("INSUFFICIENT" if not fetched else
              "CONFLICT" if orphan_exceptions else
              "STALE" if len(versions) > 1 else "OK")
    items = [{
        "clause_id": chunk.clause_id, "document_id": chunk.doc_id,
        "policy_version": chunk.version, "content": chunk.text,
        "source_ref": chunk.doc_id, "clause_group_id": chunk.clause_group_id,
        "is_exception": chunk.is_exception,
    } for chunk in fetched.values()]
    bundle = {"status": status, "items": items,
              "policy_version": versions[0] if len(versions) == 1 else None,
              "as_of": as_of, "query_trace": [{"query": query, "filters": filters}],
              "retrieval_diagnostics": diagnostics}
    out = {"retrieval_bundle": bundle, "evidence_status": status}
    if status in ("INSUFFICIENT", "CONFLICT", "STALE", "ERROR"):
        out["manual_review_task_id"] = await _ensure_policy_review(state, deps, bundle, status)
        out["public_status"] = "PROCESSING"
    return out


async def _ensure_policy_review(state, deps: Deps, bundle: dict, reason_code: str) -> str:
    return await deps.persistence.ensure_policy_review_task(
        tenant_id=state["tenant_id"], ticket_id=state["ticket_id"],
        thread_id=state.get("thread_id"), policy_version=bundle.get("policy_version"),
        reason_code=reason_code, retrieval_bundle=bundle,
        facts_snapshot={"order": state.get("order_snapshot"),
                        "finance": state.get("finance_snapshot")})


async def llm_decision(state, deps: Deps) -> dict:
    """基于事实和检索证据生成强类型候选方案；失败安全降级为 MANUAL。"""
    if deps.llm is None:
        proposal = DecisionProposal(decision="MANUAL", reason_code="LLM_DISABLED",
                                    missing_fields=["LLM_UNAVAILABLE"])
        return {"decision_proposal": proposal.model_dump(mode="json"), "error_code": "LLM_DISABLED"}

    bundle = state.get("retrieval_bundle") or {}
    evidence = bundle.get("items") or []
    user = {
        "message": state["messages"][-1].get("content", "") if state.get("messages") else "",
        "facts": {"order": state.get("order_snapshot") or {},
                  "finance": state.get("finance_snapshot") or {},
                  "slots": state.get("slots") or {}},
        "evidence": [{"id": item.get("clause_id"), "text": item.get("content"),
                      "is_exception": item.get("is_exception")} for item in evidence],
        "evidence_ids": [item.get("clause_id") for item in evidence],
        "action_whitelist": sorted(WRITE_ACTIONS),
    }
    try:
        proposal, audit = await deps.llm.structured(
            messages=[{"role": "system", "content": deps.prompts.get("decision_proposal", "")},
                      {"role": "user", "content": str(user)}],
            schema_model=DecisionProposal, prompt_version=PROMPT_VERSION_DECISION, temperature=0.0)
        await deps.persistence.record_llm_call(
            tenant_id=state["tenant_id"], ticket_id=state.get("ticket_id"),
            run_id=state.get("run_id"), trace_id=state.get("trace_id"), node="llm_decision",
            model=audit.get("model"), prompt_version=PROMPT_VERSION_DECISION, status="SUCCESS",
            request_hash=audit.get("request_hash"), response_hash=audit.get("response_hash"),
            input_tokens=(audit.get("usage") or {}).get("prompt_tokens"),
            output_tokens=(audit.get("usage") or {}).get("completion_tokens"),
            latency_ms=audit.get("latency_ms"))
        return {"decision_proposal": proposal.model_dump(mode="json")}
    except Exception as exc:
        proposal = DecisionProposal(decision="MANUAL", reason_code="LLM_DECISION_FAILED",
                                    missing_fields=["LLM_UNAVAILABLE"])
        await deps.persistence.record_llm_call(
            tenant_id=state["tenant_id"], ticket_id=state.get("ticket_id"),
            run_id=state.get("run_id"), trace_id=state.get("trace_id"), node="llm_decision",
            model=deps.settings.llm_model, prompt_version=PROMPT_VERSION_DECISION, status="ERROR",
            error_code=str(exc)[:200])
        return {"decision_proposal": proposal.model_dump(mode="json"),
                "error_code": "LLM_DECISION_FAILED"}


async def decision_guard(state, deps: Deps) -> dict:
    """先确定性验证候选方案，再执行风控；不读取旧 policy_decision。"""
    proposal = state.get("decision_proposal") or {}
    bundle = state.get("retrieval_bundle") or {}
    order = state.get("order_snapshot") or {}
    finance = state.get("finance_snapshot") or {}
    slots = state.get("slots") or {}
    evidence_ids = {item.get("clause_id") for item in bundle.get("items", [])}
    has_exception = any(item.get("is_exception") for item in bundle.get("items", []))

    if proposal.get("decision") == "NEED_INFO":
        return _validation_result(state, "NEED_INFO", [], None, ["NEED_INFO"])
    if proposal.get("decision") == "DENY":
        return _validation_result(state, "REJECTED", [], None,
                                  [proposal.get("reason_code") or "REJECTED"])
    if proposal.get("decision") != "ALLOW":
        return await _manual_review(state, deps, ["POLICY_REVIEW"])

    action = proposal.get("action")
    order_id = proposal.get("order_id") or slots.get("order_id") or order.get("order_id")
    reasons: list[str] = []
    if action not in WRITE_ACTIONS:
        reasons.append("ACTION_NOT_ALLOWED")
    if not order_id or order_id != (order.get("order_id") or order_id):
        reasons.append("ORDER_ID_MISMATCH")
    if (state.get("intent_result") or {}).get("intent") not in (
            "REFUND_ONLY", "RETURN_REFUND", "INTERCEPT_REFUND", "RESHIPMENT", "EXPEDITE"):
        reasons.append("INTENT_NOT_EXECUTABLE")
    if [item for item in proposal.get("evidence_ids", []) if item not in evidence_ids]:
        reasons.append("EVIDENCE_NOT_FOUND")
    if not proposal.get("evidence_ids") or not has_exception:
        reasons.append("EVIDENCE_INCOMPLETE")
    if proposal.get("amount_minor") is not None:
        balance = finance.get("refundable_balance_minor")
        if balance is None or not (0 < proposal["amount_minor"] <= int(balance)):
            reasons.append("AMOUNT_EXCEEDS_REFUNDABLE")
    if action in ("REQUEST_REFUND_ONLY", "REQUEST_RETURN_REFUND") and proposal.get("amount_minor") is None:
        reasons.append("AMOUNT_MISSING")

    if reasons:
        status = "MANUAL" if any(code.startswith("EVIDENCE") for code in reasons) else "REJECTED"
        result = _validation_result(state, status, [], None, reasons)
        if status == "MANUAL":
            return await _manual_review(state, deps, reasons, result)
        return result

    validated = _validation_result(state, "VERIFIED", [action], proposal.get("amount_minor"), [])
    risk = await nodes.risk_authorization({**state, "action_plan": {
        "actions": [{"action": action, "order_id": order_id,
                     "requested_amount_minor": proposal.get("amount_minor")}]},
        "quote_amount_minor": proposal.get("amount_minor")}, deps)
    authorization = {"AUTO_APPROVE": "AUTO", "REVIEW": "REVIEW", "DENY": "DENY",
                     "MANUAL": "REVIEW", "NEED_INFO": "REVIEW"}[risk.get("risk_decision")]
    risk_result = {"authorization": authorization,
                   "reason_codes": risk.get("risk_reason_codes") or [],
                   "strategy_version": "risk-rules-v1"}
    return {**validated, "risk_result": risk_result}


def _validation_result(state, status, actions, amount, reasons) -> dict:
    result = {"validation_status": status, "authorized_actions": actions,
              "validated_amount_minor": amount,
              "evidence_ids": (state.get("decision_proposal") or {}).get("evidence_ids", []),
              "facts_version": state.get("facts_version"),
              "policy_version": (state.get("retrieval_bundle") or {}).get("policy_version"),
              "failure_reasons": reasons}
    return {"validated_decision": result, "validation_result": result,
            "validated_actions": actions, "validated_amount_minor": amount}


async def _manual_review(state, deps: Deps, reasons, base=None):
    task_id = await _ensure_policy_review(state, deps, state.get("retrieval_bundle") or {},
                                          reasons[0] if reasons else "POLICY_REVIEW")
    out = base or _validation_result(state, "MANUAL", [], None, reasons)
    return {**out, "manual_review_task_id": task_id, "public_status": "PROCESSING"}


async def safe_execution(state, deps: Deps) -> dict:
    """准备、执行前复核与幂等命令提交合并在一个图节点。"""
    proposal = state.get("decision_proposal") or {}
    order_id = proposal.get("order_id") or (state.get("slots") or {}).get("order_id")
    plan = {"order_id": order_id, "actions": [{
        "action": proposal.get("action"), "order_id": order_id,
        "requested_amount_minor": proposal.get("amount_minor")}]}
    prepared = await nodes.action_preparation(
        {**state, "action_plan": plan, "policy_decision": {"policy_decision_id": None}}, deps)
    if prepared.get("error_code"):
        return {**prepared, "submit_status": "BLOCKED", "command_ids": []}
    guarded = await nodes.pre_execution_guard({**state, **prepared, "action_plan": plan}, deps)
    if guarded.get("error_code"):
        return {**guarded, "submit_status": "BLOCKED", "command_ids": []}
    submitted = await nodes.command_submitter(
        {**state, **prepared, "action_plan": plan,
         "policy_decision": {"authorized_actions": state.get("validated_actions")}}, deps)
    return {**submitted, "submit_status": "PERSISTED" if submitted.get("command_ids") else "BLOCKED"}


def route_after_retrieval(state) -> str:
    status = state.get("evidence_status")
    if status in ("INSUFFICIENT", "CONFLICT", "STALE", "ERROR"):
        return "manual"
    if (state.get("intent_result") or {}).get("speech_act") == "ELIGIBILITY_QUERY":
        return "respond"
    return "decision"


def route_after_guard(state) -> str:
    result = state.get("validated_decision") or {}
    risk = (state.get("risk_result") or {}).get("authorization")
    if result.get("validation_status") != "VERIFIED":
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
    """注册 9 个 V4 图节点；底层安全服务复用既有实现。"""
    g = StateGraph(AgentState)

    def add(name, fn):
        retry = build_node_retry_policy(deps.settings) if name in ("policy_retrieval", "llm_decision") else None
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
