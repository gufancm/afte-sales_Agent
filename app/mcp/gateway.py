# -*- coding: utf-8 -*-
"""工具网关（Tool Gateway）：所有外部只读工具调用的统一入口（LLD §1.4 / §2.7 / §7.4.2）。

把「节点直接 httpx 调 MCP」升级为走一层网关，集中提供：
  1) 只读/写能力隔离：网关只包装只读方法；写方法不经网关（仅 SagaDispatcher 持有原始端口）；
  2) 强制租户边界：调用前校验 tenant_id（来自认证上下文），缺失即拒绝；
  3) 超时 + 指数退避重试（仅对瞬时错误重试，4xx/校验错误绝不重试）；
  4) 熔断（连续失败达阈值后冷却期内快速失败）；
  5) 限流（按工具令牌桶）与并发上限（信号量）；
  6) 响应 Schema 校验（字段缺失/类型错误 -> ToolSchemaError，不把半残数据交给下游）；
  7) 调用审计：每次调用写 `tool_calls`（谁/哪个工单节点/耗时/状态/重试次数）。

设计原则：失败必须显式、可观测、可分级（关键事实 vs 次要事实），绝不静默吞掉。
"""
from __future__ import annotations

import asyncio
import dataclasses
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.mcp.ports import (DispatchStatus, FinanceSnapshot, FulfillmentStatus, LogisticsSnapshot,
                           OrderSnapshot, RefundStats)
from app.mcp.toolctx import current_tool_context


# --------------------------------------------------------------------------- #
# 异常体系：区分「可重试」「不可重试」「授权」「Schema」四类
# --------------------------------------------------------------------------- #
class ToolError(Exception):
    """所有工具层异常的基类；`code` 用于审计与节点错误映射。"""
    code = "TOOL_ERROR"


class ToolTimeout(ToolError):
    code = "TOOL_TIMEOUT"


class ToolUnavailable(ToolError):
    code = "TOOL_UNAVAILABLE"


class ToolCircuitOpen(ToolError):
    code = "TOOL_CIRCUIT_OPEN"


class ToolRateLimited(ToolError):
    code = "TOOL_RATE_LIMITED"


class ToolSchemaError(ToolError):
    code = "TOOL_SCHEMA_INVALID"


class ToolAuthorizationError(ToolError):
    code = "TOOL_UNAUTHORIZED"


class ToolNotFound(ToolError):
    code = "TOOL_NOT_FOUND"


class ToolUpstreamError(ToolError):
    code = "TOOL_UPSTREAM_ERROR"


class TransientToolError(ToolError):
    """本地/Mock 客户端可主动抛出的瞬时错误，触发网关重试。"""
    code = "TOOL_TRANSIENT"


# --------------------------------------------------------------------------- #
# 策略
# --------------------------------------------------------------------------- #
@dataclass
class ToolPolicy:
    timeout_s: float = 10.0
    max_retries: int = 2
    backoff_base_s: float = 0.15
    backoff_max_s: float = 1.0
    failure_threshold: int = 5
    cooldown_s: float = 30.0
    rate_limit_per_s: float = 50.0


@dataclass
class _Bucket:
    tokens: float
    updated: float


@dataclass
class _Breaker:
    failures: int = 0
    opened_at: float | None = None
    trial_in_flight: bool = False


def build_policies(settings: Settings) -> dict[str, ToolPolicy]:
    """按配置构造各工具策略（当前所有只读工具共用同一组配置）。"""
    base = ToolPolicy(
        timeout_s=max(0.1, settings.mcp_tool_timeout_ms / 1000),
        max_retries=max(0, settings.mcp_tool_max_retries),
        backoff_base_s=max(0.0, settings.mcp_tool_backoff_base_ms / 1000),
        backoff_max_s=max(0.0, settings.mcp_tool_backoff_max_ms / 1000),
        failure_threshold=max(1, settings.mcp_tool_circuit_failure_threshold),
        cooldown_s=max(0.0, settings.mcp_tool_circuit_cooldown_ms / 1000),
        rate_limit_per_s=max(0.1, settings.mcp_tool_rate_limit_per_s),
    )
    return {name: dataclasses.replace(base) for name in _KNOWN_TOOLS}


_KNOWN_TOOLS = (
    "oms.get_order_detail",
    "oms.get_buyer_refund_stats",
    "oms.get_reshipment_status",
    "tms.get_dispatch_status",
    "tms.get_tracking_info",
    "tms.get_interception_status",
    "finance.get_refundable_balance",
    "finance.calculate_refund_amount",
    "finance.get_refund_status",
    "finance.get_reconciliation_result",
)


# --------------------------------------------------------------------------- #
# 响应 Schema 校验（不信任上游返回；缺字段/类型错 -> ToolSchemaError）
# --------------------------------------------------------------------------- #
class _OrderIn(BaseModel):
    model_config = {"extra": "ignore"}
    order_id: str
    owner_user_id: str
    status: str
    version: str = "v0"
    category: str | None = None
    currency: str = "CNY"
    lines: list[dict] = []
    delivered_at: str | None = None


class _LogisticsIn(BaseModel):
    model_config = {"extra": "ignore"}
    waybill_no: str | None = None
    parcel_status: str
    intercept_command_status: str | None = None
    version: str = ""


class _FinanceIn(BaseModel):
    model_config = {"extra": "ignore"}
    order_id: str
    refundable_balance_minor: int
    reserved_amount_minor: int = 0
    currency: str = "CNY"
    as_of: str = ""
    version: str = ""


class _RefundStatsIn(BaseModel):
    model_config = {"extra": "ignore"}
    order_id: str
    refund_count_30d: int
    refund_amount_30d_minor: int
    blacklisted: bool = False
    as_of: str = ""
    version: str = ""


class _ReshipmentStatusIn(BaseModel):
    model_config = {"extra": "ignore"}
    order_id: str
    reshipment_status: str
    version: str = ""


class _DispatchStatusIn(BaseModel):
    model_config = {"extra": "ignore"}
    waybill_no: str
    dispatch_status: str
    version: str = ""


class _QuoteIn(BaseModel):
    model_config = {"extra": "ignore"}
    refund_request_id: str
    quote_id: str
    quote_hash: str
    amount_minor: int
    currency: str = "CNY"
    order_version: str = ""
    policy_decision_id: str = ""
    expires_at: str | None = None
    reservation_reference: str | None = None
    reservation_expires_at: str | None = None
    available_balance_minor: int | None = None


class _StatusIn(BaseModel):
    model_config = {"extra": "ignore"}
    status: str


class _InterceptIn(BaseModel):
    model_config = {"extra": "ignore"}
    intercept_command_status: str = "UNKNOWN"
    waybill_no: str | None = None


def _mapping(obj: Any, tool: str) -> dict:
    if isinstance(obj, dict):
        return obj
    if dataclasses.is_dataclass(obj):
        return {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
    raise ToolSchemaError(f"{tool}: 期望 dict 或 dataclass，实际 {type(obj).__name__}")


def _validate(model: type[BaseModel], obj: Any, tool: str) -> BaseModel:
    try:
        return model.model_validate(_mapping(obj, tool))
    except ValidationError as exc:
        raise ToolSchemaError(f"{tool}: 响应字段校验失败 {exc.errors()[:3]}") from exc


def validate_order(raw: Any) -> OrderSnapshot:
    m: _OrderIn = _validate(_OrderIn, raw, "oms.get_order_detail")  # type: ignore[assignment]
    return OrderSnapshot(order_id=m.order_id, owner_user_id=m.owner_user_id, status=m.status,
                         version=m.version, category=m.category, currency=m.currency,
                         lines=[dict(x) for x in m.lines], delivered_at=m.delivered_at)


def validate_refund_stats(raw: Any) -> RefundStats:
    m: _RefundStatsIn = _validate(_RefundStatsIn, raw, "oms.get_buyer_refund_stats")  # type: ignore[assignment]
    return RefundStats(order_id=m.order_id, refund_count_30d=m.refund_count_30d,
                       refund_amount_30d_minor=m.refund_amount_30d_minor, blacklisted=m.blacklisted,
                       as_of=m.as_of, version=m.version)


def validate_logistics(raw: Any) -> LogisticsSnapshot:
    m: _LogisticsIn = _validate(_LogisticsIn, raw, "tms.get_tracking_info")  # type: ignore[assignment]
    return LogisticsSnapshot(waybill_no=m.waybill_no, parcel_status=m.parcel_status,
                             intercept_command_status=m.intercept_command_status, version=m.version)


def validate_reshipment_status(raw: Any) -> FulfillmentStatus:
    m: _ReshipmentStatusIn = _validate(_ReshipmentStatusIn, raw, "oms.get_reshipment_status")  # type: ignore[assignment]
    return FulfillmentStatus(order_id=m.order_id, reshipment_status=m.reshipment_status, version=m.version)


def validate_dispatch_status(raw: Any) -> DispatchStatus:
    m: _DispatchStatusIn = _validate(_DispatchStatusIn, raw, "tms.get_dispatch_status")  # type: ignore[assignment]
    return DispatchStatus(waybill_no=m.waybill_no, dispatch_status=m.dispatch_status, version=m.version)


def validate_finance(raw: Any) -> FinanceSnapshot:
    m: _FinanceIn = _validate(_FinanceIn, raw, "finance.get_refundable_balance")  # type: ignore[assignment]
    return FinanceSnapshot(order_id=m.order_id, refundable_balance_minor=m.refundable_balance_minor,
                           reserved_amount_minor=m.reserved_amount_minor, currency=m.currency,
                           as_of=m.as_of, version=m.version)


def validate_quote(raw: Any) -> dict:
    m: _QuoteIn = _validate(_QuoteIn, raw, "finance.calculate_refund_amount")  # type: ignore[assignment]
    out = m.model_dump()
    if isinstance(raw, dict):
        out = {**raw, **out}  # 保留 breakdown 等附加字段
    return out


def validate_status(raw: Any) -> dict:
    m: _StatusIn = _validate(_StatusIn, raw, "finance.status")  # type: ignore[assignment]
    return {**(raw if isinstance(raw, dict) else {}), **m.model_dump()}


def validate_intercept(raw: Any) -> dict:
    m: _InterceptIn = _validate(_InterceptIn, raw, "tms.get_interception_status")  # type: ignore[assignment]
    return {**(raw if isinstance(raw, dict) else {}), **m.model_dump()}


# --------------------------------------------------------------------------- #
# 网关本体
# --------------------------------------------------------------------------- #
def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, TransientToolError,
                        ConnectionError, OSError)):
        return True
    if isinstance(exc, ToolError):
        return False
    try:  # httpx 可能未安装（纯内存模式）
        import httpx  # type: ignore
        if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
            return True
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500:
            return True
    except Exception:  # pragma: no cover
        pass
    return type(exc).__name__ in (
        "ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout",
        "PoolTimeout", "ReadError", "RemoteProtocolError", "NetworkError",
    )


def is_transient_error(exc: BaseException) -> bool:
    """公开的「瞬时故障」判定：网关重试与 LangGraph 节点级 RetryPolicy 共用同一口径。

    只认超时/连接类/上游 5xx；业务错误、授权错误、Schema 错误一律不重试。
    """
    return _is_transient(exc)


def _normalize(exc: BaseException, tool: str, policy: ToolPolicy) -> ToolError:
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return ToolTimeout(f"{tool}: 超时（>{policy.timeout_s}s）")
    if _is_transient(exc):
        return ToolUnavailable(f"{tool}: {type(exc).__name__}: {exc}")
    return ToolUpstreamError(f"{tool}: {type(exc).__name__}: {exc}")


class ToolGateway:
    """只读工具调用的统一入口：超时/重试/熔断/限流/审计/校验。"""

    def __init__(self, *, policies: dict[str, ToolPolicy] | None = None,
                 default_policy: ToolPolicy | None = None,
                 on_call: Callable[[dict], Awaitable[None]] | None = None,
                 max_concurrency: int = 8,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._policies = policies or {}
        self._default = default_policy or ToolPolicy()
        self._on_call = on_call
        self._sem = asyncio.Semaphore(max(1, max_concurrency))
        self._breakers: dict[str, _Breaker] = {}
        self._buckets: dict[str, _Bucket] = {}
        self._clock = clock
        self.calls: list[dict] = []  # 进程内指标（内存/SQL 之外的红利）

    # ---------------- 内部：熔断 / 限流 ----------------
    def _policy(self, tool: str) -> ToolPolicy:
        return self._policies.get(tool, self._default)

    def _breaker_open(self, tool: str, policy: ToolPolicy) -> bool:
        b = self._breakers.setdefault(tool, _Breaker())
        if b.opened_at is None:
            return False
        if self._clock() - b.opened_at < policy.cooldown_s:
            return True
        # 冷却结束：半开，放行一次试探
        if b.trial_in_flight:
            return True
        b.trial_in_flight = True
        return False

    def _note_success(self, tool: str) -> None:
        b = self._breakers.setdefault(tool, _Breaker())
        b.failures = 0
        b.opened_at = None
        b.trial_in_flight = False

    def _note_failure(self, tool: str, policy: ToolPolicy) -> None:
        b = self._breakers.setdefault(tool, _Breaker())
        b.trial_in_flight = False
        b.failures += 1
        if b.failures >= policy.failure_threshold:
            b.opened_at = self._clock()

    async def _acquire_rate(self, tool: str, policy: ToolPolicy) -> None:
        now = self._clock()
        b = self._buckets.setdefault(tool, _Bucket(tokens=policy.rate_limit_per_s, updated=now))
        capacity = max(1.0, policy.rate_limit_per_s)
        b.tokens = min(capacity, b.tokens + (now - b.updated) * policy.rate_limit_per_s)
        b.updated = now
        if b.tokens < 1.0:
            wait = (1.0 - b.tokens) / policy.rate_limit_per_s
            await asyncio.sleep(wait)
            now = self._clock()
            b.tokens = min(capacity, b.tokens + (now - b.updated) * policy.rate_limit_per_s)
            b.updated = now
        b.tokens -= 1.0

    # ---------------- 对外：执行一次只读调用 ----------------
    async def call(self, *, tool: str, func: Callable[[], Awaitable[Any]],
                   validator: Callable[[Any], Any] | None = None,
                   critical: bool = True, tenant_id: str | None = None) -> Any:
        ctx = current_tool_context()
        # 显式传入的 tenant_id 优先（直接调用场景）；否则用绑定的认证上下文
        tenant_id = tenant_id or ctx.get("tenant_id")
        policy = self._policy(tool)
        started = self._clock()
        attempts = 0
        status, err_code = "SUCCESS", None
        try:
            if not tenant_id:
                raise ToolAuthorizationError(f"{tool}: 缺少 tenant_id（认证上下文）")
            if self._breaker_open(tool, policy):
                raise ToolCircuitOpen(f"{tool}: 熔断打开（冷却中）")
            await self._acquire_rate(tool, policy)
            async with self._sem:
                while True:
                    attempts += 1
                    try:
                        out = await asyncio.wait_for(func(), timeout=policy.timeout_s)
                        if validator is not None:
                            out = validator(out)
                        self._note_success(tool)
                        return out
                    except (ToolSchemaError, ToolAuthorizationError, ToolNotFound):
                        self._note_failure(tool, policy)
                        raise
                    except BaseException as exc:  # noqa: BLE001 - 需按可重试性分类
                        if _is_transient(exc) and attempts <= policy.max_retries:
                            delay = min(policy.backoff_max_s,
                                        policy.backoff_base_s * (2 ** (attempts - 1)))
                            if delay > 0:
                                await asyncio.sleep(delay)
                            continue
                        self._note_failure(tool, policy)
                        raise _normalize(exc, tool, policy)
        except ToolError as exc:
            status, err_code = "ERROR", exc.code
            raise
        finally:
            latency_ms = int((self._clock() - started) * 1000)
            await self._emit(tool=tool, tenant_id=tenant_id, ctx=ctx, status=status,
                             error_code=err_code, attempts=attempts, latency_ms=latency_ms,
                             critical=critical)

    async def _emit(self, *, tool: str, tenant_id: str | None, ctx: dict, status: str,
                    error_code: str | None, attempts: int, latency_ms: int, critical: bool) -> None:
        record = {"tool": tool, "tenant_id": tenant_id, "ticket_id": ctx.get("ticket_id"),
                  "run_id": ctx.get("run_id"), "trace_id": ctx.get("trace_id"),
                  "node": ctx.get("node"), "status": status, "error_code": error_code,
                  "attempts": attempts, "latency_ms": latency_ms, "critical": critical}
        self.calls.append(record)
        if self._on_call is None:
            return
        try:
            await self._on_call(record)
        except Exception:  # 审计失败绝不影响业务调用
            pass


# --------------------------------------------------------------------------- #
# 只读门面（facade）：只暴露只读方法；写方法仅存在于原始端口（SagaDispatcher 持有）
# --------------------------------------------------------------------------- #
class ReadOnlyOrderTools:
    def __init__(self, raw: Any, gw: ToolGateway) -> None:
        self._raw = raw
        self._gw = gw

    async def get_order_detail(self, *, tenant_id: str, order_id: str) -> OrderSnapshot:
        async def _do() -> Any:
            return await self._raw.get_order_detail(tenant_id=tenant_id, order_id=order_id)
        return await self._gw.call(tool="oms.get_order_detail", tenant_id=tenant_id, func=_do, validator=validate_order)

    async def get_buyer_refund_stats(self, *, tenant_id: str, order_id: str) -> RefundStats:
        async def _do() -> Any:
            return await self._raw.get_buyer_refund_stats(tenant_id=tenant_id, order_id=order_id)
        return await self._gw.call(tool="oms.get_buyer_refund_stats", tenant_id=tenant_id, func=_do,
                                   validator=validate_refund_stats, critical=False)

    async def get_reshipment_status(self, *, tenant_id: str, order_id: str) -> FulfillmentStatus:
        async def _do() -> Any:
            return await self._raw.get_reshipment_status(tenant_id=tenant_id, order_id=order_id)
        return await self._gw.call(tool="oms.get_reshipment_status", tenant_id=tenant_id, func=_do,
                                   validator=validate_reshipment_status, critical=False)


class ReadOnlyLogisticsTools:
    def __init__(self, raw: Any, gw: ToolGateway) -> None:
        self._raw = raw
        self._gw = gw

    async def get_tracking_info(self, *, tenant_id: str, order_id: str,
                                waybill_no: str | None) -> LogisticsSnapshot:
        async def _do() -> Any:
            return await self._raw.get_tracking_info(tenant_id=tenant_id, order_id=order_id,
                                                     waybill_no=waybill_no)
        return await self._gw.call(tool="tms.get_tracking_info", tenant_id=tenant_id, func=_do, validator=validate_logistics)

    async def get_interception_status(self, *, tenant_id: str, waybill_no: str) -> dict:
        async def _do() -> Any:
            return await self._raw.get_interception_status(tenant_id=tenant_id, waybill_no=waybill_no)
        return await self._gw.call(tool="tms.get_interception_status", tenant_id=tenant_id, func=_do,
                                   validator=validate_intercept, critical=False)

    async def get_dispatch_status(self, *, tenant_id: str, waybill_no: str) -> DispatchStatus:
        async def _do() -> Any:
            return await self._raw.get_dispatch_status(tenant_id=tenant_id, waybill_no=waybill_no)
        return await self._gw.call(tool="tms.get_dispatch_status", tenant_id=tenant_id, func=_do,
                                   validator=validate_dispatch_status, critical=False)


class ReadOnlyFinanceTools:
    def __init__(self, raw: Any, gw: ToolGateway) -> None:
        self._raw = raw
        self._gw = gw

    async def get_refundable_balance(self, *, tenant_id: str, order_id: str) -> FinanceSnapshot:
        async def _do() -> Any:
            return await self._raw.get_refundable_balance(tenant_id=tenant_id, order_id=order_id)
        return await self._gw.call(tool="finance.get_refundable_balance", tenant_id=tenant_id, func=_do,
                                   validator=validate_finance)

    async def calculate_refund_amount(self, *, tenant_id: str, refund_request_id: str, order_id: str,
                                      items: list[dict], policy_decision_id: str,
                                      order_version: str) -> dict:
        async def _do() -> Any:
            return await self._raw.calculate_refund_amount(
                tenant_id=tenant_id, refund_request_id=refund_request_id, order_id=order_id,
                items=items, policy_decision_id=policy_decision_id, order_version=order_version)
        return await self._gw.call(tool="finance.calculate_refund_amount", tenant_id=tenant_id, func=_do,
                                   validator=validate_quote)

    async def get_refund_status(self, *, tenant_id: str, refund_request_id: str) -> dict:
        async def _do() -> Any:
            return await self._raw.get_refund_status(tenant_id=tenant_id, refund_request_id=refund_request_id)
        return await self._gw.call(tool="finance.get_refund_status", tenant_id=tenant_id, func=_do,
                                   validator=validate_status, critical=False)

    async def get_reconciliation_result(self, *, tenant_id: str, refund_request_id: str) -> dict:
        async def _do() -> Any:
            return await self._raw.get_reconciliation_result(tenant_id=tenant_id,
                                                            refund_request_id=refund_request_id)
        return await self._gw.call(tool="finance.get_reconciliation_result", tenant_id=tenant_id, func=_do,
                                   validator=validate_status, critical=False)


@dataclass
class WriteTools:
    """原始（可写）端口集合：仅供 SagaDispatcher / Worker / 联调脚本持有，图节点不得引用。"""
    orders: Any
    logistics: Any
    finance: Any


@dataclass
class ReadToolkit:
    orders: ReadOnlyOrderTools
    logistics: ReadOnlyLogisticsTools
    finance: ReadOnlyFinanceTools


def build_read_toolkit(raw: WriteTools, gateway: ToolGateway) -> ReadToolkit:
    return ReadToolkit(orders=ReadOnlyOrderTools(raw.orders, gateway),
                       logistics=ReadOnlyLogisticsTools(raw.logistics, gateway),
                       finance=ReadOnlyFinanceTools(raw.finance, gateway))
