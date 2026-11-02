# -*- coding: utf-8 -*-
"""生成 E8 检索金标复核表（CSV + Markdown）。

若已跑过 run_retrieval.py，则带入各配置的首命中位次，便于人工核对 gold 是否合理。
Markdown 版把 gold 条款原文一并展开，避免复核时来回翻条款库。
"""
import argparse, csv, io, json, pathlib
from collections import Counter

HERE = pathlib.Path("tests/eval")
DATA = HERE / "data" / "retrieval_queries.jsonl"
REPORTS = pathlib.Path("reports")
OUT_CSV = REPORTS / "e8-retrieval-review.csv"
OUT_MD = REPORTS / "e8-retrieval-review.md"

rows = [json.loads(l) for l in DATA.read_text(encoding="utf-8").splitlines() if l.strip()]
cl = json.loads(pathlib.Path("policy/clauses_jd.json").read_text(encoding="utf-8"))
by_id = {c["clause_id"]: c for c in cl["clauses"]}
c2doc = {c["clause_id"]: c["doc_id"] for c in cl["clauses"]}
titles = {d["doc_id"]: d["title"] for d in cl["documents"]}

_ap = argparse.ArgumentParser(description="生成 E8 金标复核表")
_ap.add_argument("--detail", default="", help="指定带位次的明细 JSON；默认取 reports/e8-retrieval-*.json 最新一份")
_a, _ = _ap.parse_known_args()

detail = {}
if _a.detail:
    data = json.loads(pathlib.Path(_a.detail).read_text(encoding="utf-8"))
else:
    js = sorted(REPORTS.glob("e8-retrieval-*.json"))
    data = json.loads(js[-1].read_text(encoding="utf-8")) if js else {}
detail = {d["qid"]: d for d in data.get("detail", [])}
print("rank source:", _a.detail or "latest", "| gold_version:", data.get("gold_version", "v1.0"))


def sev(r, d):
    if r.get("review") != "confirmed":
        return "高"
    if d.get("full_rank") == "miss":
        return "高"
    if r["type"] == "exception":
        return "中"
    return "低"


def clip(text: str, n: int = 90) -> str:
    s = " ".join((text or "").split())
    return s if len(s) <= n else s[:n] + "…"


RANK = {"高": 0, "中": 1, "低": 2}
rows.sort(key=lambda r: (RANK[sev(r, detail.get(r["qid"], {}))], r["qid"]))

with io.open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["序号", "qid", "问句", "类型", "gold条款", "gold条款原文(截断)",
                "gold文档(主题)", "bm25位次", "hybrid位次", "full位次",
                "判定-gold(确认/改:X/删)", "备注"])
    for i, r in enumerate(rows, 1):
        d = detail.get(r["qid"], {})
        docs = ";".join(titles.get(x, x) for x in r["gold_doc_ids"])
        texts = " || ".join(clip(by_id[c]["text"]) for c in r["gold_clause_ids"] if c in by_id)
        w.writerow([i, r["qid"], r["query"], r["type"], ";".join(r["gold_clause_ids"]), texts,
                    docs, d.get("bm25_rank", ""), d.get("hybrid_rank", ""), d.get("full_rank", ""),
                    "", r.get("note", "")])

STD = """# E8 检索金标复核表

## 复核标准（逐条判断，只回答一个问题）

**「只凭这句话，一个熟悉平台规则的人翻到这条条款，能不能回答问题？」**

| 判定 | 含义 | 什么时候用 |
| --- | --- | --- |
| `确认` | gold 条款确实能回答问题，且是最直接的那条 | 默认 |
| `改:CL-XXX` | 有更直接的条款，写条款号替换 | 命中了隔壁条款/上位条款 |
| `删` | 这条问句在现有 17 份 PDF 里根本没有答案 | 属于其他平台/其他文档的规则 |

## 五条具体口径

1. **直接性优先**：既有「总则」又有「具体细则」时，gold 记**细则**。总则能推出来不算。
2. **例外条款单独标**：问句问的是「什么情况下不适用/例外」，gold 必须是带 `is_exception` 的条款，不能记主条款。
3. **一条不够可以多条**：完整回答需要两三条并读时，全部写入（用 `;` 分隔），但最多 3 条。
4. **宁缺毋滥**：条款只是「提到了关键词」但没回答问题，是**错标**，不是「勉强算对」。
5. **不做跨库推理**：需要结合订单/物流事实才能回答的，gold 只记「规则口径」条款；事实不写进 gold。

## 需要你特别留意的三类

- `full位次 = miss`：检索没捞到。要么是 gold 标错，要么是真难 case，两类都要看。
- 类型 `exception`（7 条）：最容易标成主条款。
- 类型 `colloquial`（30 条）：口语化问句，判断标准是「条款能不能覆盖这层意思」，不是字面重合。

"""
lines = [STD, "## 118 条清单（按风险排序：未确认 > 检索未命中 > 例外 > 其余）\n",
         "| 序号 | qid | 问句 | 类型 | gold 条款 | 条款原文（截断） | full 位次 |",
         "| --- | --- | --- | --- | --- | --- | --- |"]
for i, r in enumerate(rows, 1):
    d = detail.get(r["qid"], {})
    texts = " || ".join(f"`{c}` " + clip(by_id[c]["text"], 70) for c in r["gold_clause_ids"] if c in by_id)
    lines.append(f"| {i} | {r['qid']} | {r['query']} | {r['type']} | "
                 f"{';'.join(r['gold_clause_ids'])} | {texts} | {d.get('full_rank', '')} |")
OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

print("wrote", OUT_CSV, len(rows), "rows")
print("wrote", OUT_MD)
print(Counter(r["type"] for r in rows))