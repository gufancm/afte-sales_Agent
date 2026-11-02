# -*- coding: utf-8 -*-
"""统一 Task Success 判据（v2.4 口径），用已保存的 live 结果离线重算，不重跑模型。

判据（与 Decision Accuracy 明确分工）：
  1. 终态：response_type / run_status 与“按真实诉求推导的期望终态”一致；
  2. 写动作：实际写命令集合必须落在金标命令集合内（禁止金标外写动作）；
  3. 审批：金标要求审批 <=> 实际出现审批（取 decision-accuracy 的 authorization 维度）；
  4. 业务完成：business_expect.applicable 的用例必须到达下游终态。

意图标签本身**不计入** Task Success：
  意图错但诉求处理正确 -> 仍算任务成功；
  意图错导致终态/命令/审批错误 -> 才算任务失败。

用法：
  python tests/eval/recompute_task_success.py \
    --task-csv reports/task-success-live-2026-10-29.csv \
    --decision-csv reports/decision-accuracy-2026-10-29-v2.4.csv \
    --data tests/eval/data/frozen/task_success_v2.v2.4.jsonl \
    --tag v2.4
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = ROOT / "reports"


def _cmds(text: str) -> set[str]:
    out: set[str] = set()
    for part in (text or "").replace(";", ",").replace("|", ",").split(","):
        part = part.strip()
        if part:
            out.add(part.split(":")[0].strip())
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-csv", required=True)
    ap.add_argument("--decision-csv", default="")
    ap.add_argument("--data", required=True)
    ap.add_argument("--tag", default="")
    a = ap.parse_args()

    cases = [json.loads(x) for x in pathlib.Path(a.data).read_text(encoding="utf-8").splitlines() if x.strip()]
    live = {r["case_id"]: r for r in csv.DictReader(open(a.task_csv, encoding="utf-8-sig"))}
    dec = {}
    if a.decision_csv:
        dec = {r["case_id"]: r for r in csv.DictReader(open(a.decision_csv, encoding="utf-8-sig"))}

    rows = []
    for c in cases:
        cid = c["case_id"]
        row = live.get(cid)
        if not row or row.get("workflow") == "SKIP":
            continue
        e = c["expect_final"]
        want_types = e.get("response_type_any_of") or [e.get("response_type")]
        want_status = e.get("run_status_any_of") or [e.get("run_status")]
        route_ok = row.get("response_type") in want_types and row.get("run_status") in want_status

        want_cmds = set(e.get("commands") or [])
        got_cmds = _cmds(row.get("commands"))
        cmd_ok = got_cmds <= want_cmds

        d = dec.get(cid) or {}
        auth = d.get("authorization", "")
        approval_ok = auth != "NG"

        biz_applicable = bool((e.get("business_expect") or {}).get("applicable"))
        business_ok = (row.get("business") == "PASS") if biz_applicable else True

        passed = route_ok and cmd_ok and approval_ok and business_ok
        rows.append({
            "case_id": cid, "group": c["group"], "workflow": "PASS" if passed else "FAIL",
            "route_ok": route_ok, "cmd_ok": cmd_ok, "approval_ok": approval_ok,
            "business_applicable": biz_applicable, "business_ok": business_ok,
            "response_type": row.get("response_type"), "run_status": row.get("run_status"),
            "want": f"{want_types}/{want_status}",
            "commands": sorted(got_cmds), "want_commands": sorted(want_cmds),
            "intent_expected": d.get("intent_expected", ""), "intent_actual": d.get("intent_actual", ""),
        })

    total = len(rows)
    passed = sum(r["workflow"] == "PASS" for r in rows)
    stamp = datetime.date.today().isoformat()
    suffix = f"-{a.tag}" if a.tag else ""

    intent_mismatch = [r for r in rows if r["intent_expected"] and r["intent_expected"] != r["intent_actual"]]
    caused = [r for r in intent_mismatch if r["workflow"] == "FAIL"]

    lines = [f"# Task Success 重算（统一判据，{stamp}）", "",
             f"- 金标：`{pathlib.Path(a.data).name}`",
             f"- 轨迹：`{pathlib.Path(a.task_csv).name}`（不重跑模型）",
             f"- 判据：终态 + 写动作合规 + 审批合规 + 业务终态；**意图标签不计入**", "",
             "| 指标 | 结果 |", "| --- | --- |",
             f"| **Workflow Handling Success** | **{passed}/{total} = {passed / total:.1%}** |",
             f"| 意图标签不一致 | {len(intent_mismatch)} 条 |",
             f"| 其中造成诉求处理错误 | {len(caused)} 条 |", "",
             "## 意图标签不一致的逐条影响", "",
             "| 用例 | 期望意图 | 实际意图 | 终态 | 诉求处理 |", "| --- | --- | --- | --- | --- |"]
    for r in intent_mismatch:
        lines.append(f"| {r['case_id']} | {r['intent_expected']} | {r['intent_actual']} | "
                     f"{r['response_type']}/{r['run_status']} | {'错误（终态不符）' if r['workflow'] == 'FAIL' else '正确（终态符合诉求）'} |")
    fails = [r for r in rows if r["workflow"] == "FAIL"]
    lines += ["", f"## 失败用例（{len(fails)}）", ""]
    for r in fails:
        bad = []
        if not r["route_ok"]:
            bad.append(f"终态 {r['response_type']}/{r['run_status']} != {r['want']}")
        if not r["cmd_ok"]:
            bad.append(f"命令 {r['commands']} ⊄ {r['want_commands']}")
        if not r["approval_ok"]:
            bad.append("审批要求不符")
        if not r["business_ok"]:
            bad.append("业务终态未达成")
        lines.append(f"- `{r['case_id']}`（{r['group']}）：" + "；".join(bad))
    lines += ["", "## 口径说明", "",
              "- 本指标只看“用户诉求是否被正确处理”，不看内部意图标签；因此 TS-008（意图标签不同但终态正确）计为成功。",
              "- 与 Decision Accuracy 分开报告：后者衡量五维推理链，前者衡量业务结果。",
              "- `PROCESSING` 仅代表命令已受理，不等于业务完成（业务完成率单列）。"]
    (OUT / f"task-success-recompute-{stamp}{suffix}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT / f"task-success-recompute-{stamp}{suffix}.json").write_text(json.dumps(
        {"date": stamp, "data": pathlib.Path(a.data).name, "total": total, "passed": passed,
         "rate": round(passed / total, 4), "intent_mismatch": len(intent_mismatch),
         "intent_mismatch_caused_failure": len(caused), "rows": rows},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
