"""真实 PostgreSQL Saga 资金安全专项验收。

验收点：
- 两个独立 Python 进程并发竞争同一条命令，只能有一个有效租约；
- Finance 成功后、命令状态回写前进程崩溃，重启后同幂等键只扣一次款；
- Finance 成功但响应丢失，命令必须 UNKNOWN 并进入对账，不得换幂等键重试；
- Mock Finance 幂等记录持久化在 PostgreSQL，跨进程/崩溃后仍可用；
- V3/V4 workflow_version 读写回归。

前置：postgres 已启动且迁移已执行；建议停止常驻 worker。
用法：python scripts/verify_db_saga_safety.py
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import uuid

from sqlalchemy import text

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.db.engine import make_engine, make_sessionmaker, system_transaction  # noqa: E402
from app.db.persistence import SqlPersistence  # noqa: E402
from app.saga.dispatcher import SagaDispatcher  # noqa: E402
from app.saga.persistent_finance import PostgreSQLMockFinance  # noqa: E402

WORKER_DSN = os.environ.get(
    "POLICY_WORKER_DSN",
    "postgresql+asyncpg://aftersale_worker:worker_pw_change_me@localhost:5432/aftersale")
TENANT = "tenant_demo"
ORDER_ID = "o_safety"
PYTHON = sys.executable


class _NoWriteOrders:
    async def create_reshipment(self, **kwargs):
        raise AssertionError("本专项不得执行补发")


class _NoWriteLogistics:
    async def intercept_parcel(self, **kwargs):
        raise AssertionError("本专项不得执行拦截")

    async def expedite_delivery(self, **kwargs):
        raise AssertionError("本专项不得执行加速")


def _dispatcher(finance: PostgreSQLMockFinance) -> SagaDispatcher:
    return SagaDispatcher(orders=_NoWriteOrders(), logistics=_NoWriteLogistics(), finance=finance)  # type: ignore[arg-type]


def _make_persistence() -> SqlPersistence:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = make_engine(Settings(database_url=WORKER_DSN))
    return SqlPersistence(make_sessionmaker(_ENGINE))


_ENGINE = None


async def _dispose_engine() -> None:
    global _ENGINE
    if _ENGINE is not None:
        await _ENGINE.dispose()
        _ENGINE = None


async def _run_child(kind: str, command_id: str | None = None, mode: str = "normal") -> str:
    args = [PYTHON, __file__, "--child", kind, "--mode", mode]
    if command_id:
        args += ["--command-id", command_id]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
    text_out = out.decode("utf-8", errors="replace").strip()
    text_err = err.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        raise AssertionError(f"子进程失败({proc.returncode}): {text_err or text_out}")
    global _ENGINE
    _ENGINE = None
    return text_out


async def _clear_safety_commands() -> None:
    persistence = _make_persistence()
    async with system_transaction(persistence.sf) as session:
        await session.execute(text("DELETE FROM saga_safety_finance_idempotency"))
        await session.execute(text("DELETE FROM saga_safety_finance"))
        await session.execute(text(
            "DELETE FROM reconciliation_tasks WHERE command_id IN "
            "(SELECT command_id FROM saga_commands WHERE idempotency_key LIKE 'safety:%')"))
        await session.execute(text(
            "DELETE FROM outbox_events WHERE aggregate_id IN "
            "(SELECT command_id FROM saga_commands WHERE idempotency_key LIKE 'safety:%')"))
        await session.execute(text("DELETE FROM saga_commands WHERE idempotency_key LIKE 'safety:%'"))
        await session.execute(text(
            "DELETE FROM aftersale_tickets WHERE ticket_id::text LIKE 'safety:%'"))
    await _dispose_engine()


async def _enqueue(persistence: SqlPersistence, ticket_id: str, idempotency_key: str) -> str:
    return await persistence.enqueue_command(
        tenant_id=TENANT, ticket_id=ticket_id, run_id=str(uuid.uuid4()),
        action="REQUEST_REFUND_ONLY", order_id=ORDER_ID, refund_request_id=str(uuid.uuid4()),
        quote_id=str(uuid.uuid4()), quote_hash=f"hash-{idempotency_key}",
        plan_id=None, authorization_ref=f"approval:{idempotency_key}",
        idempotency_key=idempotency_key, payload_hash=f"payload-{idempotency_key}",
        payload={"test": idempotency_key}, priority=-100)


async def _command_status(persistence: SqlPersistence, command_id: str) -> str:
    rows = await persistence.list_command_statuses(tenant_id=TENANT, command_ids=[command_id])
    return str(rows[0]["status"]) if rows else "MISSING"


async def _finance_state(persistence: SqlPersistence) -> dict:
    async with system_transaction(persistence.sf) as session:
        row = (await session.execute(text(
            "SELECT refundable_minor, refund_count FROM saga_safety_finance WHERE order_id=:order_id"),
            {"order_id": ORDER_ID})).mappings().first()
    return dict(row or {})


async def _find_command(persistence: SqlPersistence, command_id: str) -> dict:
    async with system_transaction(persistence.sf) as session:
        row = (await session.execute(text(
            "SELECT command_id::text,tenant_id,ticket_id::text,run_id::text,action,"
            "refund_request_id::text,order_id,quote_id::text,quote_hash,waybill_no,sku_ids,"
            "authorization_ref,idempotency_key,payload_hash,payload,plan_id::text,attempt_count "
            "FROM saga_commands WHERE command_id=:command_id"),
            {"command_id": command_id})).mappings().first()
    assert row, f"command not found: {command_id}"
    return dict(row)


async def _refresh_command(persistence: SqlPersistence, command_id: str | None) -> dict | None:
    if not command_id:
        return None
    async with system_transaction(persistence.sf) as session:
        row = (await session.execute(text(
            "SELECT command_id::text,tenant_id,ticket_id::text,run_id::text,action,status,"
            "refund_request_id::text,order_id,quote_id::text,quote_hash,waybill_no,sku_ids,"
            "authorization_ref,idempotency_key,payload_hash,payload,plan_id::text,attempt_count "
            "FROM saga_commands WHERE command_id=:command_id"),
            {"command_id": command_id})).mappings().first()
    return dict(row) if row else None


async def _execute(persistence: SqlPersistence, command: dict, finance: PostgreSQLMockFinance) -> dict:
    return await _dispatcher(finance).dispatch(command)


async def _test_workflow_version_persistence() -> None:
    persistence = _make_persistence()
    ticket_ids = []
    for workflow_version in ("v3", "v4"):
        ticket = await persistence.create_ticket(
            tenant_id=TENANT, user_id="u_demo", order_id=ORDER_ID, intent="UNKNOWN",
            workflow_version=workflow_version)
        ticket_ids.append(ticket["ticket_id"])
        loaded = await persistence.get_ticket(tenant_id=TENANT, ticket_id=ticket["ticket_id"])
        assert loaded and loaded["workflow_version"] == workflow_version, (
            f"workflow_version 应持久化为 {workflow_version}，实际 {loaded}")
    async with system_transaction(persistence.sf) as session:
        await session.execute(text(
            "DELETE FROM aftersale_tickets WHERE ticket_id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": ticket_ids})
    await _dispose_engine()
    print("WORKFLOW_VERSION_PERSIST_OK")


async def _test_duplicate_enqueue() -> None:
    persistence = _make_persistence()
    idempotency_key = f"safety:duplicate:{uuid.uuid4()}"
    ticket_id = str(uuid.uuid4())
    first = await _enqueue(persistence, ticket_id, idempotency_key)
    second = await _enqueue(persistence, ticket_id, idempotency_key)
    async with system_transaction(persistence.sf) as session:
        count = await session.scalar(text(
            "SELECT count(*) FROM saga_commands WHERE tenant_id=:tenant AND idempotency_key=:key"),
            {"tenant": TENANT, "key": idempotency_key})
    assert count == 1, "相同幂等键重复提交必须只有一条数据库命令"
    async with system_transaction(persistence.sf) as session:
        await session.execute(text("DELETE FROM outbox_events WHERE aggregate_id=:cid"),
                              {"cid": first})
        await session.execute(text("DELETE FROM saga_commands WHERE command_id=:cid"),
                              {"cid": first})
        await session.execute(text("DELETE FROM aftersale_tickets WHERE ticket_id=:tid"),
                              {"tid": ticket_id})
    print("DUPLICATE_ENQUEUE_OK", {"command_ids": [first, second], "database_rows": count})
    await _dispose_engine()


async def _test_concurrent_workers() -> None:
    persistence = _make_persistence()
    finance = PostgreSQLMockFinance(persistence.sf)
    await finance.prepare(ORDER_ID, 20000)
    idempotency_key = f"safety:concurrent:{uuid.uuid4()}"
    command_id = await _enqueue(persistence, str(uuid.uuid4()), idempotency_key)
    other_commands: list[tuple[str, str | None]] = []
    async with system_transaction(persistence.sf) as session:
        rows = (await session.execute(text(
            "SELECT command_id::text, next_retry_at::text FROM saga_commands "
            "WHERE status='PREPARED' AND command_id<>:cid"),
            {"cid": command_id})).all()
        other_commands = [(str(row[0]), None if row[1] is None else str(row[1])) for row in rows]
        if other_commands:
            await session.execute(text(
                "UPDATE saga_commands SET next_retry_at=now()+interval '1 hour' "
                "WHERE status='PREPARED' AND command_id<>:cid"), {"cid": command_id})
    await _dispose_engine()

    first, second = await asyncio.gather(
        _run_child("claim", command_id), _run_child("claim", command_id))
    claims = [json.loads(value) for value in (first, second) if value != "null"]
    assert len(claims) == 1, f"两个独立进程只能有一个有效租约，实际 {claims}"
    assert claims[0]["command_id"] == command_id, "领取结果必须是目标命令"
    assert await _command_status(persistence, command_id) == "CLAIMED"
    normal_status = await _run_child("execute", command_id)
    assert normal_status == "CONFIRMED"
    assert await _command_status(persistence, command_id) == "CONFIRMED"
    state = await _finance_state(persistence)
    assert state == {"refundable_minor": 19000, "refund_count": 1}, state
    if other_commands:
        async with system_transaction(persistence.sf) as session:
            for other_id, next_retry_at in other_commands:
                await session.execute(text(
                    "UPDATE saga_commands SET next_retry_at=CAST(:next AS timestamptz) "
                    "WHERE command_id=:cid"),
                    {"next": next_retry_at, "cid": other_id})
    print("CONCURRENT_WORKER_OK", {"command_id": command_id, "claims": 1, "status": "CONFIRMED"})
    await _dispose_engine()


async def _test_crash_after_finance_success() -> None:
    persistence = _make_persistence()
    finance = PostgreSQLMockFinance(persistence.sf)
    await finance.prepare(ORDER_ID, 20000)
    idempotency_key = f"safety:crash:{uuid.uuid4()}"
    command_id = await _enqueue(persistence, str(uuid.uuid4()), idempotency_key)
    await _dispose_engine()

    provider_txn_id = await _run_child("execute", command_id, "crash")
    assert provider_txn_id
    assert await _command_status(persistence, command_id) == "CLAIMED"
    assert await _finance_state(persistence) == {"refundable_minor": 19000, "refund_count": 1}

    replay = json.loads(await _run_child("replay", command_id))
    assert replay["provider_txn_id"] == provider_txn_id, "崩溃重放必须复用稳定幂等键"
    assert await _finance_state(persistence) == {"refundable_minor": 19000, "refund_count": 1}

    await persistence.mark_command_result(command_id=command_id, tenant_id=TENANT,
                                           result={"status": "UNKNOWN", "error": "WORKER_CRASH"})
    task = await persistence.claim_due_reconciliation()
    assert task and task.get("command_id") == command_id, task
    assert task.get("refund_request_id"), "UNKNOWN 对账必须携带退款申请 ID"
    authority = await finance.get_reconciliation_result(
        tenant_id=TENANT, refund_request_id=task["refund_request_id"])
    assert authority["status"] == "SUCCEEDED"
    reconciled = await persistence.reconcile_command_result(command_id=command_id, result=authority)
    assert reconciled == "CONFIRMED"
    await persistence.complete_reconciliation(task_id=task["task_id"], status="DONE")
    assert await _command_status(persistence, command_id) == "CONFIRMED"
    print("CRASH_AFTER_FINANCE_OK", {
        "command_id": command_id, "provider_txn_id": provider_txn_id, "refund_count": 1})
    await _dispose_engine()


async def _test_response_lost() -> None:
    persistence = _make_persistence()
    finance = PostgreSQLMockFinance(persistence.sf)
    await finance.prepare(ORDER_ID, 20000)
    idempotency_key = f"safety:timeout:{uuid.uuid4()}"
    command_id = await _enqueue(persistence, str(uuid.uuid4()), idempotency_key)
    await _dispose_engine()

    first = json.loads(await _run_child("execute", command_id, "response_lost"))
    assert first["status"] == "UNKNOWN", "响应丢失不得被误报为成功"
    assert await _command_status(persistence, command_id) == "UNKNOWN"
    assert await _finance_state(persistence) == {"refundable_minor": 19000, "refund_count": 1}

    task = await persistence.claim_due_reconciliation()
    assert task and task.get("command_id") == command_id, task
    assert task.get("refund_request_id"), "响应丢失对账必须携带退款申请 ID"
    authority = await finance.get_reconciliation_result(
        tenant_id=TENANT, refund_request_id=task["refund_request_id"])
    assert authority["status"] == "SUCCEEDED"
    assert await persistence.reconcile_command_result(command_id=command_id, result=authority) == "CONFIRMED"
    await persistence.complete_reconciliation(task_id=task["task_id"], status="DONE")
    assert await _command_status(persistence, command_id) == "CONFIRMED"
    assert await _finance_state(persistence) == {"refundable_minor": 19000, "refund_count": 1}
    print("RESPONSE_LOST_OK", {
        "command_id": command_id, "first_result": "UNKNOWN", "reconciled": "CONFIRMED",
        "refund_count": 1})
    await _dispose_engine()


async def _child(kind: str, command_id: str | None, mode: str) -> None:
    persistence = _make_persistence()
    finance = PostgreSQLMockFinance(persistence.sf)
    current = await _refresh_command(persistence, command_id)
    status = "CONFIRMED"
    try:
        if kind == "claim":
            if current and current.get("status") == "PREPARED":
                claimed = await persistence.claim_next_command()
                current = claimed
            print(json.dumps(current, default=str) if current else "null")
        elif kind == "execute":
            assert current
            if current.get("status") == "PREPARED":
                claimed = await persistence.claim_next_command()
                if not claimed or claimed["command_id"] != command_id:
                    raise AssertionError("执行子进程未能领取目标命令")
                current = claimed
            result = await _execute(persistence, current, finance)
            if mode == "crash":
                print(result["provider_txn_id"], flush=True)
                os._exit(0)  # 有意跳过 mark_command_result，模拟崩溃窗口。
            if mode == "response_lost":
                await persistence.mark_command_result(
                    command_id=current["command_id"], tenant_id=TENANT,
                    result={"status": "UNKNOWN", "error": "RESPONSE_LOST"})
                status = "UNKNOWN"
                print(json.dumps({"status": status, "error": "RESPONSE_LOST"}))
            else:
                status = await persistence.mark_command_result(
                    command_id=current["command_id"], tenant_id=TENANT, result=result)
                print(status)
        elif kind == "replay":
            assert current
            result = await _execute(persistence, current, finance)
            print(json.dumps(result, default=str))
        else:
            raise AssertionError(f"unknown child kind: {kind}")
    finally:
        await _dispose_engine()


async def main() -> None:
    if "--child" in sys.argv:
        kind = sys.argv[sys.argv.index("--child") + 1]
        command_id = sys.argv[sys.argv.index("--command-id") + 1] if "--command-id" in sys.argv else None
        mode = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv else "normal"
        await _child(kind, command_id, mode)
        return

    await _clear_safety_commands()
    await _test_workflow_version_persistence()
    await _test_duplicate_enqueue()
    await _test_concurrent_workers()
    await _test_crash_after_finance_success()
    await _test_response_lost()
    print("DB_SAGA_SAFETY_OK")


if __name__ == "__main__":
    asyncio.run(main())

