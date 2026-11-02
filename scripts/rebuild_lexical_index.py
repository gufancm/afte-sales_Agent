# -*- coding: utf-8 -*-
"""为 policy_chunks 回填真实 BM25 词法索引（tokens: {term:tf} 与 doc_len）。

真实 BM25 检索（app/rag/pg.py PgBM25）依赖 tokens/doc_len 两列。若条款是早期入库的（无这两列），
或修改了分词器，运行本脚本重建索引；新入库走 ingest_policy.py 会自动写入。

用法：
  python scripts/rebuild_lexical_index.py --dry-run
  python scripts/rebuild_lexical_index.py --tenant tenant_demo
  python scripts/rebuild_lexical_index.py --tenant tenant_demo --all   # 连已填充的也重算
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys

import asyncpg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.rag.tokenize import backend_name, token_counts  # noqa: E402


def _dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def main() -> int:
    ap = argparse.ArgumentParser(description="重建 policy_chunks 的 BM25 词法索引")
    ap.add_argument("--tenant", default="tenant_demo")
    ap.add_argument("--database-url", default=None)
    ap.add_argument("--doc-id", default=None)
    ap.add_argument("--all", action="store_true", help="重算全部（默认只补 tokens 为空的）")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    settings = get_settings()
    dsn = _dsn(a.database_url or settings.database_url)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("SELECT set_config('app.tenant_id', $1, false)", a.tenant)
        where = "tenant_id=$1"
        params: list = [a.tenant]
        if a.doc_id:
            params.append(a.doc_id)
            where += f" AND doc_id=${len(params)}"
        if not a.all:
            where += " AND (tokens IS NULL OR tokens = '{}'::jsonb OR doc_len IS NULL)"
        rows = await conn.fetch(f"SELECT clause_id, version, text FROM policy_chunks WHERE {where}", *params)
        print(f"待重建 {len(rows)} 条（分词后端={backend_name()}）")
        if a.dry_run:
            return 0
        updated = 0
        async with conn.transaction():
            for r in rows:
                tc = token_counts(r["text"] or "")
                doc_len = sum(tc.values())
                await conn.execute(
                    "UPDATE policy_chunks SET tokens=CAST($3 AS jsonb), doc_len=$4 "
                    "WHERE tenant_id=$1 AND clause_id=$2 AND version=$5",
                    a.tenant, r["clause_id"], json.dumps(tc, ensure_ascii=False), doc_len, r["version"])
                updated += 1
        print(f"已重建 {updated} 条")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))