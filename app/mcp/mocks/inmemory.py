"""外部系统内存 Mock（OMS/TMS/Finance），实现 ports 协议，用于本地联调与测试。"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.mcp.ports import (DispatchStatus, FinanceSnapshot, FulfillmentStatus, LogisticsSnapshot,
                           OrderSnapshot, RefundStats)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryOMS:
    def __init__(self) -> None:
        self.orders: dict[str, OrderSnapshot] = {}
        self.refund_stats: dict[str, dict] = {}
        self.reshipments: dict[str, str] = {}
        self._idem: dict[str, dict] = {}

    def seed(self, order: OrderSnapshot) -> None:
        self.orders[order.order_id] = order

    def seed_refund_stats(self, order_id: str, *, refund_count_30d: int = 0,
                          refund_amount_30d_minor: int = 0, blacklisted: bool = False) -> None:
        self.refund_stats[order_id] = {"refund_count_30d": refund_count_30d,
                                       "refund_amount_30d_minor": refund_amount_30d_minor,
                                       "blacklisted": blacklisted}

    async def get_order_detail(self, *, tenant_id: str, order_id: str) -> OrderSnapshot:
        if order_id not in self.orders:
            raise KeyError(f"ORDER_NOT_FOUND:{order_id}")
        return self.orders[order_id]

    async def get_buyer_refund_stats(self, *, tenant_id: str, order_id: str) -> RefundStats:
        if order_id not in self.orders:
            raise KeyError(f"ORDER_NOT_FOUND:{order_id}")
        s = self.refund_stats.get(order_id, {})
        return RefundStats(order_id=order_id, refund_count_30d=int(s.get("refund_count_30d", 0)),
                           refund_amount_30d_minor=int(s.get("refund_amount_30d_minor", 0)),
                           blacklisted=bool(s.get("blacklisted", False)),
                           as_of=_now().isoformat().replace("+00:00", "Z"), version="rs0")

    def seed_reshipment_status(self, order_id: str, status: str) -> None:
        self.reshipments[order_id] = status

    async def get_reshipment_status(self, *, tenant_id: str, order_id: str) -> FulfillmentStatus:
        return FulfillmentStatus(order_id=order_id,
                                 reshipment_status=self.reshipments.get(order_id, "UNKNOWN"), version="f0")

    async def hold_fulfillment(self, *, tenant_id: str, order_id: str, idempotency_key: str) -> dict:
        if idempotency_key in self._idem:
            return self._idem[idempotency_key]
        res = {"status": "HELD", "order_id": order_id}
        self._idem[idempotency_key] = res
        return res

    async def create_reshipment(self, *, tenant_id: str, order_id: str, sku_ids: list[str], idempotency_key: str) -> dict:
        if idempotency_key in self._idem:
            return self._idem[idempotency_key]
        res = {"status": "SUBMITTED", "reshipment_id": str(uuid.uuid4()), "order_id": order_id, "sku_ids": sku_ids}
        self._idem[idempotency_key] = res
        self.reshipments[order_id] = "CREATED"
        return res


class InMemoryTMS:
    def __init__(self) -> None:
        self.tracking: dict[str, LogisticsSnapshot] = {}
        self.intercepts: dict[str, dict] = {}
        self.dispatches: dict[str, str] = {}
        self._idem: dict[str, dict] = {}

    def seed(self, snap: LogisticsSnapshot) -> None:
        self.tracking[snap.waybill_no or ""] = snap

    async def get_tracking_info(self, *, tenant_id: str, order_id: str, waybill_no: str | None) -> LogisticsSnapshot:
        if waybill_no and waybill_no in self.tracking:
            return self.tracking[waybill_no]
        return LogisticsSnapshot(waybill_no=waybill_no, parcel_status="UNKNOWN", version="v0")

    async def intercept_parcel(self, *, tenant_id: str, waybill_no: str, idempotency_key: str) -> dict:
        if idempotency_key in self._idem:
            return self._idem[idempotency_key]
        res = {"status": "ACCEPTED", "intercept_command_status": "ACCEPTED", "waybill_no": waybill_no}
        self._idem[idempotency_key] = res
        self.intercepts[waybill_no] = res
        return res

    async def get_interception_status(self, *, tenant_id: str, waybill_no: str) -> dict:
        return self.intercepts.get(waybill_no, {"intercept_command_status": "UNKNOWN", "waybill_no": waybill_no})

    async def expedite_delivery(self, *, tenant_id: str, waybill_no: str, idempotency_key: str) -> dict:
        if idempotency_key in self._idem:
            return self._idem[idempotency_key]
        res = {"status": "REQUESTED", "waybill_no": waybill_no}
        self._idem[idempotency_key] = res
        self.dispatches[waybill_no] = "IN_PROGRESS"
        return res

    def seed_dispatch_status(self, waybill_no: str, status: str) -> None:
        self.dispatches[waybill_no] = status

    async def get_dispatch_status(self, *, tenant_id: str, waybill_no: str) -> DispatchStatus:
        return DispatchStatus(waybill_no=waybill_no,
                              dispatch_status=self.dispatches.get(waybill_no, "UNKNOWN"), version="d0")


class InMemoryFinance:
    """Finance 唯一余额权威：预占、释放、退款与幂等都在此模型内完成（§9.10）。"""

    HOLD_TTL = timedelta(minutes=30)

    def __init__(self) -> None:
        self.balances: dict[str, dict] = {}
        self.refunds: dict[str, dict] = {}
        self._idem: dict[str, dict] = {}
        # 预占（hold）：reservation_reference -> 预占记录；每笔退款申请最多 1 个活跃预占
        self._reservations: dict[str, dict] = {}
        self._active_hold: dict[str, str] = {}

    def seed(self, order_id: str, refundable_minor: int, currency: str = "CNY") -> None:
        self.balances[order_id] = {"refundable": refundable_minor, "reserved": 0, "currency": currency, "version": "b0"}

    def _purge_expired(self, now: datetime) -> None:
        """惰性回收过期预占（等价于真实系统里的 hold 到期释放）。"""
        for ref, r in list(self._reservations.items()):
            if r["status"] == "ACTIVE" and r["_expires_dt"] <= now:
                b = self.balances.get(r["order_id"])
                if b:
                    b["reserved"] = max(0, int(b["reserved"]) - r["amount_minor"])
                r["status"] = "EXPIRED"
                if self._active_hold.get(r["refund_request_id"]) == ref:
                    self._active_hold.pop(r["refund_request_id"], None)

    def _release(self, ref: str) -> None:
        r = self._reservations.get(ref)
        if not r:
            return
        if r["status"] == "ACTIVE":
            b = self.balances.get(r["order_id"])
            if b:
                b["reserved"] = max(0, int(b["reserved"]) - r["amount_minor"])
            r["status"] = "RELEASED"
        if self._active_hold.get(r["refund_request_id"]) == ref:
            self._active_hold.pop(r["refund_request_id"], None)

    async def get_refundable_balance(self, *, tenant_id: str, order_id: str) -> FinanceSnapshot:
        self._purge_expired(_now())
        b = self.balances.get(order_id, {"refundable": 0, "reserved": 0, "currency": "CNY", "version": "b0"})
        return FinanceSnapshot(order_id=order_id, refundable_balance_minor=b["refundable"],
                               reserved_amount_minor=b["reserved"], currency=b["currency"],
                               as_of=_now().isoformat().replace("+00:00", "Z"), version=b["version"])

    async def calculate_refund_amount(self, *, tenant_id: str, refund_request_id: str, order_id: str,
                                      items: list[dict], policy_decision_id: str, order_version: str) -> dict:
        """报价 + 预占（原子）：金额由 Finance 计算；成功后冻结一笔可用余额 hold。

        可用额度 = 可退余额 - 已预占。额度不足时**不预占**（`reservation_reference=None`），
        由调用方 fail-closed（不得先报价后放行）。同一申请重报价会先释放旧预占。
        """
        now = _now()
        self._purge_expired(now)
        prev = self._active_hold.pop(refund_request_id, None)
        if prev:
            self._release(prev)

        amount = sum(int(i.get("quantity") or 1) for i in items) * 1000 or 1000
        b = self.balances.setdefault(order_id, {"refundable": 0, "reserved": 0, "currency": "CNY", "version": "b0"})
        available = max(0, int(b["refundable"]) - int(b["reserved"]))
        expires_dt = now + self.HOLD_TTL
        expires = expires_dt.isoformat().replace("+00:00", "Z")
        quote = {
            "refund_request_id": refund_request_id, "quote_id": str(uuid.uuid4()),
            "quote_hash": "sha256:" + uuid.uuid4().hex, "amount_minor": amount, "currency": b["currency"],
            "order_version": order_version, "policy_decision_id": policy_decision_id, "expires_at": expires,
            "breakdown": {"items": items, "order_line_amount_minor": amount, "reserved_amount_minor": 0},
            "reservation_reference": None, "reservation_expires_at": None, "available_balance_minor": available,
        }
        if amount > available:
            quote["breakdown"] = {"items": items, "reason": "INSUFFICIENT_AVAILABLE_BALANCE"}
            return quote

        ref = str(uuid.uuid4())
        self._reservations[ref] = {
            "reservation_reference": ref, "refund_request_id": refund_request_id, "order_id": order_id,
            "amount_minor": amount, "quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"],
            "policy_decision_id": policy_decision_id, "order_version": order_version,
            "expires_at": expires, "_expires_dt": expires_dt, "status": "ACTIVE",
        }
        self._active_hold[refund_request_id] = ref
        b["reserved"] = int(b["reserved"]) + amount
        quote["reservation_reference"] = ref
        quote["reservation_expires_at"] = expires
        quote["available_balance_minor"] = available - amount
        quote["breakdown"] = {"items": items, "order_line_amount_minor": amount, "reserved_amount_minor": amount}
        return quote

    async def execute_refund(self, *, tenant_id: str, refund_request_id: str, quote_id: str, quote_hash: str,
                             authorization_ref: str, idempotency_key: str, payload_hash: str,
                             order_id: str | None = None) -> dict:
        if idempotency_key in self._idem:
            existing = self._idem[idempotency_key]
            if existing.get("payload_hash") != payload_hash:
                raise ValueError("IDEMPOTENCY_CONFLICT")
            return existing
        now = _now()
        self._purge_expired(now)
        ref = self._active_hold.get(refund_request_id)
        r = self._reservations.get(ref) if ref else None
        if r and r["status"] == "ACTIVE" and r["quote_hash"] != quote_hash:
            raise ValueError("QUOTE_HASH_MISMATCH")
        consumed = None
        if r and r["status"] == "ACTIVE":
            b = self.balances.get(r["order_id"])
            if b:
                b["refundable"] = max(0, int(b["refundable"]) - r["amount_minor"])
                b["reserved"] = max(0, int(b["reserved"]) - r["amount_minor"])
            r["status"] = "CONSUMED"
            self._active_hold.pop(refund_request_id, None)
            consumed = r["reservation_reference"]
        res = {"status": "SUCCEEDED", "refund_request_id": refund_request_id,
               "provider_txn_id": str(uuid.uuid4()), "payload_hash": payload_hash,
               "reservation_reference": consumed, "amount_minor": (r or {}).get("amount_minor")}
        self._idem[idempotency_key] = res
        self.refunds[refund_request_id] = res
        return res

    async def get_refund_status(self, *, tenant_id: str, refund_request_id: str) -> dict:
        return self.refunds.get(refund_request_id, {"status": "UNKNOWN", "refund_request_id": refund_request_id})

    async def get_reconciliation_result(self, *, tenant_id: str, refund_request_id: str) -> dict:
        return self.refunds.get(refund_request_id, {"status": "UNKNOWN"})
