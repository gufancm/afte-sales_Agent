# -*- coding: utf-8 -*-
"""生成 Policy Engine 120 条金标复核表（Excel 可直接打开）。

用法：python tests/eval/make_policy_review.py
输出：
  tests/eval/policy-review-v2.csv   （UTF-8 BOM，Excel 打开不乱码）
  tests/eval/POLICY-REVIEW.md       （分组说明 + 条款原文）
"""
from __future__ import annotations

import csv
import io
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
DATA = HERE / "data" / "policy_cases_v2.jsonl"
CLAUSES = HERE.parents[1] / "policy" / "clauses_jd.json"


def clause_map() -> dict[str, dict]:
    doc = json.loads(CLAUSES.read_text(encoding="utf-8"))
    return {c["clause_id"]: c for c in doc.get("clauses", [])}


def fact_text(f: dict) -> str:
    order, logi, fin, claim = f.get("order", {}), f.get("logistics", {}), f.get("finance", {}), f.get("claim", {})
    parts = [
        f"品类={order.get('category')}",
        f"订单状态={order.get('status')}",
        f"物流={logi.get('parcel_status')}",
        f"签收={order.get('delivered_at') or '缺失'}",
        f"申请={claim.get('claim_at') or '缺失'}",
        f"原因={claim.get('reason_code') or '缺失'}",
    ]
    if fin.get("requested_amount_minor") is not None:
        parts.append(f"申请金额={fin['requested_amount_minor']}分")
    else:
        parts.append("申请金额=缺失")
    if claim.get("quantity") is not None:
        parts.append(f"数量={claim['quantity']}")
    return "；".join(parts)


def main() -> int:
    cases = [json.loads(l) for l in DATA.read_text(encoding="utf-8").splitlines() if l.strip()]
    cmap = clause_map()

    csv_path = HERE / "policy-review-v2.csv"
    with io.open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["序号", "用例ID", "分组", "意图", "事实组合", "期望裁决", "期望原因码",
                    "命中规则", "条款ID", "条款原文", "用例说明", "复核结论"])
        for i, c in enumerate(cases, 1):
            ids = c["expect"]["required_clause_ids"]
            texts = []
            for cid in ids:
                cl = cmap.get(cid)
                texts.append(f"[{cid}] {cl['text'].strip() if cl else '（条款库未找到）'}")
            w.writerow([i, c["case_id"], c["group"], c["intent"], fact_text(c["facts"]),
                        c["expect"]["decision"], c["expect"]["reason_code"], c["rule_ref"] or "-",
                        " / ".join(ids) or "-", "\n".join(texts) or "-", c.get("note", ""), ""])

    lines = ["# Policy Engine 120 条金标复核表", "",
             f"- 用例文件：`tests/eval/data/policy_cases_v2.jsonl`（{len(cases)} 条）",
             "- 复核方式：逐条确认「期望裁决 / 期望原因码 / 条款原文」是否与真实政策一致；",
             "- 结论填 `确认` 或 `改:<正确期望>`，改完同步修改 jsonl 的 `expect` 并把 `review` 置为 `confirmed`。", "",
             "## 分布", "", "| 分组 | 数量 |", "| --- | --- |"]
    groups: dict[str, int] = {}
    for c in cases:
        groups[c["group"]] = groups.get(c["group"], 0) + 1
    for g, n in groups.items():
        lines.append(f"| {g} | {n} |")

    for g in groups:
        lines += ["", f"## {g}", "", "| 用例 | 事实组合 | 期望 | 规则 | 条款 |", "| --- | --- | --- | --- | --- |"]
        for c in [x for x in cases if x["group"] == g]:
            ids = c["expect"]["required_clause_ids"]
            lines.append("| %s | %s | %s / `%s` | `%s` | %s |" % (
                c["case_id"], fact_text(c["facts"]), c["expect"]["decision"],
                c["expect"]["reason_code"], c["rule_ref"] or "-",
                "、".join(f"`{i}`" for i in ids) or "-"))
    (HERE / "POLICY-REVIEW.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("已生成：", csv_path)
    print("已生成：", HERE / "POLICY-REVIEW.md")
    missing = [cid for c in cases for cid in c["expect"]["required_clause_ids"] if cid not in cmap]
    if missing:
        print("警告：条款库缺失", sorted(set(missing)))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())