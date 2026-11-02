"""按顺序应用 migrations/sql 下的迁移（§19.2 顺序）。用法：python scripts/migrate.py

- 使用 asyncpg 的 simple-query 协议执行整文件多语句（含 DO $$ ... $$ 块）；
- 以 schema_migrations 记录已应用文件，重复执行是幂等的（适合容器每次 up 运行）；
- 若目标库已存在历史 schema 但无迁移记录（旧库），自动标记现有文件为已应用（bootstrap），
  避免对已存在的对象重复 CREATE/ALTER 报错。
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

import asyncpg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402

SQL_DIR = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "sql"


def _asyncpg_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def main() -> None:
    settings = get_settings()
    conn = await asyncpg.connect(_asyncpg_dsn(settings.database_url))
    files = sorted(SQL_DIR.glob("*.sql"))
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        applied = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}

        if not applied:
            existing = await conn.fetchval("SELECT to_regclass('public.tenants')")
            if existing is not None:
                async with conn.transaction():
                    for f in files:
                        await conn.execute(
                            "INSERT INTO schema_migrations(version) VALUES($1) ON CONFLICT DO NOTHING", f.name)
                print("detected existing schema; bootstrapped migration records (no-op)")
                return

        for f in files:
            if f.name in applied:
                print(f"skip {f.name} (already applied)")
                continue
            sql = f.read_text(encoding="utf-8")
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute("INSERT INTO schema_migrations(version) VALUES($1)", f.name)
            print(f"applied {f.name}")
    finally:
        await conn.close()
    print("migrations complete")


if __name__ == "__main__":
    asyncio.run(main())
