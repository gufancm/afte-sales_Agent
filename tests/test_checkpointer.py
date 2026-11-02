# -*- coding: utf-8 -*-
"""Postgres Checkpointer：DSN 派生、降级策略、以及「重启后可恢复 interrupt」的真实回归。

最后一条是关键：它用**两个独立的 saver 实例 + 两个独立的图对象**模拟进程重启，
证明 HITL 暂停点真的落在 PostgreSQL，而不是只活在内存里。
未提供可用数据库时该用例跳过（本机默认 127.0.0.1:5432）。
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict

from app.config import Settings
from app.graph.checkpointer import _mask_dsn, open_checkpointer

DSN = os.getenv("CHECKPOINT_TEST_DSN",
                "postgresql://agent_user:change-me@127.0.0.1:5432/aftersale")


def _pg_settings(**kw) -> Settings:
    base = dict(checkpoint_backend="postgres", checkpoint_database_url=DSN,
                checkpoint_setup_on_start=True, checkpoint_connect_timeout_s=3.0)
    base.update(kw)
    return Settings(**base)


class _S(TypedDict, total=False):
    steps: list[str]


def test_checkpoint_dsn_strips_async_driver():
    s = Settings(database_url="postgresql+asyncpg://u:p@db:5432/x")
    assert s.checkpoint_dsn == "postgresql://u:p@db:5432/x"
    s2 = Settings(database_url="postgresql+asyncpg://u:p@db:5432/x",
                  checkpoint_database_url="postgresql://u2:p2@db2:5433/y")
    assert s2.checkpoint_dsn == "postgresql://u2:p2@db2:5433/y"


def test_mask_dsn_hides_password():
    assert "p@ss" not in _mask_dsn("postgresql://u:p@ss@db:5432/x")
    assert _mask_dsn("postgresql://u:p@ss@db:5432/x").startswith("postgresql://u:***@")


async def test_memory_backend_is_not_durable():
    h = await open_checkpointer(Settings(checkpoint_backend="memory"))
    assert h.backend == "memory"
    assert h.durable is False
    await h.aclose()


async def test_unreachable_postgres_falls_back():
    h = await open_checkpointer(_pg_settings(
        checkpoint_database_url="postgresql://u:p@127.0.0.1:1/x",
        checkpoint_connect_timeout_s=1.0, checkpoint_setup_on_start=False))
    assert h.backend == "memory"
    assert h.detail.startswith("fallback")
    await h.aclose()


async def test_strict_mode_refuses_to_start():
    with pytest.raises(RuntimeError):
        await open_checkpointer(_pg_settings(
            checkpoint_database_url="postgresql://u:p@127.0.0.1:1/x",
            checkpoint_connect_timeout_s=1.0, checkpoint_setup_on_start=False,
            checkpoint_strict=True))


def _graph():
    async def ask(state: _S) -> _S:
        answer = interrupt({"need": "approval"})
        return {"steps": list(state.get("steps") or []) + [f"resumed:{answer['ok']}"]}

    g = StateGraph(_S)
    g.add_node("ask", ask)
    g.add_edge(START, "ask")
    g.add_edge("ask", END)
    return g


def _cleanup(thread_id: str) -> None:
    try:
        import psycopg
        with psycopg.connect(DSN, connect_timeout=5) as conn:
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                conn.execute(f"delete from {table} where thread_id = %s", (thread_id,))
            conn.commit()
    except Exception:  # pragma: no cover - 清理失败不影响断言
        pass


async def _restart_roundtrip() -> None:
    thread_id = f"test-ckpt-{uuid.uuid4()}"
    cfg = {"configurable": {"thread_id": thread_id}}

    h1 = await open_checkpointer(_pg_settings())
    if not h1.durable:
        await h1.aclose()
        pytest.skip(f"Postgres checkpointer 不可用：{h1.detail}")
    try:
        first = await _graph().compile(checkpointer=h1.saver).ainvoke(
            {"steps": ["start"]}, cfg)
        assert "__interrupt__" in first
    finally:
        await h1.aclose()

    # 模拟进程重启：全新 saver 实例 + 全新编译的图，仅靠 thread_id 找回暂停点
    h2 = await open_checkpointer(_pg_settings())
    try:
        second = await _graph().compile(checkpointer=h2.saver).ainvoke(
            Command(resume={"ok": True}), cfg)
        assert second["steps"] == ["start", "resumed:True"]
    finally:
        await h2.aclose()
        _cleanup(thread_id)


def test_interrupt_survives_simulated_restart():
    # Windows 的 ProactorEventLoop 不被 psycopg 异步驱动支持，必须显式用 Selector 循环
    if sys.platform == "win32":
        asyncio.run(_restart_roundtrip(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(_restart_roundtrip())
