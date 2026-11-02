# -*- coding: utf-8 -*-
"""context_aggregator 与工具网关（Tool Gateway）的回归测试。

覆盖针对评审 8 项的完善：
  1) 端口读写拆分：图只拿到只读门面，无可写方法；节点源码不引用 deps.tools
  2) 统一工具网关：超时/重试/熔断/限流/审计集中实现
  3) 归属/租户校验：mock 端强制租户；网关强制 tenant 非空
  4) 只读调用审计：tool_calls 落库（内存）
  5) 熔断：连续失败达阈值后快速失败
  6) 故障分级：关键事实 fail-closed，次要事实降级
  7) 响应 Schema 校验：字段缺失 -> ToolSchemaError，不重试
  8) facts_version 纳入 finance 版本
"""
from __future__ import annotations

import pathlib
import time

import pytest

pytest.importorskip("langgraph")

from app.bootstrap import build_deps  # noqa: E402
from app.config import Settings  # noqa: E402
from app.graph import nodes  # noqa: E402
from app.graph.deps import Deps  # noqa: E402
from app.mcp.gateway import (ReadOnlyOrderTools, ToolAuthorizationError, ToolCircuitOpen,  # noqa: E402
                             ToolGateway, ToolPolicy, ToolSchemaError, ToolUpstreamError,
                             TransientToolError, WriteTools, build_policies, build_read_toolkit)
from app.mcp.ports import OrderSnapshot  # noqa: E402


def _deps() -> Deps:
    return build_deps(Settings(_env_file=None, app_env="test", database_url="memory://", llm_model=""))


def _rebuild_read(deps: Deps, *, orders=None, logistics=None, finance=None) -> None:
    """用给定原始端口重建只读门面（模拟某个下游不可用）。"""
    raw = WriteTools(orders=orders or deps.tools.orders,
                     logistics=logistics or deps.tools.logistics,
                     finance=finance or deps.tools.finance)

    async def _on(rec: dict) -> None:
        await deps.persistence.record_tool_call(**rec)

    gw = ToolGateway(policies=build_policies(deps.settings), on_call=_on, max_concurrency=4)
    read = build_read_toolkit(raw, gw)
    deps.orders, deps.logistics, deps.finance = read.orders, read.logistics, read.finance


def _state(intent: str = "RETURN_REFUND", *, order_id: str = "o_1002") -> dict:
    return {"tenant_id": "tenant_demo", "user_id": "u_demo", "ticket_id": "tk1", "thread_id": "th1",
            "run_id": "r1", "trace_id": "tr1", "slots": {"order_id": order_id},
            "intent_result": {"intent": intent}}


class _FailOrders:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def get_order_detail(self, **kw):  # noqa: ANN003
        raise self.exc


class _FailLogistics:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def get_tracking_info(self, **kw):  # noqa: ANN003
        raise self.exc


class _FailFinance:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def get_refundable_balance(self, **kw):  # noqa: ANN003
        raise self.exc


# --------------------------------------------------------------------------- #
# 1) 读写端口拆分
# --------------------------------------------------------------------------- #
def test_read_facades_expose_no_write_methods():
    deps = _deps()
    assert not hasattr(deps.orders, "hold_fulfillment")
    assert not hasattr(deps.orders, "create_reshipment")
    assert not hasattr(deps.logistics, "intercept_parcel")
    assert not hasattr(deps.logistics, "expedite_delivery")
    assert not hasattr(deps.finance, "execute_refund")
    # 写能力仅在原始端口上存在
    assert hasattr(deps.tools.logistics, "intercept_parcel")
    assert hasattr(deps.tools.finance, "execute_refund")


def test_graph_nodes_do_not_reference_write_tools():
    src = (pathlib.Path(__file__).resolve().parents[1] / "app" / "graph" / "nodes.py").read_text(encoding="utf-8")
    assert "deps.tools" not in src


# --------------------------------------------------------------------------- #
# 3) 租户强制
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_gateway_rejects_missing_tenant():
    gw = ToolGateway(policies={}, on_call=None)
    facade = ReadOnlyOrderTools(_FailOrders(RuntimeError("x")), gw)
    with pytest.raises(ToolAuthorizationError):
        await facade.get_order_detail(tenant_id="", order_id="o_1")


# --------------------------------------------------------------------------- #
# 2) 重试 / 不重试 / 熔断 / 限流
# --------------------------------------------------------------------------- #
class _FlakyOrders:
    def __init__(self) -> None:
        self.n = 0

    async def get_order_detail(self, *, tenant_id: str, order_id: str) -> OrderSnapshot:
        self.n += 1
        if self.n < 3:
            raise TransientToolError("flaky")
        return OrderSnapshot(order_id=order_id, owner_user_id="u_demo", status="PAID_UNSHIPPED", version="v1")


@pytest.mark.asyncio
async def test_gateway_retries_transient_then_succeeds():
    raw = _FlakyOrders()
    policy = ToolPolicy(timeout_s=1.0, max_retries=3, backoff_base_s=0.0, backoff_max_s=0.0)
    gw = ToolGateway(policies={"oms.get_order_detail": policy}, on_call=None)
    facade = ReadOnlyOrderTools(raw, gw)
    out = await facade.get_order_detail(tenant_id="t1", order_id="o_1")
    assert out.order_id == "o_1"
    assert raw.n == 3  # 两次瞬时失败后成功


class _StableOrders:
    async def get_order_detail(self, *, tenant_id: str, order_id: str) -> OrderSnapshot:
        return OrderSnapshot(order_id=order_id, owner_user_id="u_demo", status="PAID_UNSHIPPED", version="v1")


class _BadShapeOrders:
    def __init__(self) -> None:
        self.n = 0

    async def get_order_detail(self, **kw):  # noqa: ANN003
        self.n += 1
        return {"wrong": "shape"}


@pytest.mark.asyncio
async def test_gateway_schema_error_not_retried():
    raw = _BadShapeOrders()
    policy = ToolPolicy(timeout_s=1.0, max_retries=5, backoff_base_s=0.0, backoff_max_s=0.0)
    gw = ToolGateway(policies={"oms.get_order_detail": policy}, on_call=None)
    facade = ReadOnlyOrderTools(raw, gw)
    with pytest.raises(ToolSchemaError):
        await facade.get_order_detail(tenant_id="t1", order_id="o_1")
    assert raw.n == 1  # 校验错误绝不重试


class _AlwaysFailOrders:
    def __init__(self) -> None:
        self.n = 0

    async def get_order_detail(self, **kw):  # noqa: ANN003
        self.n += 1
        raise ValueError("permanent")


@pytest.mark.asyncio
async def test_gateway_circuit_opens_after_threshold():
    raw = _AlwaysFailOrders()
    policy = ToolPolicy(timeout_s=1.0, max_retries=0, failure_threshold=2, cooldown_s=30.0)
    gw = ToolGateway(policies={"oms.get_order_detail": policy}, on_call=None)
    facade = ReadOnlyOrderTools(raw, gw)
    for _ in range(2):
        with pytest.raises(ToolUpstreamError):
            await facade.get_order_detail(tenant_id="t1", order_id="o_1")
    before = raw.n
    with pytest.raises(ToolCircuitOpen):
        await facade.get_order_detail(tenant_id="t1", order_id="o_1")
    assert raw.n == before  # 熔断后不再打到下游


@pytest.mark.asyncio
async def test_gateway_rate_limit_throttles():
    policy = ToolPolicy(timeout_s=1.0, max_retries=0, rate_limit_per_s=2.0)
    gw = ToolGateway(policies={"oms.get_order_detail": policy}, on_call=None)
    facade = ReadOnlyOrderTools(_StableOrders(), gw)
    await facade.get_order_detail(tenant_id="t1", order_id="o_1")
    await facade.get_order_detail(tenant_id="t1", order_id="o_1")
    started = time.monotonic()
    await facade.get_order_detail(tenant_id="t1", order_id="o_1")
    assert time.monotonic() - started >= 0.4  # 令牌耗尽后必须等待


# --------------------------------------------------------------------------- #
# 4/6/8) context_aggregator：审计、降级、facts_version
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_context_aggregator_records_tool_calls_and_facts_version():
    deps = _deps()
    out = await nodes.context_aggregator(_state("RETURN_REFUND"), deps)
    assert out.get("error_code") is None
    assert out["facts_version"] == "v1:v0:b0"  # order:logistics:finance
    assert {"oms.get_order_detail", "tms.get_tracking_info", "finance.get_refundable_balance",
            "oms.get_buyer_refund_stats"} == {c["tool"] for c in deps.persistence.tool_calls}
    assert out["context_meta"]["degraded"] == {}


@pytest.mark.asyncio
async def test_context_aggregator_degrades_noncritical_logistics():
    deps = _deps()
    _rebuild_read(deps, logistics=_FailLogistics(ToolUpstreamError("tms down")))
    out = await nodes.context_aggregator(_state("RETURN_REFUND"), deps)
    assert out.get("error_code") is None  # 非关键事实失败不 fail-closed
    assert out["logistics_snapshot"]["degraded"] is True
    assert out["context_meta"]["degraded"] == {"logistics": "READ_FAILED"}
    assert out["facts_version"] == "v1:unknown:b0"


@pytest.mark.asyncio
async def test_context_aggregator_failclosed_when_finance_critical():
    deps = _deps()
    _rebuild_read(deps, finance=_FailFinance(ToolUpstreamError("finance down")))
    out = await nodes.context_aggregator(_state("REFUND_ONLY"), deps)
    assert out["error_code"] == "READ_FAILED"  # 退款族：资金事实关键 -> fail-closed


@pytest.mark.asyncio
async def test_context_aggregator_order_failure_is_failclosed():
    deps = _deps()
    from app.mcp.gateway import ToolNotFound
    _rebuild_read(deps, orders=_FailOrders(ToolNotFound("ORDER_NOT_FOUND")))
    out = await nodes.context_aggregator(_state("RETURN_REFUND"), deps)
    assert out["error_code"] == "ORDER_NOT_FOUND"
    assert nodes.route_after_context(out) == "manual"


@pytest.mark.asyncio
async def test_context_aggregator_owner_mismatch_denied():
    deps = _deps()
    state = _state("RETURN_REFUND")
    state["user_id"] = "u_someone_else"
    out = await nodes.context_aggregator(state, deps)
    assert out["error_code"] == "NOT_OWNER"
    assert nodes.route_after_context(out) == "deny"


# --------------------------------------------------------------------------- #
# 3') mock 服务端强制租户/404
# --------------------------------------------------------------------------- #
def test_mock_server_enforces_tenant_and_404():
    from fastapi.testclient import TestClient
    from app.mcp.mocks.server import app as mock_app
    c = TestClient(mock_app)
    # 未知租户 -> 403
    r = c.post("/oms/get_order_detail", json={"tenant_id": "evil", "order_id": "o_1001"})
    assert r.status_code == 403 and "TENANT_FORBIDDEN" in str(r.json().get("detail"))
    # 缺失租户 -> 403
    r = c.post("/oms/get_order_detail", json={"order_id": "o_1001"})
    assert r.status_code == 403
    # 订单不存在 -> 404
    r = c.post("/oms/get_order_detail", json={"tenant_id": "tenant_demo", "order_id": "o_nope"})
    assert r.status_code == 404 and r.json().get("detail") == "ORDER_NOT_FOUND"
    # 归属不符 -> 403
    r = c.post("/oms/get_order_detail", json={"tenant_id": "tenant_demo", "order_id": "o_1001",
                                              "authenticated_user_id": "u_hacker"})
    assert r.status_code == 403 and r.json().get("detail") == "NOT_OWNER"

def test_order_validator_preserves_delivered_at():
    """OMS 返回的 delivered_at 必须穿过网关校验（政策时效依赖它，丢失会导致全量转人工）。"""
    from app.mcp.gateway import validate_order
    snap = validate_order({
        "order_id": "o1", "owner_user_id": "u_demo", "status": "DELIVERED", "version": "v1",
        "category": "APPAREL", "currency": "CNY", "lines": [],
        "delivered_at": "2026-10-20T10:00:00+08:00",
    })
    assert snap.delivered_at == "2026-10-20T10:00:00+08:00"
