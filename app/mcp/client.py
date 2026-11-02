"""MCP 只读/写客户端（HTTP）。写调用只允许由 SagaDispatcher 经此发起（§18.10）。

只读方法会被 ToolGateway 包装后再交给图节点；写方法仅由 SagaDispatcher 持有原始端口调用。
HTTP 状态码统一映射为结构化异常：404 -> ToolNotFound、401/403 -> ToolAuthorizationError、
5xx/网络错误 -> 交给网关按「可重试」处理。
"""
from __future__ import annotations

from typing import Any

import httpx

from app.mcp.gateway import ToolAuthorizationError, ToolNotFound
from app.mcp.ports import (DispatchStatus, FinanceSnapshot, FulfillmentStatus, LogisticsSnapshot,
                           OrderSnapshot, RefundStats)


def _detail(resp: httpx.Response, default: str) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("detail"):
            return str(body["detail"])
    except Exception:
        pass
    return default


async def _post(base_url: str, path: str, payload: dict, timeout_s: float) -> dict:
    async with httpx.AsyncClient(timeout=timeout_s) as c:
        r = await c.post(f"{base_url}{path}", json=payload)
    if r.status_code == 404:
        raise ToolNotFound(_detail(r, f"NOT_FOUND:{path}"))
    if r.status_code in (401, 403):
        raise ToolAuthorizationError(_detail(r, f"FORBIDDEN:{path}"))
    if r.status_code >= 400:
        r.raise_for_status()  # 其余 4xx/5xx：4xx 由网关判为不可重试，5xx 可重试
    return r.json()


class HttpOMS:
    def __init__(self, base_url: str, *, timeout_s: float = 15.0,
                 client: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._client = client

    async def get_order_detail(self, *, tenant_id: str, order_id: str) -> OrderSnapshot:
        data = await _post(self.base_url, "/oms/get_order_detail",
                           {"tenant_id": tenant_id, "order_id": order_id}, self._timeout_s)
        return OrderSnapshot(**data)

    async def get_buyer_refund_stats(self, *, tenant_id: str, order_id: str) -> RefundStats:
        data = await _post(self.base_url, "/oms/get_buyer_refund_stats",
                           {"tenant_id": tenant_id, "order_id": order_id}, self._timeout_s)
        return RefundStats(**data)

    async def get_reshipment_status(self, *, tenant_id: str, order_id: str) -> FulfillmentStatus:
        data = await _post(self.base_url, "/oms/get_reshipment_status",
                           {"tenant_id": tenant_id, "order_id": order_id}, self._timeout_s)
        return FulfillmentStatus(**data)

    async def hold_fulfillment(self, *, tenant_id: str, order_id: str, idempotency_key: str) -> dict:
        return await _post(self.base_url, "/oms/hold_fulfillment",
                           {"tenant_id": tenant_id, "order_id": order_id,
                            "idempotency_key": idempotency_key}, self._timeout_s)

    async def create_reshipment(self, *, tenant_id: str, order_id: str, sku_ids: list[str],
                                idempotency_key: str) -> dict:
        return await _post(self.base_url, "/oms/create_reshipment",
                           {"tenant_id": tenant_id, "order_id": order_id, "sku_ids": sku_ids,
                            "idempotency_key": idempotency_key}, self._timeout_s)


class HttpTMS:
    def __init__(self, base_url: str, *, timeout_s: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    async def get_tracking_info(self, *, tenant_id: str, order_id: str,
                                waybill_no: str | None) -> LogisticsSnapshot:
        data = await _post(self.base_url, "/tms/get_tracking_info",
                           {"tenant_id": tenant_id, "order_id": order_id,
                            "waybill_no": waybill_no}, self._timeout_s)
        return LogisticsSnapshot(**data)

    async def get_dispatch_status(self, *, tenant_id: str, waybill_no: str) -> DispatchStatus:
        data = await _post(self.base_url, "/tms/get_dispatch_status",
                           {"tenant_id": tenant_id, "waybill_no": waybill_no}, self._timeout_s)
        return DispatchStatus(**data)

    async def intercept_parcel(self, *, tenant_id: str, waybill_no: str, idempotency_key: str) -> dict:
        return await _post(self.base_url, "/tms/intercept_parcel",
                           {"tenant_id": tenant_id, "waybill_no": waybill_no,
                            "idempotency_key": idempotency_key}, self._timeout_s)

    async def get_interception_status(self, *, tenant_id: str, waybill_no: str) -> dict:
        return await _post(self.base_url, "/tms/get_interception_status",
                           {"tenant_id": tenant_id, "waybill_no": waybill_no}, self._timeout_s)

    async def expedite_delivery(self, *, tenant_id: str, waybill_no: str, idempotency_key: str) -> dict:
        return await _post(self.base_url, "/tms/expedite_delivery",
                           {"tenant_id": tenant_id, "waybill_no": waybill_no,
                            "idempotency_key": idempotency_key}, self._timeout_s)


class HttpFinance:
    def __init__(self, base_url: str, *, timeout_s: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    async def get_refundable_balance(self, *, tenant_id: str, order_id: str) -> FinanceSnapshot:
        data = await _post(self.base_url, "/finance/get_refundable_balance",
                           {"tenant_id": tenant_id, "order_id": order_id}, self._timeout_s)
        return FinanceSnapshot(**data)

    async def calculate_refund_amount(self, *, tenant_id: str, refund_request_id: str, order_id: str,
                                      items: list[dict], policy_decision_id: str, order_version: str) -> dict:
        return await _post(self.base_url, "/finance/calculate_refund_amount", {
            "tenant_id": tenant_id, "refund_request_id": refund_request_id, "order_id": order_id,
            "items": items, "policy_decision_id": policy_decision_id,
            "order_version": order_version}, self._timeout_s)

    async def execute_refund(self, *, tenant_id: str, refund_request_id: str, quote_id: str,
                             quote_hash: str, authorization_ref: str, idempotency_key: str,
                             payload_hash: str) -> dict:
        return await _post(self.base_url, "/finance/execute_refund", {
            "tenant_id": tenant_id, "refund_request_id": refund_request_id, "quote_id": quote_id,
            "quote_hash": quote_hash, "authorization_ref": authorization_ref,
            "idempotency_key": idempotency_key, "payload_hash": payload_hash}, self._timeout_s)

    async def get_refund_status(self, *, tenant_id: str, refund_request_id: str) -> dict:
        return await _post(self.base_url, "/finance/get_refund_status",
                           {"tenant_id": tenant_id, "refund_request_id": refund_request_id}, self._timeout_s)

    async def get_reconciliation_result(self, *, tenant_id: str, refund_request_id: str) -> dict:
        return await _post(self.base_url, "/finance/get_reconciliation_result",
                           {"tenant_id": tenant_id, "refund_request_id": refund_request_id}, self._timeout_s)