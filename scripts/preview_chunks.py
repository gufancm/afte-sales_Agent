# -*- coding: utf-8 -*-
"""本地预览/导出条款切分结果（不连数据库）——用于人工核对与清洗。

用法：
  # 单份
  python scripts/preview_chunks.py --md "E:\\agent\\政策解析结果\\jd\\4268_....docling.md" \
      --json "E:\\agent\\政策解析结果\\jd\\4268_....docling.json" --rule-id 4268 --doc-id jd_4268
  # 整个目录
  python scripts/preview_chunks.py --dir "E:\\agent\\政策解析结果\\jd" --out-dir "E:\\agent\\政策切分预览"

产物：
  <out>/<doc>.chunks.md     人读版（Parent + Child 全文 + token 数）
  <out>/<doc>.chunks.csv    机器版（可直接改 keep/override_is_exception）
  <out>/政策切分复核.csv     汇总复核表（人工清洗入口）
"""
from __future__ import annotations

import argparse
import csv
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.rag.clause_parser import parse_document, load_overrides, cn_to_int  # noqa: E402
from app.rag.parent_child import (get_token_counter, build_parents,  # noqa: E402
                                  build_children_split, build_children_hybrid)


def build_for_doc(md_path: pathlib.Path, json_path: pathlib.Path | None,
                  rule_id: str, doc_id: str, version: str, max_tokens: int,
                  tokenizer: str, use_hybrid: bool, overrides: dict):
    md = md_path.read_text(encoding="utf-8")
    units = parse_document(md, doc_id=doc_id, rule_id=rule_id, version=version, overrides=overrides)
    parents = build_parents(units, doc_id, version)
    count_fn, label = get_token_counter(tokenizer)
    children = None
    note = ""
    if use_hybrid and json_path and json_path.is_file():
        try:
            children, unmatched = build_children_hybrid(
                json_path, parents, max_tokens, count_fn,
                tokenizer if label != "estimate" else "BAAI/bge-m3")
            note = f"hybrid(unmatched={unmatched})"
        except Exception as e:  # noqa: BLE001
            note = f"hybrid失败:{type(e).__name__}"
            children = None
    if children is None:
        children = build_children_split(parents, max_tokens, count_fn)
        note = note or "split(确定性兜底)"
    for p in parents:
        p.token_count = None
    # parent token 数
    for p in parents:
        p.token_count = count_fn(p.display_text)
    return units, parents, children, label, note


def review_rows(doc_id, units, children, count_fn):
    rows = []
    by_parent = {}
    for c in children:
        by_parent.setdefault(c.logical_clause_id, []).append(c)
    for u in units:
        kids = by_parent.get(u.logical_clause_id, [])
        rows.append({
            "doc_id": doc_id, "logical_clause_id": u.logical_clause_id, "level": u.level,
            "is_exception": u.is_exception, "tokens": count_fn(u.text), "chars": len(u.text),
            "child_count": len(kids), "heading_path": " > ".join(u.heading_path),
            "keep": "true", "override_is_exception": "", "note": "",
            "text": u.text.replace("\n", " "),
        })
    return rows


def dump_doc(out_dir: pathlib.Path, doc_id, units, parents, children, label, note):
    out_dir.mkdir(parents=True, exist_ok=True)
    md = [f"# {doc_id} 切分预览", "",
          f"- 条款单元：{len(units)}（章 {sum(1 for u in units if u.level=='CHAPTER')}，"
          f"条/其他 {sum(1 for u in units if u.level=='CLAUSE')}，表 {sum(1 for u in units if u.level=='TABLE')}）",
          f"- Child 数：{len(children)}；tokenizer={label}；切分方式：{note}", ""]
    md.append("## Parent（条/章）")
    for p in sorted(parents, key=lambda x: x.order_index):
        flag = "，例外" if p.is_exception else ""
        md.append(f"- `{p.parent_id}` level={p.level} tokens={p.token_count}{flag} :: {p.display_text[:60]}")
    md.append("\n## Child")
    for c in sorted(children, key=lambda x: (x.order_index, x.clause_id)):
        md.append(f"- `{c.clause_id}` parent={c.parent_id} tokens={c.token_count} :: {c.text[:60]}")
    (out_dir / f"{doc_id}.chunks.md").write_text("\n".join(md), encoding="utf-8")
    with (out_dir / f"{doc_id}.chunks.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["clause_id", "parent_id", "logical_clause_id", "tokens", "chars", "is_exception", "text"])
        for c in children:
            w.writerow([c.clause_id, c.parent_id, c.logical_clause_id, c.token_count, len(c.text),
                        c.is_exception, c.text])


def run_one(out_dir, md_path, json_path, rule_id, doc_id, version, max_tokens,
            tokenizer, use_hybrid, overrides_path, review_acc):
    ov = load_overrides(overrides_path, doc_id) if overrides_path else {}
    units, parents, children, label, note = build_for_doc(
        md_path, json_path, rule_id, doc_id, version, max_tokens, tokenizer, use_hybrid, ov)
    count_fn, _ = get_token_counter(tokenizer)
    dump_doc(out_dir, doc_id, units, parents, children, label, note)
    review_acc.extend(review_rows(doc_id, units, children, count_fn))
    print(f"[{doc_id}] units={len(units)} parents={len(parents)} children={len(children)} ({note})")


def main() -> int:
    ap = argparse.ArgumentParser(description="条款切分本地预览/导出（不连库）")
    ap.add_argument("--md")
    ap.add_argument("--json")
    ap.add_argument("--dir")
    ap.add_argument("--rule-id", default="")
    ap.add_argument("--doc-id", default="")
    ap.add_argument("--version", default="v1")
    ap.add_argument("--max-tokens", type=int, default=512)
    ap.add_argument("--tokenizer", default="BAAI/bge-m3")
    ap.add_argument("--no-hybrid", action="store_true")
    ap.add_argument("--out-dir", default=r"E:\agent\政策切分预览")
    ap.add_argument("--overrides", default=None)
    a = ap.parse_args()

    out_dir = pathlib.Path(a.out_dir)
    review_acc: list[dict] = []
    if a.dir:
        base = pathlib.Path(a.dir)
        for md in sorted(base.glob("*.docling.md")):
            rid = md.name.split("_", 1)[0]
            did = a.doc_id or f"jd_{rid}"
            js = md.with_name(md.name.replace(".docling.md", ".docling.json"))
            run_one(out_dir, md, js if js.is_file() else None, rid, did, a.version,
                    a.max_tokens, a.tokenizer, not a.no_hybrid, a.overrides, review_acc)
    elif a.md:
        md = pathlib.Path(a.md)
        rid = a.rule_id or md.name.split("_", 1)[0]
        did = a.doc_id or f"jd_{rid}"
        js = pathlib.Path(a.json) if a.json else md.with_name(md.name.replace(".docling.md", ".docling.json"))
        run_one(out_dir, md, js if js.is_file() else None, rid, did, a.version,
                a.max_tokens, a.tokenizer, not a.no_hybrid, a.overrides, review_acc)
    else:
        print("需要 --md 或 --dir")
        return 2

    if review_acc:
        rep = out_dir / "政策切分复核.csv"
        with rep.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(review_acc[0].keys()))
            w.writeheader()
            w.writerows(review_acc)
        print(f"复核表：{rep}（改 keep=false 或 override_is_exception 后由人工清洗回到 clause_overrides.json）")
    print(f"完成 -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())