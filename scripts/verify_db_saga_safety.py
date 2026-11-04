# -*- coding: utf-8 -*-
"""真实 PostgreSQL Saga 安全专项：多 Worker 竞争与崩溃窗口。

前置：docker compose up -d postgres；scripts/migrate.py 已执行。
用法：python scripts/verify_db_saga_safety.py

说明：资金端使用本地 InMemoryFinance 代替容器内 HTTP Mock，因为专项要精确注入
“Finance 成功后、DB 状态回写前”的崩溃窗口。幂等行为由 Finance 幂等键保证，
不受验证脚本所在进程重启影响。
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import uuid

from sqlalchemy import text

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.db.engine import make_engine, make_sessionmaker, system_transaction  # noqa: E402
from app.db.persistence import SqlPersistence  # noqa: E402
from app.mcp.mocks.inmemory import InMemoryFinance  # noqa: E402
from app.saga.dispatcher import SagaDispatcher  # noqa: E402

WORKER_DSN = os.environ.get(
    "POLICY_WORKER_DSN",
    "postgresql+asyncpg://aftersale_worker:worker_pw_change_me@localhost:5432/aftersale")
TENANT = "tenant_demo"


class _NoWriteOrders:
    async def create_reshipment(self, **kwargs):
        raise AssertionError("本专项不得执行补发")


class _NoWriteLogistics:
    async def intercept_parcel(self, **kwargs):
        raise AssertionError("本专项不得执行拦截")

    async def expedite_delivery(self, **kwargs):
        raise AssertionError("本专项不得执行加速")


def _dispatcher(finance: InMemoryFinance) -> SagaDispatcher:
    return SagaDispatcher(orders=_NoWriteOrders(), logistics=_NoWriteLogistics(), finance=finance)  # type: ignore[arg-type]


async def _enqueue(persistence: SqlPersistence, ticket_id: str, idempotency_key: str) -> str:
    quote_id = str(uuid.uuid4())
    return await persistence.enqueue_command(
        tenant_id=TENANT, ticket_id=ticket_id, run_id=str(uuid.uuid4()),
        action="REQUEST_REFUND_ONLY", order_id="o_1001", refund_request_id=str(uuid.uuid4()),
        quote_id=quote_id, quote_hash=f"hash-{idempotency_key}",
        plan_id=None, authorization_ref=f"approval:{idempotency_key}",
        idempotency_key=idempotency_key, payload_hash=f"payload-{idempotency_key}",
        payload={"test": idempotency_key}, priority=10)


async def _claim(persistence: SqlPersistence) -> dict | None:
    return await persistence.claim_next_command()


async def _clear_safety_commands(persistence: SqlPersistence) -> None:
    async with system_transaction(persistence.sf) as session:
        await session.execute(
            text("DELETE FROM saga_commands WHERE idempotency_key LIKE 'safety:%'"),
        )


async def _command_status(persistence: SqlPersistence, command_id: str) -> str:
    rows = await persistence.list_command_statuses(tenant_id=TENANT, command_ids=[command_id])
    return str(rows[0]["status"]) if rows else "MISSING"


async def _test_concurrent_workers(persistence: SqlPersistence) -> None:
    idempotency_key = f"safety:concurrent:{uuid.uuid4()}"
    command_id = await _enqueue(persistence, str(uuid.uuid4()), idempotency_key)
    first, second = await asyncio.gather(_claim(persistence), _claim(persistence))
    claims = [claim for claim in (first, second) if claim]
    assert claims and claims[0]["command_id"] == command_id, "必须成功领取唯一命令"
    assert len(claims) == 1, "两个 Worker 并发领取时只能产生一个有效租约"
    assert second is None or second["command_id"] != command_id, "第二个 Worker 不得领取同一命令"

    finance = InMemoryFinance()
    finance.seed("o_1001", 20000)
    result = await _dispatcher(finance).dispatch(claims[0])
    status = await persistence.mark_command_result(command_id=command_id, tenant_id=TENANT, result=result)
    assert status == "CONFIRMED"
    assert finance.refunds[claims[0]["refund_request_id"]]["status"] == "SUCCEEDED"
    assert len(finance._idem) == 1
    print("CONCURRENT_WORKER_OK", {"command_id": command_id, "claims": len(claims), "status": status})


async def _test_crash_after_finance_success(persistence: SqlPersistence) -> None:
    idempotency_key = f"safety:crash:{uuid.uuid4()}"
    command_id = await _enqueue(persistence, str(uuid.uuid4()), idempotency_key)
    command = await _claim(persistence)
    assert command and command["command_id"] == command_id

    finance = InMemoryFinance()
    finance.seed("o_1001", 20000)
    first = await _dispatcher(finance).dispatch(command)
    assert first["status"] == "SUCCEEDED"

    await persistence.mark_command_result(command_id=command_id, tenant_id=TENANT,
                                           result={"status": "UNKNOWN", "error": "CRASH_AFTER_FINANCE_SUCCESS"})
    assert await _command_status(persistence, command_id) == "UNKNOWN"

    replay = await _dispatcher(finance).dispatch(command)
    assert replay == first
    authority = await finance.get_refund_status(tenant_id=TENANT,
                                                refund_request_id=command["refund_request_id"])
    assert authority["status"] == "SUCCEEDED"
    assert len(finance._idem) == 1
    assert finance.balances["o_1001"]["refundable"] == 19000
    print("CRASH_AFTER_FINANCE_OK", {
        "command_id": command_id, "idempotent_results": 1,
        "provider_txn_id": first["provider_txn_id"]})


async def _test_response_lost(persistence: SqlPersistence) -> None:
    idempotency_key = f"safety:timeout:{uuid.uuid4()}"
    command_id = await _enqueue(persistence, str(uuid.uuid4()), idempotency_key)
    command = await _claim(persistence)
    assert command and command["command_id"] == command_id

    class ResponseLostFinance(InMemoryFinance):
        calls = 0

        async def execute_refund(self, **kwargs):
            ResponseLostFinance.calls += 1
            result = await super().execute_refund(**kwargs)
            raise TimeoutError("response lost after finance success")

    finance = ResponseLostFinance()
    finance.seed("o_1001", 20000)
    dispatcher = _dispatcher(finance)
    try:
        await dispatcher.dispatch(command)
    except TimeoutError:
        pass
    await persistence.mark_command_result(command_id=command_id, tenant_id=TENANT,
                                           result={"status": "UNKNOWN", "error": "TIMEOUT"})
    assert await _command_status(persistence, command_id) == "UNKNOWN"

    replay = await _dispatcher(finance).dispatch(command)
    assert replay["status"] == "SUCCEEDED"
    authority = await finance.get_refund_status(tenant_id=TENANT,
                                                refund_request_id=command["refund_request_id"])
    assert authority["status"] == "SUCCEEDED"
    assert ResponseLostFinance.calls == 2
    assert len(finance._idem) == 1
    assert finance.balances["o_1001"]["refundable"] == 19000
    print("RESPONSE_LOST_OK", {
        "command_id": command_id, "finance_calls": ResponseLostFinance.calls,
        "unique_downstream_effects": 1})


async def _test_duplicate_enqueue(persistence: SqlPersistence) -> None:
    idempotency_key = f"safety:duplicate:{uuid.uuid4()}"
    ticket_id = str(uuid.uuid4())
    first = await _enqueue(persistence, ticket_id, idempotency_key)
    second = await _enqueue(persistence, ticket_id, idempotency_key)
    async with system_transaction(persistence.sf) as session:
        count = await session.scalar(
            text("SELECT count(*) FROM saga_commands WHERE tenant_id=:tenant AND idempotency_key=:key"),
            {"tenant": TENANT, "key": idempotency_key})
    assert count == 1, "相同幂等键并发/重复提交必须只有一条数据库命令"
    print("DUPLICATE_ENQUEUE_OK", {"command_ids": [first, second], "database_rows": count})


async def _test_workflow_version_persistence(persistence: SqlPersistence) -> None:
    for workflow_version in ("v3", "v4"):
        ticket = await persistence.create_ticket(
            tenant_id=TENANT, user_id="u_demo", order_id="o_1001", intent="UNKNOWN",
            workflow_version=workflow_version)
        loaded = await persistence.get_ticket(tenant_id=TENANT, ticket_id=ticket["ticket_id"])
        assert loaded and loaded["workflow_version"] == workflow_version, (
            f"workflow_version 应持久化为 {workflow_version}，实际 {loaded}")
    print("WORKFLOW_VERSION_PERSIST_OK")


async def main() -> None:
    sf = make_sessionmaker(make_engine_from_url(WORKER_DSN))
    persistence = SqlPersistence(sf)
    await _clear_safety_commands(persistence)
    await _test_workflow_version_persistence(persistence)
    await _test_duplicate_enqueue(persistence)
    await _test_concurrent_workers(persistence)
    await _test_crash_after_finance_success(persistence)
    await _test_response_lost(persistence)
    print("DB_SAGA_SAFETY_OK")


def make_engine_from_url(url: str):
    from app.config import Settings
    return make_engine(Settings(database_url=url))


if __name__ == "__main__":
    asyncio.run(main())
