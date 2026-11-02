"""数据库引擎与事务封装（LLD §18.7 / §19.2）。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings


def make_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_min,
        max_overflow=max(0, settings.db_pool_max - settings.db_pool_min),
        pool_pre_ping=True,
        connect_args={
            "server_settings": {
                "statement_timeout": str(settings.db_statement_timeout_ms),
                "lock_timeout": str(settings.db_lock_timeout_ms),
            }
        },
    )


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def tenant_transaction(session_factory: async_sessionmaker[AsyncSession], tenant_id: str):
    """租户作用域事务。set_config(..., true) 仅在本事务有效；无租户 -> 拒绝（§18.7）。"""
    if not tenant_id:
        raise ValueError("tenant_id required")
    async with session_factory() as session:
        async with session.begin():
            await session.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id})
            yield session


@asynccontextmanager
async def system_transaction(session_factory: async_sessionmaker[AsyncSession]):
    """系统级事务（Worker / 对账）。生产应由独立的服务角色连接，且该角色不得用于业务读写。
    注意：若目标表启用 RLS，此路径需要服务角色显式授权或走单独只读路径（§12.2/§19.2）。"""
    async with session_factory() as session:
        async with session.begin():
            yield session