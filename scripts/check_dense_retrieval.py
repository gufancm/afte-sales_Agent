# -*- coding: utf-8 -*-
"""RAG Dense 检索冒烟测试：embedding 服务 -> pgvector 最近邻。

用法：
  python scripts/check_dense_retrieval.py --query "七天无理由退货的运费谁承担" --top-k 5
宿主机直跑时把地址指向本机服务：--embedding-base-url http://localhost:7997/v1
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

import asyncpg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402


def _dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _embed_one(base: str, key: str, model: str, text: str) -> list[float]:
    import httpx
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{base.rstrip('/')}/embeddings", headers=headers,
                         json={"model": model, "input": text})
        r.raise_for_status()
        return r.json()["data"][0]["embedding"]


async def main() -> int:
    ap = argparse.ArgumentParser(description="Dense 检索冒烟测试")
    ap.add_argument("--query", required=True)
    ap.add_argument("--tenant", default="tenant_demo")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--database-url", default=None)
    ap.add_argument("--embedding-base-url", default=None)
    a = ap.parse_args()

    s = get_settings()
    base = a.embedding_base_url or s.embedding_base_url or s.llm_base_url
    key = s.embedding_api_key or s.llm_api_key
    model = s.embedding_model
    if not model:
        print("[!] EMBEDDING_MODEL 未配置，无法做 Dense 检索。")
        return 2

    vec = await _embed_one(base, key, model, a.query)
    lit = "[" + ",".join(repr(float(x)) for x in vec) + "]"

    conn = await asyncpg.connect(_dsn(a.database_url or s.database_url))
    try:
        await conn.execute("SELECT set_config('app.tenant_id',$1,false)", a.tenant)
        rows = await conn.fetch(
            "SELECT chunk_id, doc_id, clause_id, heading_path, "
            "       (embedding <=> CAST($2 AS vector)) AS dist "
            "FROM policy_chunks "
            "WHERE tenant_id=$1 AND embedding IS NOT NULL AND embedding_model=$3 "
            "ORDER BY dist ASC LIMIT $4",
            a.tenant, lit, model, a.top_k)
        print(f"query={a.query!r}")
        print(f"model={model} dim={len(vec)} hits={len(rows)}")
        for r in rows:
            print(f"  dist={r['dist']:.4f}  chunk={r['chunk_id']}  clause={r['clause_id']}  "
                  f"doc={r['doc_id']}  heading={str(r['heading_path'])[:60]}")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))