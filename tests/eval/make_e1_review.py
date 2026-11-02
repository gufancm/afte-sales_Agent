# -*- coding: utf-8 -*-
import io, json, pathlib, csv

HERE = pathlib.Path("tests/eval")
DATA = HERE / "data"
OUT = pathlib.Path("reports/e1-review.csv")

GROUPS = [("intent_safety.jsonl", "安全正样本"),
          ("intent_safety_negative.jsonl", "安全负样本"),
          ("intent_multi.jsonl", "多意图"),
          ("intent_golden.jsonl", "单意图")]
RANK = {"高": 0, "中": 1, "低": 2}
ORDER = [g for _, g in GROUPS]

def load(name):
    p = DATA / name
    if not p.is_file():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]

def severity(group, e):
    if group in ("安全正样本", "安全负样本", "多意图"):
        return "高"
    if e["safety_flags"]:
        return "高"
    if e["intent"] == "UNKNOWN":
        return "高"
    if e["secondary_intents"] or e["intent"] in ("COMPLAINT", "INTERCEPT_REFUND"):
        return "中"
    return "低"

rows = []
for fname, group in GROUPS:
    for c in load(fname):
        e = c["expect"]
        rows.append((group, severity(group, e), c["text"], e["intent"],
                     "/".join(e["secondary_intents"]), "/".join(e["safety_flags"]) or "",
                     c.get("review", "pending"), c.get("note", "")))
rows.sort(key=lambda r: (RANK[r[1]], ORDER.index(r[0])))

with io.open(OUT, "w", encoding="utf-8-sig", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["序号", "分组", "疑点", "文本", "期望主意图", "期望次意图", "期望旗标",
                "判定-意图(确认/改:X)", "判定-旗标(确认/改:X)", "备注"])
    for i, r in enumerate(rows, 1):
        w.writerow([i, r[0], r[1], r[2], r[3], r[4], r[5], "", "", r[7]])
print("wrote", OUT, len(rows), "rows")

from collections import Counter
print(Counter((r[0], r[1]) for r in rows))
print("pending:", sum(1 for r in rows if r[6] != "confirmed"))