# -*- coding: utf-8 -*-
"""节点级重试策略：白名单正确性 + LangGraph 真实重试行为。

覆盖三条不变量：
  1. 只有「可重入、无业务副作用」的节点配置了 RetryPolicy；
  2. 资金写/审批暂停/状态回查节点在编译后的图里 retry_policy 必须为 None；
  3. 策略只对瞬时故障生效（超时重试、业务错误不重试）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from app.config import Settings
from app.graph.build import build_graph
from app.graph.retry import (NO_RETRY_NODES, RETRY_SAFE_NODES, build_node_retry_policy,
                             retry_policy_for)
from app.mcp.gateway import ToolAuthorizationError, is_transient_error


class _S(TypedDict):
    n: int


def _compiled(policy, fn):
    g = StateGraph(_S)
    g.add_node("n", fn, retry_policy=policy)
    g.add_edge(START, "n")
    g.add_edge("n", END)
    return g.compile()


def test_whitelists_are_disjoint_and_cover_all_nodes():
    assert RETRY_SAFE_NODES.isdisjoint(NO_RETRY_NODES)
    graph = build_graph(SimpleNamespace(settings=Settings()), checkpointer=None)
    assert set(graph.nodes) - {"__start__"} == RETRY_SAFE_NODES | NO_RETRY_NODES


def test_write_and_pause_nodes_never_retry():
    settings = Settings(graph_node_retry_enabled=True)
    for name in ("command_submitter", "hitl_review", "pre_execution_guard",
                 "result_verifier", "risk_authorization", "action_preparation"):
        assert retry_policy_for(name, settings) is None, name


def test_compiled_graph_wires_retry_only_on_safe_nodes():
    graph = build_graph(SimpleNamespace(settings=Settings()), checkpointer=None)
    for name in RETRY_SAFE_NODES:
        assert graph.nodes[name].retry_policy, name
    for name in NO_RETRY_NODES:
        assert graph.nodes[name].retry_policy is None, name


def test_disabled_by_config():
    assert build_node_retry_policy(Settings(graph_node_retry_enabled=False)) is None
    assert retry_policy_for("intent_guardrail",
                            Settings(graph_node_retry_enabled=False)) is None


def test_retry_on_predicate_is_narrow():
    assert is_transient_error(TimeoutError("t"))
    assert is_transient_error(ConnectionError("reset"))
    assert not is_transient_error(ValueError("bad json"))
    assert not is_transient_error(ToolAuthorizationError("NOT_OWNER"))
    assert not is_transient_error(KeyError("k"))


async def test_transient_error_is_retried_until_success():
    attempts = {"n": 0}

    async def flaky(state: _S) -> _S:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise TimeoutError("upstream timeout")
        return {"n": attempts["n"]}

    out = await _compiled(build_node_retry_policy(Settings()), flaky).ainvoke({"n": 0})
    assert out["n"] == 3
    assert attempts["n"] == 3


async def test_non_transient_error_is_not_retried():
    attempts = {"n": 0}

    async def broken(state: _S) -> _S:
        attempts["n"] += 1
        raise ValueError("schema invalid")

    with pytest.raises(ValueError):
        await _compiled(build_node_retry_policy(Settings()), broken).ainvoke({"n": 0})
    assert attempts["n"] == 1
