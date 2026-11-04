"""API 与 SSE（LLD §11）。幂等、版本冲突、事件重放（§18.6）。"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from langgraph.types import Command

from app.api.security import Ctx, get_ctx
from app.domain.canonical import payload_hash, sha256_hex

router = APIRouter(prefix="/api/v1")

_TICKET_STATUS_BY_RESPONSE = {
    "NEED_INFO": "NEED_INFO",
    "DENIED": "REJECTED",
    "MANUAL": "MANUAL",
    "PROCESSING": "PROCESSING",
    "ANSWER": "CLOSED",
    "SUCCEEDED": "COMPLETED",
    "FAILED": "MANUAL",
    "UNKNOWN": "PENDING_RECONCILE",
}


def _ticket_status_for(response_type: str | None, run_status: str | None) -> str | None:
    """run 终态 -> 工单状态（运营视角的索引状态，不是资金事实）。"""
    if run_status == "WAITING_APPROVAL":
        return "PENDING_APPROVAL"
    if run_status == "WAITING_EXTERNAL":
        return "PROCESSING"
    return _TICKET_STATUS_BY_RESPONSE.get(str(response_type or ""), None)


def _interrupt_payload(result: Any) -> dict | None:
    """提取 LangGraph interrupt() 抛出的 payload（HITL 等待审批）。"""
    items = result.get("__interrupt__") if isinstance(result, dict) else None
    if not items:
        return None
    first = items[0]
    value = getattr(first, "value", None)
    if value is None and isinstance(first, dict):
        value = first.get("value")
    return value if isinstance(value, dict) else {"raw": str(value)}


def _deps(request: Request) -> Any:
    return request.app.state.deps


def _persist(request: Request) -> Any:
    return request.app.state.deps.persistence


def _graph_for_ticket(request: Request, ticket: dict):
    """按工单固定版本选图，避免旧 Checkpoint 被 V4 图恢复。"""
    deps = _deps(request)
    return deps.v4_graph if ticket.get("workflow_version") == "v4" else deps.graph


async def _load_memory(request: Request, state: dict, *, query: str) -> None:
    """把记忆装载进 state：短期跨轮历史 + 滚动摘要 + 情景/语义召回。

    硬约束：记忆仅供理解与措辞（非权威），绝不进入政策/风控/资金裁决；
    任何失败都降级为空，绝不影响本轮请求。
    """
    memory = getattr(_deps(request), "memory", None)
    if memory is None:
        return
    tenant_id = state["tenant_id"]
    ticket_id = state["ticket_id"]
    try:
        conv = await memory.load_conversation(tenant_id=tenant_id, ticket_id=ticket_id)
        state.update(conv.as_state())
        ctx = await memory.build_context(tenant_id=tenant_id, user_id=state["user_id"],
                                        ticket_id=ticket_id, query=query)
        if not ctx.is_empty():
            state["memory_context"] = {"profile": ctx.profile, "episodes": ctx.episodes,
                                       "meta": ctx.meta}
        state["memory_meta"] = {**conv.stats, "recall": ctx.meta}
    except Exception:  # noqa: BLE001 - 记忆失败不得影响主流程
        state["memory_meta"] = {"error": "MEMORY_UNAVAILABLE"}


@router.post("/tickets")
async def create_ticket(body: dict, request: Request, ctx: Ctx = Depends(get_ctx),
                        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict:
    intent = body.get("intent") or "UNKNOWN"
    order_id = body.get("order_id")  # 允许 NULL -> 后续 NEED_INFO（§19.1）
    workflow_version = "v4" if _deps(request).settings.workflow_v4_enabled else "v3"
    t = await _persist(request).create_ticket(
        tenant_id=ctx.tenant_id, user_id=ctx.user_id, order_id=order_id, intent=intent,
        workflow_version=workflow_version)
    tid = t["ticket_id"]
    if body.get("initial_message"):
        msg = body["initial_message"]
        await _persist(request).append_message(tenant_id=ctx.tenant_id, ticket_id=tid, thread_id=str(uuid.uuid4()),
                                               role="user", content=msg, content_hash=sha256_hex(msg),
                                               idempotency_key=idempotency_key or str(uuid.uuid4()))
    return {"ticket_id": tid, "status": t["status"], "version": t["version"]}


@router.post("/tickets/{ticket_id}/messages")
async def append_message(ticket_id: str, body: dict, request: Request, ctx: Ctx = Depends(get_ctx),
                         if_match: str | None = Header(default=None, alias="If-Match"),
                         idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict:
    persist = _persist(request)
    ticket = await persist.get_ticket(tenant_id=ctx.tenant_id, ticket_id=ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="TICKET_NOT_FOUND")
    if if_match is not None and str(ticket.get("version")) != str(if_match).strip('"'):
        raise HTTPException(status_code=409, detail="STATE_VERSION_CONFLICT")

    text = body.get("text", "")
    thread_id = body.get("thread_id") or str(uuid.uuid4())
    await persist.append_message(tenant_id=ctx.tenant_id, ticket_id=ticket_id, thread_id=thread_id,
                                 role="user", content=text, content_hash=sha256_hex(text),
                                 idempotency_key=idempotency_key or str(uuid.uuid4()),
                                 client_message_id=body.get("message_id"))
    run_id = await persist.create_run(tenant_id=ctx.tenant_id, ticket_id=ticket_id, thread_id=thread_id)

    # 运行图（简化：同步执行；生产应入队由 Worker 执行）
    deps = _deps(request)
    state = {"tenant_id": ctx.tenant_id, "user_id": ctx.user_id, "ticket_id": ticket_id,
             "thread_id": thread_id, "run_id": run_id, "trace_id": ctx.trace_id,
             # 政策时效以“申请时刻”为基准；允许显式传入以支持回放/补录（缺省取当前东八区时间）
             "claim_at": body.get("claim_at") or datetime.now(timezone(timedelta(hours=8))).isoformat(),
             "messages": [{"role": "user", "content": text}]}
    # 短期跨轮记忆：回填历史 + 滚动摘要 + 情景/语义召回（非权威，仅注入理解/措辞）
    await _load_memory(request, state, query=text)
    thread_key = f"{ticket_id}:{run_id}"
    try:
        graph = _graph_for_ticket(request, ticket)
        result = await graph.ainvoke(state, config={"configurable": {"thread_id": thread_key}})
    except Exception as exc:  # 图异常不得影响已提交的持久化
        await persist.set_run_status(tenant_id=ctx.tenant_id, run_id=run_id, status="FAILED")
        await persist.append_ticket_event(tenant_id=ctx.tenant_id, ticket_id=ticket_id,
                                          event_type="run_failed", payload={"error": str(exc)[:200]})
        raise HTTPException(status_code=500, detail="GRAPH_ERROR")

    interrupt_payload = _interrupt_payload(result)
    if interrupt_payload is not None:
        await persist.set_run_status(tenant_id=ctx.tenant_id, run_id=run_id, status="WAITING_APPROVAL")
        await persist.set_ticket_status(tenant_id=ctx.tenant_id, ticket_id=ticket_id,
                                        status="PENDING_APPROVAL")
        await persist.append_ticket_event(tenant_id=ctx.tenant_id, ticket_id=ticket_id,
                                          event_type="approval_required", payload=interrupt_payload)
        return {"run_id": run_id, "ticket_id": ticket_id, "status": "QUEUED",
                "run_status": "WAITING_APPROVAL", "approval": interrupt_payload,
                "events_url": f"/api/v1/tickets/{ticket_id}/events"}

    status = result.get("response_status") or "COMPLETED"
    await persist.set_run_status(tenant_id=ctx.tenant_id, run_id=run_id, status=status)
    ticket_status = _ticket_status_for(result.get("response_type"), status)
    if ticket_status:
        await persist.set_ticket_status(tenant_id=ctx.tenant_id, ticket_id=ticket_id,
                                        status=ticket_status)
    await persist.append_ticket_event(
        tenant_id=ctx.tenant_id, ticket_id=ticket_id, event_type="message",
        payload={"text": result.get("final_response") or "", "run_status": status})
    return {"run_id": run_id, "ticket_id": ticket_id, "status": "QUEUED",
            "run_status": status, "response_type": result.get("response_type"),
            "response": result.get("final_response"),
            "events_url": f"/api/v1/tickets/{ticket_id}/events"}


@router.get("/tickets/{ticket_id}")
async def get_ticket(ticket_id: str, request: Request, ctx: Ctx = Depends(get_ctx)) -> dict:
    t = await _persist(request).get_ticket(tenant_id=ctx.tenant_id, ticket_id=ticket_id)
    if not t:
        raise HTTPException(status_code=404, detail="TICKET_NOT_FOUND")
    return t


@router.post("/tickets/{ticket_id}/approval")
async def decide_approval(ticket_id: str, body: dict, request: Request, ctx: Ctx = Depends(get_ctx)) -> dict:
    approval_id = body.get("approval_id")
    decision = body.get("decision")
    if decision not in ("APPROVED", "REJECTED"):
        raise HTTPException(status_code=400, detail="INVALID_DECISION")
    persist = _persist(request)
    deps = _deps(request)
    # 审批人角色门：白名单为空表示不启用角色校验（联调）；非空时必须命中
    roles = [r.strip() for r in (getattr(deps.settings, "approval_allowed_roles", "") or "").split(",") if r.strip()]
    try:
        await persist.decide_approval(tenant_id=ctx.tenant_id, approval_id=approval_id,
                                      decision=decision, reviewer_id=ctx.user_id,
                                      reviewer_role=body.get("reviewer_role"),
                                      comment=body.get("comment"), allowed_roles=roles)
    except PermissionError:
        raise HTTPException(status_code=403, detail="REVIEWER_ROLE_NOT_ALLOWED")
    await persist.append_ticket_event(tenant_id=ctx.tenant_id, ticket_id=ticket_id,
                                      event_type="approval", payload={"approval_id": approval_id,
                                                                      "decision": decision,
                                                                      "reviewer_role": body.get("reviewer_role")})

    # HITL 恢复：同一 thread_id 用 Command(resume=...) 继续同一执行片段（§18.3 / §19.1.6）。
    # 恢复节点会重放 interrupt 之前的代码并从 DB 读取真实审批结果（不信任 resume payload）。
    deps = _deps(request)
    ticket = await persist.get_ticket(tenant_id=ctx.tenant_id, ticket_id=ticket_id) or {}
    run_id = ticket.get("active_run_id")
    thread_key = f"{ticket_id}:{run_id}"
    try:
        resumed = await _graph_for_ticket(request, ticket).ainvoke(
            Command(resume={"approval_id": approval_id, "decision": decision}),
            config={"configurable": {"thread_id": thread_key}})
    except Exception as exc:
        await persist.append_ticket_event(tenant_id=ctx.tenant_id, ticket_id=ticket_id,
                                          event_type="approval_resume_failed", payload={"error": str(exc)[:200]})
        return {"approval_id": approval_id, "decision": decision, "resumed": False}
    status = resumed.get("response_status") or "COMPLETED"
    if run_id:
        await persist.set_run_status(tenant_id=ctx.tenant_id, run_id=run_id, status=status)
    ticket_status = _ticket_status_for(resumed.get("response_type"), status)
    if ticket_status:
        await persist.set_ticket_status(tenant_id=ctx.tenant_id, ticket_id=ticket_id,
                                        status=ticket_status)
    await persist.append_ticket_event(
        tenant_id=ctx.tenant_id, ticket_id=ticket_id, event_type="message",
        payload={"text": resumed.get("final_response") or "", "run_status": status})
    return {"approval_id": approval_id, "decision": decision, "resumed": True,
            "run_status": status, "response_type": resumed.get("response_type"),
            "response": resumed.get("final_response")}


@router.get("/tickets/{ticket_id}/events")
async def sse_events(ticket_id: str, request: Request, ctx: Ctx = Depends(get_ctx),
                     last_event_id: str | None = Header(default=None, alias="Last-Event-ID")):
    persist = _persist(request)
    t = await persist.get_ticket(tenant_id=ctx.tenant_id, ticket_id=ticket_id)
    if not t:
        raise HTTPException(status_code=404, detail="TICKET_NOT_FOUND")
    start = int(last_event_id) if (last_event_id and last_event_id.isdigit()) else 0

    async def gen():
        cursor = start
        idle = 0
        while True:
            events = await persist.list_events(tenant_id=ctx.tenant_id, ticket_id=ticket_id, after_sequence=cursor)
            for e in events:
                cursor = e["seq"]
                payload = e["payload"]
                data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
                yield f"id: {e['seq']}\nevent: {e['type']}\ndata: {data}\n\n"
            if events:
                idle = 0
            else:
                idle += 1
                if idle > 60:  # ~30s 无新事件 -> 发心跳
                    yield ": heartbeat\n\n"
                    idle = 0
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream")
