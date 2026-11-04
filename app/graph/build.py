# -*- coding: utf-8 -*-
"""LangGraph 装配与路由（LLD §7.2）。唯一准入路线；不在此处发外部写调用。

v3.6 结构（12 个核心节点 + 独立 Saga/对账 Worker + 确定性业务服务）：
  intent_guardrail -> context_aggregator -> policy_engine -> policy_evidence
    -> action_planner -> action_preparation -> risk_authorization
    -> [hitl_review] -> pre_execution_guard -> command_submitter -> result_verifier
    -> response_composer
Policy Engine / Risk / Evidence 都是可独立测试的确定性模块；长跑对账不在图内。
"""
from __future__ import annotations

from functools import partial

from langgraph.graph import END, START, StateGraph

from app.graph import nodes
from app.graph.deps import Deps
from app.graph.retry import retry_policy_for
from app.graph.state import AgentState
from app.graph.v4 import build_v4_graph

RESPOND = "response_composer"


def build_graph(deps: Deps, checkpointer=None):
    if getattr(deps.settings, "workflow_v4_enabled", False):
        return build_v4_graph(deps, checkpointer)
    g = StateGraph(AgentState)

    def add(name: str, fn) -> None:
        """统一装配：重试策略按节点白名单取；写节点恒为 None（见 graph/retry.py）。"""
        g.add_node(name, partial(fn, deps=deps),
                   retry_policy=retry_policy_for(name, deps.settings))

    add("intent_guardrail", nodes.intent_guardrail)
    add("context_aggregator", nodes.context_aggregator)
    add("policy_engine", nodes.policy_engine)
    add("policy_evidence", nodes.policy_evidence)
    add("action_planner", nodes.action_planner)
    add("action_preparation", nodes.action_preparation)
    add("risk_authorization", nodes.risk_authorization)
    add("hitl_review", nodes.hitl_review)
    add("pre_execution_guard", nodes.pre_execution_guard)
    add("command_submitter", nodes.command_submitter)
    add("result_verifier", nodes.result_verifier)
    add(RESPOND, nodes.response_composer)

    g.add_edge(START, "intent_guardrail")
    g.add_conditional_edges("intent_guardrail", nodes.route_after_intent,
                            {"aggregate": "context_aggregator", "clarify": RESPOND, "deny": RESPOND,
                             "manual": RESPOND, "readonly": "context_aggregator"})
    g.add_conditional_edges("context_aggregator", nodes.route_after_context,
                            {"policy": "policy_engine", "respond": RESPOND, "clarify": RESPOND,
                             "deny": RESPOND, "manual": RESPOND})
    g.add_conditional_edges("policy_engine", nodes.route_after_policy,
                            {"retrieve": "policy_evidence", "deny": RESPOND, "clarify": RESPOND,
                             "manual": RESPOND})
    g.add_conditional_edges("policy_evidence", nodes.route_after_evidence,
                            {"plan": "action_planner", "clarify": RESPOND, "manual": RESPOND,
                             "answer": RESPOND})
    g.add_conditional_edges("action_planner", nodes.route_after_plan,
                            {"prepare": "action_preparation", "clarify": RESPOND, "manual": RESPOND,
                             "replan": "action_planner"})
    g.add_edge("action_preparation", "risk_authorization")
    g.add_conditional_edges("risk_authorization", nodes.route_after_risk,
                            {"deny": RESPOND, "hitl": "hitl_review", "manual": RESPOND,
                             "guard": "pre_execution_guard"})
    g.add_conditional_edges("hitl_review", nodes.route_after_hitl,
                            {"guard": "pre_execution_guard", "deny": RESPOND})
    g.add_conditional_edges("pre_execution_guard", nodes.route_after_guard,
                            {"execute": "command_submitter", "reprepare": "action_preparation",
                             "rerisk": "risk_authorization", "deny": RESPOND, "manual": RESPOND})
    g.add_edge("command_submitter", "result_verifier")
    # 对账已移出图：result_verifier 幂等创建对账任务，交独立 Worker 长跑（§18.10）
    g.add_edge("result_verifier", RESPOND)
    g.add_edge(RESPOND, END)

    return g.compile(checkpointer=checkpointer)
