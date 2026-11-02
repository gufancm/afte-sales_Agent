# -*- coding: utf-8 -*-
"""可观测性：Prometheus 指标（prometheus_client）。

进程内指标注册表；单副本下由 API 的 `/metrics` 抓取。多副本应改集中式（Pushgateway / OTel Collector）。
指标在持久化写入路径统一埋点（内存/SQL 后端一致），因此图节点与 Worker 的写入都会被计入。
"""
from __future__ import annotations

from prometheus_client import (CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram,
                               generate_latest)

REGISTRY = CollectorRegistry()

_MS_BUCKETS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)

# ---- HTTP ----
HTTP_REQUESTS = Counter("aftersale_http_requests_total", "HTTP 请求数", ["method", "path", "status"],
                        registry=REGISTRY)
HTTP_LATENCY = Histogram("aftersale_http_request_duration_ms", "HTTP 请求耗时(ms)", ["method"],
                         buckets=_MS_BUCKETS, registry=REGISTRY)

# ---- LLM ----
LLM_CALLS = Counter("aftersale_llm_calls_total", "LLM 调用数", ["node", "status"], registry=REGISTRY)
LLM_LATENCY = Histogram("aftersale_llm_latency_ms", "LLM 调用耗时(ms)", ["node"],
                        buckets=_MS_BUCKETS, registry=REGISTRY)
LLM_TOKENS = Counter("aftersale_llm_tokens_total", "LLM token 数", ["direction"], registry=REGISTRY)

# ---- 只读工具 ----
TOOL_CALLS = Counter("aftersale_tool_calls_total", "只读 MCP 工具调用数", ["tool", "status"], registry=REGISTRY)
TOOL_LATENCY = Histogram("aftersale_tool_latency_ms", "只读 MCP 工具耗时(ms)", ["tool"],
                         buckets=_MS_BUCKETS, registry=REGISTRY)

# ---- 业务 ----
TICKETS = Counter("aftersale_tickets_total", "新建工单数", registry=REGISTRY)
COMMANDS = Counter("aftersale_commands_total", "入队写命令数", ["action"], registry=REGISTRY)
COMMAND_RESULTS = Counter("aftersale_command_results_total", "写命令终态数", ["status"], registry=REGISTRY)
APPROVALS = Counter("aftersale_approvals_total", "审批决定数", ["decision"], registry=REGISTRY)
RECONCILIATION = Counter("aftersale_reconciliation_total", "对账任务完成数", ["status"], registry=REGISTRY)

# ---- 记忆系统（短期跨轮 / 情景 / 语义）----
MEMORY_SUMMARIES = Counter("aftersale_memory_summaries_total", "滚动摘要生成数", registry=REGISTRY)
MEMORY_EPISODES = Counter("aftersale_memory_episodic_total", "情景记忆归档数", ["outcome_type"],
                          registry=REGISTRY)
MEMORY_RECALLS = Counter("aftersale_memory_recall_total", "情景记忆召回次数", ["hit"], registry=REGISTRY)


def record_llm(*, node=None, status: str = "SUCCESS", latency_ms=None, input_tokens=None,
               output_tokens=None) -> None:
    label = str(node or "unknown")
    LLM_CALLS.labels(node=label, status=str(status)).inc()
    if latency_ms is not None:
        LLM_LATENCY.labels(node=label).observe(float(latency_ms))
    if input_tokens:
        LLM_TOKENS.labels(direction="input").inc(float(input_tokens))
    if output_tokens:
        LLM_TOKENS.labels(direction="output").inc(float(output_tokens))


def record_tool(*, tool=None, status: str = "SUCCESS", latency_ms=None) -> None:
    label = str(tool or "unknown")
    TOOL_CALLS.labels(tool=label, status=str(status)).inc()
    if latency_ms is not None:
        TOOL_LATENCY.labels(tool=label).observe(float(latency_ms))


def render() -> bytes:
    return generate_latest(REGISTRY)


def content_type() -> str:
    return CONTENT_TYPE_LATEST
