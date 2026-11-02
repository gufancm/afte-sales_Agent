# -*- coding: utf-8 -*-
"""MCP（Model Context Protocol）Server：以 JSON-RPC 2.0 / stdio 暴露 15 个售后工具。

为什么要有这一层：
    仓库原有 `app/mcp/` 是「自研 HTTP 工具网关」（鉴权/限流/熔断/审计），协议是 REST 而非 MCP。
    任何 MCP Host（Claude Desktop、Codex、IDE Agent）都无法直接接入。本模块用官方 SDK
    （`mcp>=2.0`）把同一批工具以标准 MCP 工具语义暴露，**复用**既有处理逻辑，不复制业务规则：

      MCP Host --JSON-RPC/stdio--> MCPServer --> ToolTransport --> 现有 mock/真实服务处理器

两层传输可选（环境变量 `MCP_STDIO_BACKEND`）：
    - asgi（默认）：进程内直连 `app.mcp.mocks.server:app`，零网络、零端口，适合本地与 CI；
    - http        ：转发到 `MCP_MOCK_URL`（容器里指向 mcp-mock:9100），适合独立部署。

工具语义：
    - 只读工具标注 readOnlyHint=true；写工具标注 destructiveHint=true 且要求 idempotency_key，
      与 `SagaDispatcher` 的幂等/审计口径保持一致；
    - 传入的 tenant_id 必须与调用方身份一致，缺失由下游拒绝（不在此处放行）。

启动：
    python -m app.mcp.stdio_server                     # stdio（默认）
    python -m app.mcp.stdio_server --transport streamable-http --port 9200
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as _McpToolError
from mcp.types import ToolAnnotations

DEFAULT_TENANT = os.getenv("MCP_DEFAULT_TENANT", "tenant_demo")

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                            idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                        idempotentHint=True, openWorldHint=False)


class ToolCallError(_McpToolError):
    """可预期的工具失败（订单不存在/租户越权等）。

    继承 SDK 的 ToolError：客户端拿到 `isError=true` 且消息里保留业务错误码，
    而不是被折叠成 "Error executing tool xxx" 的未知崩溃。
    """


class ToolTransport:
    """把 MCP 工具调用转发到既有处理器；仅负责传输，不含业务规则。"""

    async def post(self, path: str, payload: dict) -> dict[str, Any]:  # pragma: no cover - 抽象
        raise NotImplementedError

    async def aclose(self) -> None:  # pragma: no cover - 抽象
        return None


class HttpTransport(ToolTransport):
    """转发到已部署的 HTTP 服务（容器内 mcp-mock:9100）。"""

    def __init__(self, base_url: str, *, timeout_s: float = 15.0) -> None:
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_s)

    async def post(self, path: str, payload: dict) -> dict[str, Any]:
        return await _decode(await self._client.post(path, json=payload))

    async def aclose(self) -> None:
        await self._client.aclose()


class AsgiTransport(ToolTransport):
    """进程内直连 FastAPI mock：零网络端口，本地与 CI 使用。"""

    def __init__(self, app: Any | None = None, *, timeout_s: float = 15.0) -> None:
        if app is None:
            from app.mcp.mocks.server import app as mock_app
            app = mock_app
        self._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://mcp-asgi", timeout=timeout_s)

    async def post(self, path: str, payload: dict) -> dict[str, Any]:
        return await _decode(await self._client.post(path, json=payload))

    async def aclose(self) -> None:
        await self._client.aclose()


async def _decode(resp: httpx.Response) -> dict[str, Any]:
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = resp.text[:200]
        raise ToolCallError(f"{resp.status_code} {detail}")
    data = resp.json()
    return data if isinstance(data, dict) else {"result": data}


_TRANSPORT: ToolTransport | None = None


def set_transport(transport: ToolTransport) -> None:
    global _TRANSPORT
    _TRANSPORT = transport


def _t() -> ToolTransport:
    if _TRANSPORT is None:  # pragma: no cover - build_server 一定先设置
        raise ToolCallError("TRANSPORT_NOT_CONFIGURED")
    return _TRANSPORT


def _body(tenant_id: str, **fields: Any) -> dict[str, Any]:
    payload = {"tenant_id": tenant_id}
    payload.update({k: v for k, v in fields.items() if v is not None})
    return payload


# --------------------------------------------------------------------------- #
# OMS（订单）
# --------------------------------------------------------------------------- #
async def oms_get_order_detail(order_id: str, tenant_id: str = DEFAULT_TENANT,
                              authenticated_user_id: str | None = None) -> dict[str, Any]:
    """查询订单权威快照（状态、品类、行项目、版本、归属人）。"""
    return await _t().post("/oms/get_order_detail",
                           _body(tenant_id, order_id=order_id,
                                 authenticated_user_id=authenticated_user_id))


async def oms_get_reshipment_status(order_id: str, tenant_id: str = DEFAULT_TENANT) -> dict[str, Any]:
    """查询补发单履约状态（是否已创建、是否已出库）。"""
    return await _t().post("/oms/get_reshipment_status", _body(tenant_id, order_id=order_id))


async def oms_get_buyer_refund_stats(order_id: str, tenant_id: str = DEFAULT_TENANT) -> dict[str, Any]:
    """查询买家近 30 天退款频次与累计金额（风控维度）。"""
    return await _t().post("/oms/get_buyer_refund_stats", _body(tenant_id, order_id=order_id))


async def oms_hold_fulfillment(order_id: str, idempotency_key: str,
                               tenant_id: str = DEFAULT_TENANT) -> dict[str, Any]:
    """挂起订单履约（发货前拦截）。写操作，必须携带幂等键。"""
    return await _t().post("/oms/hold_fulfillment",
                           _body(tenant_id, order_id=order_id, idempotency_key=idempotency_key))


async def oms_create_reshipment(order_id: str, idempotency_key: str,
                                sku_ids: list[str] | None = None,
                                tenant_id: str = DEFAULT_TENANT) -> dict[str, Any]:
    """创建补发单。写操作，必须携带幂等键。"""
    return await _t().post("/oms/create_reshipment",
                           _body(tenant_id, order_id=order_id, sku_ids=sku_ids or [],
                                 idempotency_key=idempotency_key))


# --------------------------------------------------------------------------- #
# TMS（物流）
# --------------------------------------------------------------------------- #
async def tms_get_tracking_info(order_id: str, tenant_id: str = DEFAULT_TENANT,
                                waybill_no: str | None = None) -> dict[str, Any]:
    """查询运单轨迹与包裹状态。"""
    return await _t().post("/tms/get_tracking_info",
                           _body(tenant_id, order_id=order_id, waybill_no=waybill_no))


async def tms_get_dispatch_status(waybill_no: str, tenant_id: str = DEFAULT_TENANT) -> dict[str, Any]:
    """查询派送状态（是否在派送中、是否已签收）。"""
    return await _t().post("/tms/get_dispatch_status", _body(tenant_id, waybill_no=waybill_no))


async def tms_intercept_parcel(waybill_no: str, idempotency_key: str,
                               tenant_id: str = DEFAULT_TENANT) -> dict[str, Any]:
    """在途拦截包裹。写操作，必须携带幂等键。"""
    return await _t().post("/tms/intercept_parcel",
                           _body(tenant_id, waybill_no=waybill_no, idempotency_key=idempotency_key))


async def tms_get_interception_status(waybill_no: str, tenant_id: str = DEFAULT_TENANT) -> dict[str, Any]:
    """查询拦截结果（拦截中/成功/失败）。"""
    return await _t().post("/tms/get_interception_status", _body(tenant_id, waybill_no=waybill_no))


async def tms_expedite_delivery(waybill_no: str, idempotency_key: str,
                                tenant_id: str = DEFAULT_TENANT) -> dict[str, Any]:
    """催派/催件。写操作，必须携带幂等键。"""
    return await _t().post("/tms/expedite_delivery",
                           _body(tenant_id, waybill_no=waybill_no, idempotency_key=idempotency_key))


# --------------------------------------------------------------------------- #
# Finance（资金）
# --------------------------------------------------------------------------- #
async def finance_get_refundable_balance(order_id: str, tenant_id: str = DEFAULT_TENANT) -> dict[str, Any]:
    """查询可退金额余额（以分为单位）。"""
    return await _t().post("/finance/get_refundable_balance", _body(tenant_id, order_id=order_id))


async def finance_calculate_refund_amount(refund_request_id: str, order_id: str,
                                          policy_decision_id: str, order_version: str,
                                          items: list[dict] | None = None,
                                          tenant_id: str = DEFAULT_TENANT) -> dict[str, Any]:
    """按政策裁决与订单版本计算退款报价（返回 quote_id / quote_hash / 金额）。"""
    return await _t().post("/finance/calculate_refund_amount",
                           _body(tenant_id, refund_request_id=refund_request_id, order_id=order_id,
                                 items=items or [], policy_decision_id=policy_decision_id,
                                 order_version=order_version))


async def finance_execute_refund(refund_request_id: str, quote_id: str, quote_hash: str,
                                 authorization_ref: str, idempotency_key: str,
                                 payload_hash: str, tenant_id: str = DEFAULT_TENANT) -> dict[str, Any]:
    """执行退款（资金写）。必须同时带报价、审批授权与幂等键。"""
    return await _t().post("/finance/execute_refund",
                           _body(tenant_id, refund_request_id=refund_request_id, quote_id=quote_id,
                                 quote_hash=quote_hash, authorization_ref=authorization_ref,
                                 idempotency_key=idempotency_key, payload_hash=payload_hash))


async def finance_get_refund_status(refund_request_id: str, tenant_id: str = DEFAULT_TENANT) -> dict[str, Any]:
    """查询退款单最终状态（PENDING/SUCCESS/FAILED）。"""
    return await _t().post("/finance/get_refund_status",
                           _body(tenant_id, refund_request_id=refund_request_id))


async def finance_get_reconciliation_result(refund_request_id: str,
                                            tenant_id: str = DEFAULT_TENANT) -> dict[str, Any]:
    """查询资金对账结果（是否单边账、是否已平账）。"""
    return await _t().post("/finance/get_reconciliation_result",
                           _body(tenant_id, refund_request_id=refund_request_id))


TOOLS: tuple[tuple[Any, str, ToolAnnotations], ...] = (
    (oms_get_order_detail, "oms.get_order_detail", READ_ONLY),
    (oms_get_reshipment_status, "oms.get_reshipment_status", READ_ONLY),
    (oms_get_buyer_refund_stats, "oms.get_buyer_refund_stats", READ_ONLY),
    (oms_hold_fulfillment, "oms.hold_fulfillment", WRITE),
    (oms_create_reshipment, "oms.create_reshipment", WRITE),
    (tms_get_tracking_info, "tms.get_tracking_info", READ_ONLY),
    (tms_get_dispatch_status, "tms.get_dispatch_status", READ_ONLY),
    (tms_intercept_parcel, "tms.intercept_parcel", WRITE),
    (tms_get_interception_status, "tms.get_interception_status", READ_ONLY),
    (tms_expedite_delivery, "tms.expedite_delivery", WRITE),
    (finance_get_refundable_balance, "finance.get_refundable_balance", READ_ONLY),
    (finance_calculate_refund_amount, "finance.calculate_refund_amount", READ_ONLY),
    (finance_execute_refund, "finance.execute_refund", WRITE),
    (finance_get_refund_status, "finance.get_refund_status", READ_ONLY),
    (finance_get_reconciliation_result, "finance.get_reconciliation_result", READ_ONLY),
)

TOOL_NAMES: tuple[str, ...] = tuple(name for _, name, _ in TOOLS)


def build_server(transport: ToolTransport | None = None) -> MCPServer:
    """注册 15 个售后工具并返回 MCPServer 实例。"""
    if transport is None:
        backend = os.getenv("MCP_STDIO_BACKEND", "asgi").lower()
        if backend == "http":
            transport = HttpTransport(os.getenv("MCP_MOCK_URL", "http://localhost:9100"))
        else:
            transport = AsgiTransport()
    set_transport(transport)

    server = MCPServer(
        name="aftersale-oms",
        version="1.0.0",
        instructions=(
            "电商售后 OMS/TMS/Finance 工具集。只读工具可直接查询；写工具必须携带 idempotency_key，"
            "且只能由具备审批授权的执行器调用。所有调用必须传 tenant_id（默认 tenant_demo）。"
        ),
    )
    for fn, name, annotations in TOOLS:
        # structured_output=True：除文本外同时回传 structuredContent，便于 Host 直接消费
        server.add_tool(fn, name=name, annotations=annotations, structured_output=True)
    return server


def main(argv: list[str] | None = None) -> int:
    # stdio 模式下 stdout 是 JSON-RPC 通道，任何日志都必须走 stderr
    import logging
    logging.basicConfig(level=os.getenv("MCP_LOG_LEVEL", "WARNING"), stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    ap = argparse.ArgumentParser(description="aftersale MCP Server（stdio / streamable-http）")
    ap.add_argument("--transport", default="stdio", choices=["stdio", "streamable-http"])
    ap.add_argument("--backend", default=os.getenv("MCP_STDIO_BACKEND", "asgi"),
                    choices=["asgi", "http"])
    ap.add_argument("--mock-url", default=os.getenv("MCP_MOCK_URL", "http://localhost:9100"))
    args = ap.parse_args(argv)

    transport: ToolTransport = (HttpTransport(args.mock_url) if args.backend == "http"
                                else AsgiTransport())
    server = build_server(transport)
    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(transport="streamable-http")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
