# -*- coding: utf-8 -*-
"""生成人工核对表：只保留人需要判断的字段，按优先级排序。

产出 reports/task-success-v2-人工核对表.csv
- P1：按历次复核意见改过的用例（changed_vs_v1=yes 或 added_v2_1），需签字确认改得对
- P2：卡在政策 v3 的用例；v2.1 起已移出正式分母，单独存放于
  tests/eval/data/task_success_pending_policy_v3.jsonl，因此本表通常为 0 条
- P3：未改动的用例，抽核用
"""
from __future__ import annotations

import csv
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "reports" / "task-success-review-v2.csv"
OUT = ROOT / "reports" / "task-success-v2-人工核对表.csv"

PRIORITY = {"pending_policy_v3": "P2", }


def priority_of(row: dict) -> str:
    if row["review_status"] == "pending_policy_v3":
        return "P2"
    return "P1" if row["changed_vs_v1"] == "yes" else "P3"


def main() -> None:
    rows = list(csv.DictReader(SRC.open(encoding="utf-8-sig")))
    for row in rows:
        row["_p"] = priority_of(row)
    order = {"P1": 0, "P2": 1, "P3": 2}
    rows.sort(key=lambda r: (order[r["_p"]], r["case_id"]))

    fields = [
        ("优先级", lambda r: r["_p"]),
        ("用例", lambda r: r["case_id"]),
        ("用户话术", lambda r: r["user_text"]),
        ("意图", lambda r: r["intent"]),
        ("请求动作", lambda r: r["requested_action"]),
        ("系统终态", lambda r: f'{r["response_type"]} / {r["gate_reason"] or "-"}'),
        ("政策裁决", lambda r: f'{r["policy_decision"]} / {r["reason_code"]}'),
        ("依据规则", lambda r: r["rule_id"] or "-"),
        ("授权动作", lambda r: r["authorized_actions"] or "-"),
        ("命令", lambda r: r["commands"] or "-"),
        ("幂等断言", lambda r: r.get("duplicate_submission") or "-"),
        ("业务依据", lambda r: r["basis"]),
        ("相对v1改动", lambda r: r["change_note"] or "-"),
        ("你要回答的问题", lambda r: _question(r)),
        ("你的结论(填: 确认/改:X/删)", lambda r: ""),
        ("备注", lambda r: ""),
    ]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([name for name, _ in fields])
        for row in rows:
            writer.writerow([fn(row) for _, fn in fields])

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["_p"]] = counts.get(row["_p"], 0) + 1
    print(f"写入 {OUT} 共 {len(rows)} 行 {counts}")


def _question(row: dict) -> str:
    status = row["review_status"]
    if status == "pending_policy_v3":
        return "需先定规则口径（是否按品类一刀切，还是补事实字段）"
    if row["changed_vs_v1"] == "yes":
        return "改动后的判定是否符合条款原文"
    return "条款是否直接回答了该诉求（抽核）"


if __name__ == "__main__":
    main()
