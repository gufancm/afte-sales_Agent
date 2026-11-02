# -*- coding: utf-8 -*-
"""导出政策库切分结果（供人工核对）：文档清单 + Parent（章/条） + Child 全文。
用法：python scripts/export_policy_chunks.py [--tenant tenant_demo] [--out-dir E:\\agent]
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import pathlib
import sys

import asyncpg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

APP_DSN = "postgresql://aftersale_app:app_pw_change_me@localhost:5432/aftersale"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default="tenant_demo")
    ap.add_argument("--database-url", default=None)
    ap.add_argument("--out-dir", default=r"E:\agent")
    a = ap.parse_args()
    dsn = (a.database_url or APP_DSN).replace("postgresql+asyncpg://", "postgresql://", 1)
    out = pathlib.Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("SELECT set_config('app.tenant_id',$1,false)", a.tenant)
        docs = await conn.fetch(
            "SELECT doc_id,title,version,status,effective_from,source_uri FROM policy_documents "
            "WHERE tenant_id=$1 ORDER BY doc_id", a.tenant)
        parents = await conn.fetch(
            "SELECT parent_id,doc_id,version,level,logical_clause_id,heading_path,order_index,"
            "is_exception,token_count,left(display_text,80) AS preview FROM policy_parents "
            "WHERE tenant_id=$1 ORDER BY doc_id, order_index", a.tenant)
        chunks = await conn.fetch(
            "SELECT doc_id,clause_id,parent_id,logical_clause_id,clause_group_id,is_exception,version,"
            "token_count,text,embedding_model,(embedding IS NULL) AS emb_null FROM policy_chunks "
            "WHERE tenant_id=$1 ORDER BY doc_id, clause_id", a.tenant)

        lines = [f"# 政策库切分结果导出（tenant={a.tenant}）", "",
                 f"- 文档数：{len(docs)}；Parent 数：{len(parents)}；Child(chunk) 数：{len(chunks)}",
                 "- 结构：Docling 解析 → 条款解析器（章/条）→ HybridChunker Child（parent_id 关联条级 Parent）", ""]
        lines.append("## 一、已入库文档清单")
        lines.append("")
        lines.append("| doc_id | 标题 | 版本 | 状态 | source_uri | Parent | Child |")
        lines.append("|---|---|---|---|---|---|---|")
        for d in docs:
            np = sum(1 for p in parents if p["doc_id"] == d["doc_id"])
            nc = sum(1 for c in chunks if c["doc_id"] == d["doc_id"])
            lines.append(f"| {d['doc_id']} | {d['title']} | {d['version']} | {d['status']} | "
                         f"{d['source_uri'] or '-'} | {np} | {nc} |")
        lines.append("\n## 二、Parent（章/条）")
        cur = None
        for p in parents:
            if p["doc_id"] != cur:
                cur = p["doc_id"]
                lines.append(f"\n### doc_id = {cur}")
            flag = "，例外" if p["is_exception"] else ""
            lines.append(f"- `{p['parent_id']}` level={p['level']} tokens={p['token_count']}{flag} "
                         f"path={'/'.join(p['heading_path'] or [])} :: {p['preview']}")
        lines.append("\n## 三、Child 全文")
        cur = None
        for c in chunks:
            if c["doc_id"] != cur:
                cur = c["doc_id"]
                lines.append(f"\n### doc_id = {cur}")
            flag = "，例外条款" if c["is_exception"] else ""
            lines.append(f"\n**[{c['clause_id']}]** parent={c['parent_id']} logical={c['logical_clause_id']} "
                         f"group={c['clause_group_id']} version={c['version']}{flag} "
                         f"tokens={c['token_count']} chars={len(c['text'])} "
                         f"embedding={'NULL' if c['emb_null'] else '有'}")
            lines.append("")
            lines.append("```text")
            lines.append(c["text"])
            lines.append("```")
        (out / "政策切分结果-导出.md").write_text("\n".join(lines), encoding="utf-8")

        with (out / "政策切分结果-导出.csv").open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["doc_id", "clause_id", "parent_id", "logical_clause_id", "clause_group_id",
                        "is_exception", "tokens", "chars", "embedding_model", "text"])
            for c in chunks:
                w.writerow([c["doc_id"], c["clause_id"], c["parent_id"], c["logical_clause_id"],
                            c["clause_group_id"], c["is_exception"], c["token_count"], len(c["text"]),
                            c["embedding_model"] or "", c["text"]])
        print("exported docs=%d parents=%d chunks=%d -> %s" % (len(docs), len(parents), len(chunks), out))
        print("embedding_null:", sum(1 for c in chunks if c["emb_null"]), "/", len(chunks))
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())