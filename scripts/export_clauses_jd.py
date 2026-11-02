# -*- coding: utf-8 -*-
"""把真实平台语料（policy_chunks）导出为 policy/clauses_jd.json（单一事实源，入库/内存共用）。

用法（容器内，或宿主机把 DSN 主机换成 localhost）：
  python scripts/export_clauses_jd.py
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys

import asyncpg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app.config import get_settings  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "policy" / "clauses_jd.json"
TENANT = "tenant_demo"


async def main() -> None:
    settings = get_settings()
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    # 导出用 worker 角色（可跨租户读）
    dsn = getattr(settings, "effective_worker_database_url", settings.database_url)
    dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT clause_id,doc_id,version,logical_clause_id,parent_id,clause_group_id,is_exception,"
            "text,heading_path,token_count FROM policy_chunks WHERE tenant_id=$1 "
            "ORDER BY doc_id, clause_id", TENANT)
        docs = await conn.fetch(
            "SELECT doc_id,title,version FROM policy_documents WHERE tenant_id=$1 AND doc_id LIKE 'jd\\_%' "
            "ORDER BY doc_id", TENANT)
    finally:
        await conn.close()

    clauses = [{
        "clause_id": r["clause_id"], "doc_id": r["doc_id"], "version": r["version"],
        "logical_clause_id": r["logical_clause_id"], "parent_id": r["parent_id"],
        "clause_group_id": r["clause_group_id"],
        "is_exception": bool(r["is_exception"]), "text": r["text"],
        "heading_path": list(r["heading_path"] or []), "token_count": r["token_count"],
    } for r in rows]
    payload = {
        "note": "真实平台语料（京东开放平台官方规则）导出自 policy_chunks；与 policy/rules.json 同源。",
        "tenant": TENANT,
        "version": (clauses[0]["version"] if clauses else "v1"),
        "documents": [{"doc_id": d["doc_id"], "title": d["title"], "version": d["version"]} for d in docs],
        "clauses": clauses,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {OUT} docs={len(docs)} clauses={len(clauses)}")


if __name__ == "__main__":
    asyncio.run(main())