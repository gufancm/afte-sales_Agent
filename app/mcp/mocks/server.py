"""OMS/TMS/Finance Mock 服务（仅联调）。暴露与 app.mcp.client 对应的 REST 路径。

安全基线（对齐真实系统应具备的网关职责）：
  - 每个调用必须携带合法 tenant_id，未知租户 -> 403 TENANT_FORBIDDEN；
  - 订单不存在 -> 404 ORDER_NOT_FOUND（而不是 500）；
  - 若调用方提供 authenticated_user_id，则做归属校验，非本人订单 -> 403 NOT_OWNER。

注意：真实系统不应把 Mock 暴露到生产；真实系统必须在被调方（网关/DB）强制上述边界。
"""
from __future__ import annotations

import json
import os

from fastapi import FastAPI, HTTPException

from app.mcp.mocks.inmemory import InMemoryFinance, InMemoryOMS, InMemoryTMS
from app.mcp.ports import LogisticsSnapshot, OrderSnapshot

app = FastAPI(title="aftersale-mcp-mock")

# 允许访问的租户集合（可由环境变量覆盖；默认仅联调租户）
KNOWN_TENANTS = {t.strip() for t in os.getenv("MCP_MOCK_TENANTS", "tenant_demo").split(",") if t.strip()}
REQUIRE_TENANT = os.getenv("MCP_MOCK_REQUIRE_TENANT", "true").lower() not in ("0", "false", "no")

oms = InMemoryOMS()
tms = InMemoryTMS()
fin = InMemoryFinance()

# 演示数据：覆盖生鲜/服饰/数码/图书 等品类，便于验证各品类政策
oms.seed(OrderSnapshot(order_id="o_1001", owner_user_id="u_demo", status="PAID_UNSHIPPED",
                       version="v1", category="FRESH", currency="CNY",
                       lines=[{"order_line_id": "l1", "sku_id": "sku_1", "quantity": 1}]))
oms.seed(OrderSnapshot(order_id="o_1002", owner_user_id="u_demo", status="DELIVERED",
                       version="v1", category="APPAREL", currency="CNY",
                       lines=[{"order_line_id": "l1", "sku_id": "sku_a1", "quantity": 1}]))
oms.seed(OrderSnapshot(order_id="o_1003", owner_user_id="u_demo", status="DELIVERED",
                       version="v1", category="DIGITAL", currency="CNY",
                       lines=[{"order_line_id": "l1", "sku_id": "sku_d1", "quantity": 1}]))
oms.seed(OrderSnapshot(order_id="o_1004", owner_user_id="u_demo", status="DELIVERED",
                       version="v1", category="BOOK_MEDIA", currency="CNY",
                       lines=[{"order_line_id": "l1", "sku_id": "sku_m1", "quantity": 1}]))
oms.seed(OrderSnapshot(order_id="o_1005", owner_user_id="u_demo", status="PAID_UNSHIPPED",
                       version="v1", category="PRESALE", currency="CNY",
                       lines=[{"order_line_id": "l1", "sku_id": "sku_p1", "quantity": 1}]))
oms.seed(OrderSnapshot(order_id="o_1006", owner_user_id="u_demo", status="PAID_UNSHIPPED",
                       version="v1", category="APPAREL", currency="CNY",
                       lines=[{"order_line_id": "l1", "sku_id": "sku_a2", "quantity": 1}]))
tms.seed(LogisticsSnapshot(waybill_no="wb_1", parcel_status="IN_TRANSIT", version="t1"))
tms.seed(LogisticsSnapshot(waybill_no="wb_2", parcel_status="DELIVERED", version="t2"))
tms.seed(LogisticsSnapshot(waybill_no="wb_3", parcel_status="DELIVERED", version="t3"))
fin.seed("o_1001", 20000)
fin.seed("o_1002", 50000)
fin.seed("o_1003", 300000)
fin.seed("o_1004", 8800)
fin.seed("o_1005", 10000)
fin.seed("o_1006", 19900)

# 可选：加载 Task Success / 其他联调 fixture（容器内路径或挂载路径）。
# 文件格式与 tests/eval/data/task_success_fixtures.json 一致；仅在显式配置时启用。
_SEED_FILE = os.getenv("MCP_MOCK_SEED_FILE", "").strip()
if _SEED_FILE and os.path.exists(_SEED_FILE):
    _seed_data = json.loads(open(_SEED_FILE, encoding="utf-8").read())
    for _row in _seed_data.get("orders", []):
        oms.seed(OrderSnapshot(**_row))
    for _row in _seed_data.get("logistics", []):
        tms.seed(LogisticsSnapshot(**_row))
    for _row in _seed_data.get("finance", []):
        fin.seed(str(_row["order_id"]), int(_row["available_minor"]))


def _tenant(body: dict) -> str:
    tenant_id = body.get("tenant_id")
    if not tenant_id:
        raise HTTPException(status_code=403, detail="TENANT_FORBIDDEN:missing")
    if REQUIRE_TENANT and tenant_id not in KNOWN_TENANTS:
        raise HTTPException(status_code=403, detail="TENANT_FORBIDDEN:unknown")
    return tenant_id


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "mcp-mock"}


@app.post("/oms/get_order_detail")
async def oms_get_order_detail(body: dict) -> dict:
    tenant_id = _tenant(body)
    try:
        o = await oms.get_order_detail(tenant_id=tenant_id, order_id=body["order_id"])
    except KeyError:
        raise HTTPException(status_code=404, detail="ORDER_NOT_FOUND")
    auth_user = body.get("authenticated_user_id")
    if auth_user and auth_user != o.owner_user_id:
        raise HTTPException(status_code=403, detail="NOT_OWNER")
    return o.__dict__


@app.post("/oms/get_reshipment_status")
async def oms_reshipment_status(body: dict) -> dict:
    s = await oms.get_reshipment_status(tenant_id=_tenant(body), order_id=body["order_id"])
    return s.__dict__


@app.post("/oms/get_buyer_refund_stats")
async def oms_refund_stats(body: dict) -> dict:
    try:
        s = await oms.get_buyer_refund_stats(tenant_id=_tenant(body), order_id=body["order_id"])
    except KeyError:
        raise HTTPException(status_code=404, detail="ORDER_NOT_FOUND")
    return s.__dict__


@app.post("/oms/hold_fulfillment")
async def oms_hold(body: dict) -> dict:
    return await oms.hold_fulfillment(tenant_id=_tenant(body), order_id=body["order_id"],
                                      idempotency_key=body["idempotency_key"])


@app.post("/oms/create_reshipment")
async def oms_reship(body: dict) -> dict:
    return await oms.create_reshipment(tenant_id=_tenant(body), order_id=body["order_id"],
                                       sku_ids=body.get("sku_ids", []), idempotency_key=body["idempotency_key"])


@app.post("/tms/get_tracking_info")
async def tms_tracking(body: dict) -> dict:
    t = await tms.get_tracking_info(tenant_id=_tenant(body), order_id=body["order_id"],
                                    waybill_no=body.get("waybill_no"))
    return t.__dict__


@app.post("/tms/get_dispatch_status")
async def tms_dispatch_status(body: dict) -> dict:
    s = await tms.get_dispatch_status(tenant_id=_tenant(body), waybill_no=body["waybill_no"])
    return s.__dict__


@app.post("/tms/intercept_parcel")
async def tms_intercept(body: dict) -> dict:
    return await tms.intercept_parcel(tenant_id=_tenant(body), waybill_no=body["waybill_no"],
                                      idempotency_key=body["idempotency_key"])


@app.post("/tms/get_interception_status")
async def tms_intercept_status(body: dict) -> dict:
    return await tms.get_interception_status(tenant_id=_tenant(body), waybill_no=body["waybill_no"])


@app.post("/tms/expedite_delivery")
async def tms_expedite(body: dict) -> dict:
    return await tms.expedite_delivery(tenant_id=_tenant(body), waybill_no=body["waybill_no"],
                                       idempotency_key=body["idempotency_key"])


@app.post("/finance/get_refundable_balance")
async def fin_balance(body: dict) -> dict:
    b = await fin.get_refundable_balance(tenant_id=_tenant(body), order_id=body["order_id"])
    return b.__dict__


@app.post("/finance/calculate_refund_amount")
async def fin_quote(body: dict) -> dict:
    return await fin.calculate_refund_amount(tenant_id=_tenant(body), refund_request_id=body["refund_request_id"],
                                             order_id=body["order_id"], items=body.get("items", []),
                                             policy_decision_id=body["policy_decision_id"], order_version=body["order_version"])


@app.post("/finance/execute_refund")
async def fin_execute(body: dict) -> dict:
    return await fin.execute_refund(tenant_id=_tenant(body), refund_request_id=body["refund_request_id"],
                                    quote_id=body["quote_id"], quote_hash=body["quote_hash"],
                                    authorization_ref=body["authorization_ref"], idempotency_key=body["idempotency_key"],
                                    payload_hash=body["payload_hash"])


@app.post("/finance/get_refund_status")
async def fin_status(body: dict) -> dict:
    return await fin.get_refund_status(tenant_id=_tenant(body), refund_request_id=body["refund_request_id"])


@app.post("/finance/get_reconciliation_result")
async def fin_recon(body: dict) -> dict:
    return await fin.get_reconciliation_result(tenant_id=_tenant(body), refund_request_id=body["refund_request_id"])
