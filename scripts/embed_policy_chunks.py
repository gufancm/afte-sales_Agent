# -*- coding: utf-8 -*-
"""为已入库的 policy_chunks 回填 embedding（OpenAI 兼容 /embeddings）。

前提：docs 已由 build_policy_index.py 入库（policy_chunks 有文本、embedding 为 NULL）。
配置来自环境变量/.env（app.config.Settings）：EMBEDDING_BASE_URL / EMBEDDING_API_KEY /
EMBEDDING_MODEL / EMBEDDING_DIM（默认 1024，对应 BGE-M3；如用 OpenAI 1536 需显式设 1536）。

用法：
  python scripts/embed_policy_chunks.py --dry-run            # 只看待回填数量
  python scripts/embed_policy_chunks.py --tenant tenant_demo # 实际回填
  python scripts/embed_policy_chunks.py --create-index        # 回填后建 HNSW 索引
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


async def _embed(base: str, key: str, model: str, texts: list[str], dim: int | None,
                 batch: int = 16) -> list[list[float]]:
    import httpx
    out: list[list[float]] = []
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with httpx.AsyncClient(timeout=90) as c:
        for i in range(0, len(texts), batch):
            payload = {"model": model, "input": texts[i:i + batch]}
            if dim:
                payload["dimensions"] = dim   # OpenAI v3 等支持；不支持的服务会忽略/报错
            r = await c.post(f"{base.rstrip('/')}/embeddings", headers=headers, json=payload)
            r.raise_for_status()
            data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
            out.extend(d["embedding"] for d in data)
    return out


def _vec(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


async def main() -> int:
    ap = argparse.ArgumentParser(description="policy_chunks embedding 回填")
    ap.add_argument("--tenant", default="tenant_demo")
    ap.add_argument("--database-url", default=None)
    ap.add_argument("--doc-id", default=None, help="仅回填某文档")
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--dim", type=int, default=None, help="覆盖维度（默认取 EMBEDDING_DIM）")
    ap.add_argument("--create-index", action="store_true", help="回填后建 HNSW 索引")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    s = get_settings()
    dim = a.dim or s.embedding_dim
    base = s.embedding_base_url or s.llm_base_url
    key = s.embedding_api_key or s.llm_api_key
    model = s.embedding_model

    dsn = _dsn(a.database_url or s.database_url)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("SELECT set_config('app.tenant_id',$1,false)", a.tenant)
        where = "tenant_id=$1 AND embedding IS NULL"
        params: list = [a.tenant]
        if a.doc_id:
            where += " AND doc_id=$2"
            params.append(a.doc_id)
        rows = await conn.fetch(f"SELECT chunk_id, text, token_count FROM policy_chunks WHERE {where} ORDER BY doc_id, clause_id", *params)
        print(f"待回填 chunk={len(rows)}；model={model or '(未配置)'} dim={dim} base={base}")
        if a.dry_run:
            print("dry-run：未调用 embedding、未写库。")
            return 0
        if not model:
            print("[!] 未配置 EMBEDDING_MODEL：请在 .env 设置后重试（或在 .env 用 EMBEDDING_MODEL）。")
            return 2
        if not rows:
            print("没有需要回填的 chunk。")
            return 0

        texts = [r["text"] for r in rows]
        vecs = await _embed(base, key, model, texts, a.dim or None, a.batch)
        if len(vecs) != len(rows):
            print(f"[!] 返回向量数({len(vecs)}) 与 文本数({len(rows)}) 不一致，中止。")
            return 3
        got_dim = len(vecs[0])
        cur = await conn.fetchval(
            "SELECT atttypmod FROM pg_attribute WHERE attrelid='policy_chunks'::regclass AND attname='embedding'")
        if cur != got_dim:
            await conn.execute(f"ALTER TABLE policy_chunks ALTER COLUMN embedding TYPE vector({int(got_dim)})")
            print(f"embedding 维度已调整为 vector({got_dim})")
        for r, v in zip(rows, vecs):
            await conn.execute(
                "UPDATE policy_chunks SET embedding=CAST($2 AS vector), embedding_model=$3 WHERE chunk_id=$1",
                r["chunk_id"], _vec(v), model)
        print(f"已回填 {len(rows)} 条（dim={got_dim}，model={model}）")

        if a.create_index:
            idx = "idx_chunks_embedding_hnsw"
            try:
                await conn.execute(
                    f"CREATE INDEX IF NOT EXISTS {idx} ON policy_chunks USING hnsw (embedding vector_cosine_ops)")
                print(f"已创建/确认 HNSW 索引 {idx}")
            except asyncpg.exceptions.InsufficientPrivilegeError:
                print("[!] 建索引需表 owner 权限；应用角色无 DDL，由 migrations/sql/0011 负责，可忽略。")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))