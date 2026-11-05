"""Saga Dispatcher：唯一对外写执行者（LLD §18.10）。
从 outbox/saga_commands 领取 PREPARED，调用 MCP 写工具；超时记 UNKNOWN，绝不盲重试。"""
from __future__ import annotations

import asyncio
from typing import Any

from app.mcp.ports import FinancePort, LogisticsPort, OrderPort


class SagaDispatcher:
    def __init__(self, *, orders: OrderPort, logistics: LogisticsPort, finance: FinancePort) -> None:
        self.orders = orders
        self.logistics = logistics
        self.finance = finance

    async def dispatch(self, command: dict[str, Any]) -> dict[str, Any]:
        action = command["action"]
        tenant_id = command["tenant_id"]
        idem = command["idempotency_key"]
        if action in ("REQUEST_REFUND_ONLY", "REQUEST_RETURN_REFUND"):
            return await self.finance.execute_refund(
                tenant_id=tenant_id, refund_request_id=command.get("refund_request_id", ""),
                quote_id=command.get("quote_id") or "", quote_hash=command.get("quote_hash") or "",
                authorization_ref=command.get("authorization_ref") or "", idempotency_key=idem,
                payload_hash=command.get("payload_hash") or "", order_id=command.get("order_id") or "",
            )
        if action == "INTERCEPT_PARCEL":
            return await self.logistics.intercept_parcel(tenant_id=tenant_id,
                                                        waybill_no=command.get("waybill_no") or "", idempotency_key=idem)
        if action == "EXPEDITE_DELIVERY":
            return await self.logistics.expedite_delivery(tenant_id=tenant_id,
                                                          waybill_no=command.get("waybill_no") or "", idempotency_key=idem)
        if action == "CREATE_RESHIPMENT":
            sku_ids = command.get("sku_ids")
            if not sku_ids:
                pl = command.get("payload")
                sku_ids = pl.get("sku_ids") if isinstance(pl, dict) else []
            return await self.orders.create_reshipment(tenant_id=tenant_id, order_id=command.get("order_id") or "",
                                                       sku_ids=list(sku_ids or []), idempotency_key=idem)
        if action == "ESCALATE_MANUAL":
            return {"status": "MANUAL"}
        # 未知动作：不执行，返回人工
        return {"status": "UNSUPPORTED", "action": action}

    async def run_once(self, *, claim_next_command, mark_result, publish_inbox) -> None:
        """单次循环：领取一条命令并派发。并发/退避由外层 Worker 调度。"""
        command = await claim_next_command()
        if not command:
            await asyncio.sleep(0.2)
            return
        try:
            result = await self.dispatch(command)
            await mark_result(command["command_id"], result)
        except (asyncio.TimeoutError, TimeoutError):
            await mark_result(command["command_id"], {"status": "UNKNOWN"})
        except Exception as exc:  # 网络类异常 -> UNKNOWN（§9.8）
            await mark_result(command["command_id"], {"status": "UNKNOWN", "error": str(exc)[:200]})
        finally:
            await publish_inbox(command)
