# -*- coding: utf-8 -*-
"""LangGraph Checkpointer 装配（HITL 暂停点的持久化，§18.3）。

为什么需要它：
    `hitl_review` 通过 `interrupt()` 把图挂起，等待人工审批后 `Command(resume=...)` 续跑。
    基线用 `InMemorySaver`，进程重启或多副本部署时暂停点会丢失，审批恢复直接 404。
    生产装配改为 PostgreSQL（`AsyncPostgresSaver`），把 checkpoint 落到同库不同表。

降级策略：
    连接失败时不静默吞掉——默认 `checkpoint_strict=false` 会降级为 `InMemorySaver`
    并输出 ERROR 日志、在 `/ready` 暴露 `checkpointer` 字段；
    生产可设 `CHECKPOINT_STRICT=true` 让启动直接失败，避免「以为能恢复、其实不能」。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings

log = logging.getLogger("aftersale.checkpointer")


@dataclass
class CheckpointerHandle:
    """Checkpointer 运行态句柄：saver 交给图，closer 负责随应用生命周期释放。"""

    saver: Any | None
    backend: str
    detail: str = ""
    _cm: Any = field(default=None, repr=False)

    async def aclose(self) -> None:
        if self._cm is None:
            return
        try:
            await self._cm.__aexit__(None, None, None)
        except Exception as exc:  # pragma: no cover - 关闭期异常不影响退出
            log.warning("checkpointer 关闭失败：%s", exc)
        finally:
            self._cm = None

    @property
    def durable(self) -> bool:
        return self.backend == "postgres"


def _memory_handle(detail: str = "") -> CheckpointerHandle:
    from langgraph.checkpoint.memory import InMemorySaver

    return CheckpointerHandle(saver=InMemorySaver(), backend="memory", detail=detail)


async def open_checkpointer(settings: Settings) -> CheckpointerHandle:
    """按配置打开 Checkpointer。postgres 失败时按 strict 决定失败或降级。"""
    backend = (settings.checkpoint_backend or "memory").lower()
    if backend != "postgres":
        return _memory_handle("configured:memory")

    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        dsn = settings.checkpoint_dsn
        cm = AsyncPostgresSaver.from_conn_string(dsn)
        saver = await asyncio.wait_for(cm.__aenter__(),
                                       timeout=max(1.0, settings.checkpoint_connect_timeout_s))
        if settings.checkpoint_setup_on_start:
            await asyncio.wait_for(saver.setup(),
                                   timeout=max(1.0, settings.checkpoint_setup_timeout_s))
        log.info("checkpointer=postgres 就绪（dsn=%s）", _mask_dsn(dsn))
        return CheckpointerHandle(saver=saver, backend="postgres", detail="configured:postgres", _cm=cm)
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        if settings.checkpoint_strict:
            raise RuntimeError(f"CHECKPOINT_STRICT=true 且 Postgres Checkpointer 不可用：{msg}") from exc
        log.error("Postgres Checkpointer 不可用，降级为 InMemorySaver（重启后 HITL 暂停点会丢失）：%s", msg)
        return _memory_handle(f"fallback:postgres_unavailable:{msg[:160]}")


def _mask_dsn(dsn: str) -> str:
    """日志脱敏：只保留 user@host:port/db。"""
    try:
        head, _, tail = dsn.partition("://")
        creds, _, rest = tail.partition("@")
        user = creds.split(":", 1)[0]
        return f"{head}://{user}:***@{rest}"
    except Exception:  # pragma: no cover
        return "<unparsable-dsn>"
