"""外部系统端口（MCP 只读/写在业务层的抽象）。

权限分层（依据 LLD §1.4「写操作唯一入口」）：
  - `ReadOnly*Port`：图节点（ContextAggregator/ActionPreparation/PreExecutionGuard/ResultVerifier）
    只允许依赖只读协议；
  - `*Port`：在只读协议基础上追加写方法，仅 SagaDispatcher / Worker / 联调脚本持有。
真实实现走 MCP；Mock 用于联调。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class OrderSnapshot:
    order_id: str
    owner_user_id: str
    status: str
    version: str
    category: str | None = None
    currency: str = "CNY"
    lines: list[dict[str, Any]] = field(default_factory=list)
    delivered_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class LogisticsSnapshot:
    waybill_no: str | None
    parcel_status: str
    intercept_command_status: str | None = None
    version: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class RefundStats:
    """买家退换/退款历史统计（风控维度，来自 OMS 买家画像）；缺失即视为不可用。"""
    order_id: str
    refund_count_30d: int
    refund_amount_30d_minor: int
    blacklisted: bool = False
    as_of: str = ""
    version: str = ""


@dataclass
class FulfillmentStatus:
    """补发/履约权威状态（OMS）：结果核验用，缺失即视为不可确认。"""
    order_id: str
    reshipment_status: str
    version: str = ""


@dataclass
class DispatchStatus:
    """催派权威状态（TMS）：结果核验用。"""
    waybill_no: str
    dispatch_status: str
    version: str = ""


@dataclass
class FinanceSnapshot:
    order_id: str
    refundable_balance_minor: int
    reserved_amount_minor: int
    currency: str = "CNY"
    as_of: str = ""
    version: str = ""


# --------------------------------------------------------------------------- #
# 只读协议（图节点只能依赖这些）
# --------------------------------------------------------------------------- #
class ReadOnlyOrderPort(Protocol):
    async def get_order_detail(self, *, tenant_id: str, order_id: str) -> OrderSnapshot: ...
    async def get_buyer_refund_stats(self, *, tenant_id: str, order_id: str) -> RefundStats: ...
    async def get_reshipment_status(self, *, tenant_id: str, order_id: str) -> FulfillmentStatus: ...


class ReadOnlyLogisticsPort(Protocol):
    async def get_tracking_info(self, *, tenant_id: str, order_id: str, waybill_no: str | None) -> LogisticsSnapshot: ...
    async def get_interception_status(self, *, tenant_id: str, waybill_no: str) -> dict: ...
    async def get_dispatch_status(self, *, tenant_id: str, waybill_no: str) -> DispatchStatus: ...


class ReadOnlyFinancePort(Protocol):
    async def get_refundable_balance(self, *, tenant_id: str, order_id: str) -> FinanceSnapshot: ...
    async def calculate_refund_amount(self, *, tenant_id: str, refund_request_id: str, order_id: str,
                                      items: list[dict], policy_decision_id: str, order_version: str) -> dict: ...
    async def get_refund_status(self, *, tenant_id: str, refund_request_id: str) -> dict: ...
    async def get_reconciliation_result(self, *, tenant_id: str, refund_request_id: str) -> dict: ...


# --------------------------------------------------------------------------- #
# 读写协议（仅 SagaDispatcher / Worker 持有）
# --------------------------------------------------------------------------- #
class OrderPort(ReadOnlyOrderPort, Protocol):
    async def hold_fulfillment(self, *, tenant_id: str, order_id: str, idempotency_key: str) -> dict: ...
    async def create_reshipment(self, *, tenant_id: str, order_id: str, sku_ids: list[str], idempotency_key: str) -> dict: ...


class LogisticsPort(ReadOnlyLogisticsPort, Protocol):
    async def intercept_parcel(self, *, tenant_id: str, waybill_no: str, idempotency_key: str) -> dict: ...
    async def expedite_delivery(self, *, tenant_id: str, waybill_no: str, idempotency_key: str) -> dict: ...


class FinancePort(ReadOnlyFinancePort, Protocol):
    async def execute_refund(self, *, tenant_id: str, refund_request_id: str, quote_id: str,
                             quote_hash: str, authorization_ref: str, idempotency_key: str, payload_hash: str) -> dict: ...