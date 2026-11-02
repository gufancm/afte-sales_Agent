# -*- coding: utf-8 -*-
"""工具调用上下文（contextvars）。

图节点在调用只读工具前绑定当前 tenant/ticket/run/trace/node，工具网关据此把每次
调用写入 `tool_calls` 审计（谁、在哪个工单/节点、耗时、状态）。这样只读工具调用也能
被观测与追责，而不是只有 LLM 调用有审计。
"""
from __future__ import annotations

import contextlib
import contextvars
from typing import Iterator

_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("tool_call_ctx", default={})


@contextlib.contextmanager
def bind_tool_context(*, tenant_id: str | None = None, ticket_id: str | None = None,
                      run_id: str | None = None, trace_id: str | None = None,
                      node: str | None = None) -> Iterator[None]:
    """在 with 块内绑定工具调用上下文；退出后恢复。"""
    merged = dict(_ctx.get())
    for k, v in (("tenant_id", tenant_id), ("ticket_id", ticket_id), ("run_id", run_id),
                 ("trace_id", trace_id), ("node", node)):
        if v is not None:
            merged[k] = v
    token = _ctx.set(merged)
    try:
        yield
    finally:
        _ctx.reset(token)


def set_tool_context(*, tenant_id: str | None = None, ticket_id: str | None = None,
                      run_id: str | None = None, trace_id: str | None = None,
                      node: str | None = None) -> None:
    """直接设置（不恢复）工具调用上下文；节点在调用工具前调用即可。

    与 bind_tool_context 的区别：不做作用域恢复。因为每个用工具的节点都会在入口重新设置，
    后一次设置会覆盖前一次，不会串号。
    """
    merged = dict(_ctx.get())
    for k, v in (("tenant_id", tenant_id), ("ticket_id", ticket_id), ("run_id", run_id),
                 ("trace_id", trace_id), ("node", node)):
        if v is not None:
            merged[k] = v
    _ctx.set(merged)


def current_tool_context() -> dict:
    """返回当前工具调用上下文的拷贝（缺省字段为 None）。"""
    c = _ctx.get()
    return {"tenant_id": c.get("tenant_id"), "ticket_id": c.get("ticket_id"),
            "run_id": c.get("run_id"), "trace_id": c.get("trace_id"), "node": c.get("node")}