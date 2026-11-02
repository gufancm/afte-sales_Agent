# -*- coding: utf-8 -*-
"""检索链路自检：查询变换（多查询+HyDE）-> BM25 词法 + 向量 Dense -> RRF -> BGE Reranker。

用法（容器内，需 .env 的 DB/embedding/rerank 配置）：
  docker compose run --rm api python scripts/check_retrieval.py "衣服有质量问题要退货退款"
  docker compose run --rm api python scripts/check_retrieval.py "..." --no-transform   # 跳过 LLM 查询变换
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.bootstrap import build_deps  # noqa: E402
from app.config import get_settings  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser(description="检索链路自检")
    ap.add_argument("query")
    ap.add_argument("--tenant", default="tenant_demo")
    ap.add_argument("--no-transform", action="store_true", help="跳过 LLM 查询变换（等价 policy=off）")
    ap.add_argument("--transform-policy", default=None, choices=["off", "always", "adaptive"],
                    help="覆盖 RAG_TRANSFORM_POLICY；adaptive=首轮低置信才触发")
    ap.add_argument("--history", action="append", default=None,
                    help="模拟前几轮用户话术（可重复），用于验证指代的零成本上下文扩展")
    a = ap.parse_args()

    settings = get_settings()
    if a.transform_policy:
        settings.rag_transform_policy = a.transform_policy
    if a.no_transform:
        settings.rag_transform_policy = "off"
    deps = build_deps(settings)
    if settings.rag_transform_policy == "off":
        deps.retriever.transformer = None  # type: ignore[attr-defined]

    chunks, diag = await deps.retriever.discover_ex(  # type: ignore[attr-defined]
        tenant_id=a.tenant, query=a.query, filters={}, history=a.history)
    print("== 检索诊断 ==")
    print(json.dumps({k: v for k, v in diag.items() if k != "variants"}, ensure_ascii=False, indent=2))
    print("== 查询变体 ==")
    for i, v in enumerate(diag.get("variants", [])):
        print(f"  [{i}] {v[:80]}")
    print("== Top-K（重排后）==")
    for i, c in enumerate(chunks, 1):
        print(f"  {i}. {c.clause_id}  (score={c.score})  {c.text[:60].replace(chr(10), ' ')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))