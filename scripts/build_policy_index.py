# -*- coding: utf-8 -*-
"""构建 Parent-Child 政策索引：解析产物 -> 条款单元 -> Parent/Child -> （可选）写库。

默认 dry-run（只统计 + 生成复核表，不连库）。--apply 才写 policy_documents/policy_parents/policy_chunks
并生成 policy_rule_clause_map 的待解析占位。

用法：
  python scripts/build_policy_index.py --dir "E:\\agent\\政策解析结果\\jd" --out-dir "E:\\agent\\政策切分预览"
  python scripts/build_policy_index.py --dir ... --apply --database-url postgresql://aftersale_app:app_pw_change_me@localhost:5432/aftersale
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.rag.clause_parser import parse_document, load_overrides  # noqa: E402
from app.rag.tokenize import token_counts  # noqa: E402
from app.rag.parent_child import (get_token_counter, build_parents,  # noqa: E402
                                  build_children_split, build_children_hybrid)

APP_DSN = "postgresql://aftersale_app:app_pw_change_me@localhost:5432/aftersale"


def chapter_group_map(units) -> dict:
    """条 -> 所属章 logical_clause_id（clause_group_id），用于例外闭包。"""
    cur = None
    out = {}
    for u in units:
        if u.level == "CHAPTER":
            cur = u.logical_clause_id
        out[u.logical_clause_id] = cur
    return out


def build_doc(md_path, json_path, doc_id, rule_id, version, max_tokens, tokenizer,
              use_hybrid, overrides_path):
    md = md_path.read_text(encoding="utf-8")
    ov = load_overrides(overrides_path, doc_id) if overrides_path else {}
    units = parse_document(md, doc_id=doc_id, rule_id=rule_id, version=version, overrides=ov)
    parents = build_parents(units, doc_id, version)
    count_fn, label = get_token_counter(tokenizer)
    children, note = None, ""
    if use_hybrid and json_path and pathlib.Path(json_path).is_file():
        try:
            children, unmatched = build_children_hybrid(
                json_path, parents, max_tokens, count_fn,
                tokenizer if label != "estimate" else "BAAI/bge-m3")
            note = f"hybrid(unmatched={unmatched})"
        except Exception as e:  # noqa: BLE001
            note = f"hybrid失败:{type(e).__name__}->兜底"
            children = None
    if children is None:
        children = build_children_split(parents, max_tokens, count_fn)
        note = note or "split"
    for p in parents:
        p.token_count = count_fn(p.display_text)
    return units, parents, children, label, note


async def apply_to_db(dsn, tenant, docs_data, rules_path):
    import asyncpg
    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://", 1))
    try:
        await conn.execute("SELECT set_config('app.tenant_id',$1,false)", tenant)
        for d in docs_data:
            doc_id, title, version = d["doc_id"], d["title"], d["version"]
            await conn.execute(
                "INSERT INTO policy_documents(doc_id,tenant_id,title,version,status,effective_from,source_uri) "
                "VALUES($1,$2,$3,$4,'PUBLISHED',$5,$6) "
                "ON CONFLICT (doc_id) DO UPDATE SET title=EXCLUDED.title, version=EXCLUDED.version, "
                "source_uri=EXCLUDED.source_uri",
                doc_id, tenant, title, version, d.get("effective_from"), d.get("source_uri"))
            await conn.execute("DELETE FROM policy_chunks WHERE tenant_id=$1 AND doc_id=$2 AND version=$3",
                               tenant, doc_id, version)
            await conn.execute("DELETE FROM policy_parents WHERE tenant_id=$1 AND doc_id=$2 AND version=$3",
                               tenant, doc_id, version)
            for p in d["parents"]:
                await conn.execute(
                    "INSERT INTO policy_parents(parent_id,tenant_id,doc_id,version,level,logical_clause_id,"
                    "heading_path,order_index,is_exception,display_text,token_count) "
                    "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) ON CONFLICT (tenant_id,parent_id) DO NOTHING",
                    p.parent_id, tenant, doc_id, version, p.level, p.logical_clause_id,
                    p.heading_path, p.order_index, p.is_exception, p.display_text, p.token_count)
            gmap = d["group_map"]
            for c in d["children"]:
                tc = token_counts(c.text or "")
                await conn.execute(
                    "INSERT INTO policy_chunks(tenant_id,doc_id,version,clause_id,clause_group_id,is_exception,"
                    "text,parent_id,logical_clause_id,heading_path,token_count,tokens,doc_len) "
                    "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11, CAST($12 AS jsonb), $13) "
                    "ON CONFLICT (tenant_id,clause_id,version) DO UPDATE SET text=EXCLUDED.text, "
                    "parent_id=EXCLUDED.parent_id, logical_clause_id=EXCLUDED.logical_clause_id, "
                    "heading_path=EXCLUDED.heading_path, token_count=EXCLUDED.token_count, "
                    "tokens=EXCLUDED.tokens, doc_len=EXCLUDED.doc_len",
                    tenant, doc_id, version, c.clause_id, gmap.get(c.logical_clause_id),
                    c.is_exception, c.text, c.parent_id, c.logical_clause_id,
                    [p for p in ([gmap.get(c.logical_clause_id)] if gmap.get(c.logical_clause_id) else [])],
                    c.token_count, json.dumps(tc, ensure_ascii=False), sum(tc.values()))
        # 判定层映射占位（UNRESOLVED，待人工/发布期解析为 parent_id）
        rp = pathlib.Path(rules_path)
        if rp.is_file():
            rules = json.loads(rp.read_text(encoding="utf-8")).get("rules", [])
            ver = json.loads(rp.read_text(encoding="utf-8")).get("version", "v1")
            for r in rules:
                for cid in r.get("required_clause_ids", []):
                    await conn.execute(
                        "INSERT INTO policy_rule_clause_map(tenant_id,rule_id,logical_clause_id,version,status) "
                        "VALUES($1,$2,$3,$4,'UNRESOLVED') ON CONFLICT DO NOTHING",
                        tenant, r["rule_id"], cid, ver)
    finally:
        await conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="构建 Parent-Child 政策索引")
    ap.add_argument("--dir", required=True, help="解析产物目录（*.docling.md/.json）")
    ap.add_argument("--out-dir", default=r"E:\agent\政策切分预览")
    ap.add_argument("--tenant", default="tenant_demo")
    ap.add_argument("--version", default="v1")
    ap.add_argument("--max-tokens", type=int, default=512)
    ap.add_argument("--tokenizer", default="BAAI/bge-m3")
    ap.add_argument("--no-hybrid", action="store_true")
    ap.add_argument("--overrides", default=None)
    ap.add_argument("--emit-review", action="store_true", default=True)
    ap.add_argument("--rules", default=str(ROOT / "policy" / "rules.json"))
    ap.add_argument("--apply", action="store_true", help="写库（默认 dry-run）")
    ap.add_argument("--database-url", default=None)
    a = ap.parse_args()

    base = pathlib.Path(a.dir)
    out_dir = pathlib.Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    docs_data, review = [], []
    for md in sorted(base.glob("*.docling.md")):
        rid = md.name.split("_", 1)[0]
        doc_id = f"jd_{rid}"
        title = md.stem.split("_", 1)[1].replace(".docling", "") if "_" in md.stem else md.stem
        js = md.with_name(md.name.replace(".docling.md", ".docling.json"))
        units, parents, children, label, note = build_doc(
            md, js if js.is_file() else None, doc_id, rid, a.version,
            a.max_tokens, a.tokenizer, not a.no_hybrid, a.overrides)
        gmap = chapter_group_map(units)
        docs_data.append({"doc_id": doc_id, "title": title, "version": a.version,
                          "parents": parents, "children": children, "group_map": gmap})
        cnt, _ = get_token_counter(a.tokenizer)
        for u in units:
            review.append({"doc_id": doc_id, "logical_clause_id": u.logical_clause_id, "level": u.level,
                           "is_exception": u.is_exception, "tokens": cnt(u.text), "chars": len(u.text),
                           "heading_path": " > ".join(u.heading_path), "keep": "true",
                           "override_is_exception": "", "note": "", "text": u.text.replace("\n", " ")})
        print(f"[{doc_id}] units={len(units)} parents={len(parents)} children={len(children)} ({note})")

    if review:
        rep = out_dir / "政策切分复核.csv"
        with rep.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(review[0].keys()))
            w.writeheader()
            w.writerows(review)
        print(f"复核表：{rep}")

    total_children = sum(len(d["children"]) for d in docs_data)
    print(f"合计：docs={len(docs_data)} parents={sum(len(d['parents']) for d in docs_data)} children={total_children}")

    if a.apply:
        asyncio.run(apply_to_db(a.database_url or APP_DSN, a.tenant, docs_data, a.rules))
        print("已写库（policy_documents / policy_parents / policy_chunks / policy_rule_clause_map）")
    else:
        print("dry-run：未写库。加 --apply 写库。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())