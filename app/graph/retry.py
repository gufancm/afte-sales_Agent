# -*- coding: utf-8 -*-
"""节点级重试策略（LangGraph RetryPolicy）。

设计原则（与工业界「可重入 / 副作用」二分一致）：

1. 只对**瞬时故障**重试：超时、连接重置、上游 5xx。业务拒绝、授权失败、Schema 不合法
   一律立即失败，重试只会放大延迟并掩盖真实原因。
2. 只对**无外部副作用、可安全重入**的节点启用：意图识别、只读事实聚合、政策检索、
   动作规划、回复措辞。这些节点重复执行的结果是幂等的。
3. 资金/物流写、审批暂停、执行前复核、结果回查**一律不配置重试**：
   它们的重试语义必须由 Saga 幂等键 + 租约回收 + 对账 Worker 承担，
   图层面的盲目重放会造成重复退款、重复审批或状态覆盖。

`NO_RETRY_NODES` 不是装饰性的：它显式登记「为什么这里的失败只能上抛」，
并由 `tests/test_graph_retry_policy.py` 断言，避免后续误配。
"""
from __future__ import annotations

from langgraph.types import RetryPolicy

from app.config import Settings
from app.mcp.gateway import is_transient_error

# 可安全重入、无业务副作用的节点：允许图级瞬时重试
RETRY_SAFE_NODES: frozenset[str] = frozenset({
    "intent_guardrail",    # 规则快路径 + LLM 判别；重复执行不改变业务状态
    "context_aggregator",  # 只读端口聚合订单/物流/资金事实
    "policy_evidence",     # 只读检索与证据校验
    "action_planner",      # 纯函数式规划（不落库、不发命令）
    "response_composer",   # 措辞生成，失败可退回模板
})

# 显式禁止重试的节点（写操作 / 暂停点 / 状态回查）
NO_RETRY_NODES: frozenset[str] = frozenset({
    "policy_engine",        # 确定性裁决：重试无收益
    "action_preparation",   # 生成报价引用与计划冻结：重试会产生多份报价
    "risk_authorization",   # 频次/累计额度计数：重试会重复累加
    "hitl_review",          # interrupt 暂停点：重试会丢弃人工审批上下文
    "pre_execution_guard",  # 执行前复核必须一次性判定，不得重放
    "command_submitter",    # 资金/物流写：由 Saga 幂等 + 租约回收负责
    "result_verifier",      # 回查并落库：重试会放大状态写入
})


def build_node_retry_policy(settings: Settings) -> RetryPolicy | None:
    """按配置构造统一的重试策略；关闭时返回 None（LangGraph 视为不重试）。"""
    if not settings.graph_node_retry_enabled:
        return None
    return RetryPolicy(
        max_attempts=max(1, settings.graph_node_retry_max_attempts),
        initial_interval=max(0.0, settings.graph_node_retry_initial_interval_s),
        backoff_factor=max(1.0, settings.graph_node_retry_backoff_factor),
        max_interval=max(0.0, settings.graph_node_retry_max_interval_s),
        jitter=True,
        retry_on=is_transient_error,
    )


def retry_policy_for(node_name: str, settings: Settings) -> RetryPolicy | None:
    """节点名 -> 重试策略。写节点与暂停点永远返回 None。"""
    if node_name in NO_RETRY_NODES or node_name not in RETRY_SAFE_NODES:
        return None
    return build_node_retry_policy(settings)
