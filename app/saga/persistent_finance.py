from __future__ import annotations

import uuid
from typing import Any

import json

from sqlalchemy import text


class PostgreSQLMockFinance:
    """验收专用持久化 Finance：幂等结果和资金副作用均落 PostgreSQL。"""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    async def prepare(self, order_id: str, refundable_minor: int) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                await session.execute(text(
                    "INSERT INTO saga_safety_finance (order_id, refundable_minor) "
                    "VALUES (:order_id, :amount) "
                    "ON CONFLICT (order_id) DO UPDATE SET "
                    "refundable_minor=:amount, refund_count=0"),
                    {"order_id": order_id, "amount": refundable_minor})

    async def state(self, order_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            row = (await session.execute(text(
                "SELECT refundable_minor, refund_count FROM saga_safety_finance WHERE order_id=:order_id"
            ), {"order_id": order_id})).mappings().first()
        return dict(row) if row else {}

    async def execute_refund(self, *, tenant_id: str, refund_request_id: str, quote_id: str,
                             quote_hash: str, authorization_ref: str, idempotency_key: str,
                             payload_hash: str, order_id: str | None = None) -> dict:
        async with self.session_factory() as session:
            async with session.begin():
                existing = (await session.execute(text(
                    "SELECT result_json FROM saga_safety_finance_idempotency "
                    "WHERE idempotency_key=:key AND payload_hash=:payload_hash"),
                    {"key": idempotency_key, "payload_hash": payload_hash})).scalar()
                if existing:
                    return existing
                conflict = (await session.execute(text(
                    "SELECT 1 FROM saga_safety_finance_idempotency "
                    "WHERE idempotency_key=:key AND payload_hash<>:payload_hash"),
                    {"key": idempotency_key, "payload_hash": payload_hash})).first()
                if conflict:
                    raise ValueError("IDEMPOTENCY_CONFLICT")
                result = {"status": "SUCCEEDED", "refund_request_id": refund_request_id,
                          "provider_txn_id": str(uuid.uuid4()), "payload_hash": payload_hash}
                result_json = json.dumps(result, separators=(",", ":"), ensure_ascii=False)
                await session.execute(text(
                    "INSERT INTO saga_safety_finance_idempotency "
                    "(idempotency_key, payload_hash, result_json) "
                    "VALUES (:key, :payload_hash, CAST(:result_json AS jsonb))"),
                    {"key": idempotency_key, "payload_hash": payload_hash, "result_json": result_json})
                await session.execute(text(
                    "UPDATE saga_safety_finance SET refundable_minor=refundable_minor-1000, "
                    "refund_count=refund_count+1 WHERE order_id=:order_id"),
                    {"order_id": order_id or ""})
                return result

    async def get_refund_status(self, *, tenant_id: str, refund_request_id: str) -> dict:
        async with self.session_factory() as session:
            row = (await session.execute(text(
                "SELECT result_json FROM saga_safety_finance_idempotency "
                "WHERE result_json->>'refund_request_id'=:refund_request_id ORDER BY created_at LIMIT 1"
            ), {"refund_request_id": refund_request_id})).scalar()
        return row or {"status": "UNKNOWN", "refund_request_id": refund_request_id}

    async def get_reconciliation_result(self, *, tenant_id: str, refund_request_id: str) -> dict:
        return await self.get_refund_status(tenant_id=tenant_id, refund_request_id=refund_request_id)
