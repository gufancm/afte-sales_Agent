"""Saga Worker：消费 saga_commands（唯一外部写执行者）与对账任务（§18.10 / §19.1.6/7）。

- 从 Agent DB 领取 PREPARED 命令 -> 经 MCP 写工具执行 -> 回写命令状态；
- 超时/网络异常一律记 UNKNOWN，绝不盲重试；UNKNOWN 自动生成对账任务，由本进程回查 Finance 权威状态；
- 使用独立的 Worker 数据库角色连接（可含 BYPASSRLS，仅用于跨租户的系统事务）。

用法：python scripts/run_worker.py
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import pathlib
import signal
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.bootstrap import build_deps  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.saga.dispatcher import SagaDispatcher  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("aftersale.worker")

_stop = asyncio.Event()


def _install_signal_handlers() -> None:
    with contextlib.suppress(Exception):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, AttributeError):
                loop.add_signal_handler(sig, _stop.set)


async def _dispatch_once(deps, dispatcher: SagaDispatcher) -> bool:
    command = await deps.persistence.claim_next_command()
    if not command:
        return False
    cid = command["command_id"]
    tenant_id = command["tenant_id"]
    try:
        result = await dispatcher.dispatch(command)
    except (asyncio.TimeoutError, TimeoutError):
        result = {"status": "UNKNOWN", "error": "TIMEOUT"}
    except Exception as exc:  # 网络/未知异常 -> UNKNOWN（§9.8）
        result = {"status": "UNKNOWN", "error": str(exc)[:200]}
    status = await deps.persistence.mark_command_result(command_id=cid, tenant_id=tenant_id, result=result)
    with contextlib.suppress(Exception):  # Outbox relay：命令已被派发 -> 标记 outbox PUBLISHED
        await deps.persistence.mark_outbox_published(aggregate_id=cid)
    with contextlib.suppress(Exception):
        await deps.persistence.write_inbox(tenant_id=tenant_id, source="saga",
                                           dedup_key=cid, payload={"command_id": cid, "status": status})
    log.info("command=%s action=%s -> %s", cid, command.get("action"), status)
    return True


async def _reconcile_once(deps) -> bool:
    """处理一条到期对账任务：优先按 command_id 回查，其次按 refund_request_id 回查 Finance。"""
    task = await deps.persistence.claim_due_reconciliation()
    if not task:
        return False
    rrid = task.get("refund_request_id")
    cid = task.get("command_id")
    reason = task.get("reason")
    max_attempts = task.get("max_attempts", 10)
    attempt = task.get("attempt_count", 0)
    status = "DONE"
    try:
        if rrid:
            res = await deps.finance.get_reconciliation_result(tenant_id=task["tenant_id"], refund_request_id=rrid)
            s = (res or {}).get("status", "UNKNOWN")
            if s in ("SUCCEEDED", "SETTLED", "FAILED_CONFIRMED"):
                status = "DONE"
            else:
                status = "PENDING" if attempt < max_attempts else "MANUAL_RECONCILE"
        elif cid:
            # 命令级对账：只有在命令已有终态（CONFIRMED / FAILED_CONFIRMED）时才判 DONE
            sts = await deps.persistence.list_command_statuses(tenant_id=task["tenant_id"], command_ids=[cid])
            st = sts[0].get("status") if sts else "UNKNOWN"
            if st in ("CONFIRMED", "FAILED_CONFIRMED"):
                status = "DONE"
            else:
                status = "PENDING" if attempt < max_attempts else "MANUAL_RECONCILE"
        else:
            status = "PENDING" if attempt < max_attempts else "MANUAL_RECONCILE"
    except Exception as exc:
        log.warning("reconcile error task=%s: %s", task.get("task_id"), exc)
        status = "PENDING" if attempt < max_attempts else "MANUAL_RECONCILE"
    await deps.persistence.complete_reconciliation(
        task_id=task["task_id"], status=status, next_run_at_seconds=30 if status == "PENDING" else 0)
    if status == "MANUAL_RECONCILE":
        with contextlib.suppress(Exception):
            await deps.persistence.append_audit(
                tenant_id=task["tenant_id"], ticket_id=task.get("ticket_id"), event="RECONCILE_NEEDS_MANUAL",
                payload={"task_id": task.get("task_id"), "reason": reason, "command_id": cid,
                         "refund_request_id": rrid, "attempts": attempt})
        with contextlib.suppress(Exception):  # 人工对账队列（inbox 去重）+ 告警
            await deps.persistence.write_inbox(
                tenant_id=task["tenant_id"], source="manual_reconcile", dedup_key=task.get("task_id"),
                payload={"task_id": task.get("task_id"), "reason": reason, "command_id": cid,
                         "refund_request_id": rrid, "ticket_id": task.get("ticket_id"), "severity": "HIGH"})
        log.warning("reconcile task=%s -> MANUAL_RECONCILE (reason=%s attempts=%s)",
                    task.get("task_id"), reason, attempt)
    else:
        log.info("reconcile task=%s -> %s", task.get("task_id"), status)
    return True


async def _sweep(deps) -> None:
    """周期性兜底：回收租约过期的命令、升级 SLA 过期的审批（幂等、可观测、落审计）。"""
    settings = deps.settings
    with contextlib.suppress(Exception):
        reclaimed = await deps.persistence.reclaim_expired_commands(
            max_attempts=int(getattr(settings, "worker_command_max_attempts", 3) or 3))
        for r in reclaimed:
            log.warning("lease expired: command=%s ticket=%s -> UNKNOWN", r.get("command_id"), r.get("ticket_id"))
            with contextlib.suppress(Exception):
                await deps.persistence.append_audit(
                    tenant_id=r["tenant_id"], ticket_id=r.get("ticket_id"), event="COMMAND_LEASE_EXPIRED",
                    payload={"command_id": r.get("command_id")})
    with contextlib.suppress(Exception):
        expired = await deps.persistence.escalate_expired_approvals()
        for a in expired:
            log.warning("approval SLA expired: approval=%s ticket=%s -> EXPIRED",
                        a.get("approval_id"), a.get("ticket_id"))
            with contextlib.suppress(Exception):
                await deps.persistence.append_audit(
                    tenant_id=a["tenant_id"], ticket_id=a.get("ticket_id"), event="APPROVAL_SLA_EXPIRED",
                    payload={"approval_id": a.get("approval_id")})


async def main() -> None:
    settings = get_settings()
    settings = settings.model_copy(update={"database_url": settings.effective_worker_database_url})
    deps = build_deps(settings)
    dispatcher = SagaDispatcher(orders=deps.tools.orders, logistics=deps.tools.logistics, finance=deps.tools.finance)
    _install_signal_handlers()
    poll = max(0.05, settings.worker_poll_interval_ms / 1000)
    log.info("worker started (target=%s)", settings.database_url.rsplit("@", 1)[-1])
    last_sweep = 0.0
    while not _stop.is_set():
        did = await _dispatch_once(deps, dispatcher)
        did = await _reconcile_once(deps) or did
        if time.monotonic() - last_sweep >= 5.0:
            await _sweep(deps)
            last_sweep = time.monotonic()
        if not did:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(_stop.wait(), timeout=poll)
    log.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
