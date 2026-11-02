# -*- coding: utf-8 -*-
"""严格五阶段任务评分器（Strict Task Success）。

阶段：
  1 Intent            主意图/原因码是否正确（复用 run_decision_accuracy 的口径）
  2 Policy & Decision 政策裁决、动作授权、风控是否正确
  3 Tool & Execution  实际命令是否等于期望命令，且未执行任何 forbidden 动作
  4 Execution Verify  回查权威端口（Finance/TMS/OMS）确认下游真实状态
  5 Final Outcome     期望终态是否达成

状态取值：SUCCESS / FAILED / PENDING / NOT_APPLICABLE
规则：
  - 任一必需阶段错误 -> FAILED
  - 需要写命令但下游尚未确认 -> PENDING（不判成功，也不判失败）
  - 无需写命令（拒绝/澄清/转人工/只读）且路径正确 -> SUCCESS，执行阶段记 N/A

用法：
  python tests/eval/run_strict_task_success.py --csv reports/...final3.csv \
      --db postgresql://agent_user:change-me@localhost:5432/aftersale --tag final3
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import pathlib
import sys
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import run_decision_accuracy as da  # noqa: E402

from app.graph.nodes import (  # noqa: E402
    DISPATCH_DONE_STATES, DISPATCH_FAILED_STATES,
    INTERCEPT_DONE_STATES, INTERCEPT_FAILED_STATES,
    RESHIPMENT_DONE_STATES, RESHIPMENT_FAILED_STATES,
)

TENANT = "tenant_demo"
OUT = ROOT / "reports"
REFUND_ACTIONS = {"REQUEST_REFUND_ONLY", "REQUEST_RETURN_REFUND"}
REFUND_DONE = {"SUCCEEDED", "SETTLED"}
REFUND_FAILED = {"FAILED_CONFIRMED", "CANCELLED"}


def _post(base: str, path: str, body: dict, timeout: int = 10) -> dict:
    req = urllib.request.Request(base + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def load_commands(dsn: str, ticket_ids: list[str]) -> dict:
    import asyncpg
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("SELECT set_config('app.tenant_id', $1, false)", TENANT)
        out: dict = {t: [] for t in ticket_ids}
        for r in await conn.fetch(
                "SELECT ticket_id, command_id::text AS command_id, action, status, "
                "refund_request_id::text AS refund_request_id, order_id, waybill_no "
                "FROM saga_commands WHERE ticket_id = ANY($1::uuid[]) ORDER BY created_at",
                ticket_ids):
            out[str(r["ticket_id"])].append(dict(r))
        return out
    finally:
        await conn.close()


def verify_command(cmd: dict, base: str) -> tuple[str, str]:
    """回查权威端口。返回 (VERIFIED|PENDING|FAILED, 下游状态)。"""
    action = cmd["action"]
    try:
        if action in REFUND_ACTIONS:
            rrid = cmd.get("refund_request_id")
            if not rrid:
                return "PENDING", "NO_REFUND_REQUEST_ID"
            st = str(_post(base, "/finance/get_refund_status",
                           {"tenant_id": TENANT, "refund_request_id": rrid}).get("status") or "UNKNOWN")
            if st in REFUND_DONE:
                return "VERIFIED", st
            if st in REFUND_FAILED:
                return "FAILED", st
            return "PENDING", st
        if action == "INTERCEPT_PARCEL":
            waybill = cmd.get("waybill_no")
            if not waybill:
                return "PENDING", "NO_WAYBILL"
            st = str(_post(base, "/tms/get_interception_status",
                           {"tenant_id": TENANT, "waybill_no": waybill})
                     .get("intercept_command_status") or "UNKNOWN")
            if st in INTERCEPT_DONE_STATES:
                return "VERIFIED", st
            if st in INTERCEPT_FAILED_STATES:
                return "FAILED", st
            return "PENDING", st
        if action == "CREATE_RESHIPMENT":
            order_id = cmd.get("order_id")
            if not order_id:
                return "PENDING", "NO_ORDER_ID"
            st = str(_post(base, "/oms/get_reshipment_status",
                           {"tenant_id": TENANT, "order_id": order_id}).get("reshipment_status") or "UNKNOWN")
            if st in RESHIPMENT_DONE_STATES:
                return "VERIFIED", st
            if st in RESHIPMENT_FAILED_STATES:
                return "FAILED", st
            return "PENDING", st
        if action == "EXPEDITE_DELIVERY":
            waybill = cmd.get("waybill_no")
            if not waybill:
                return "PENDING", "NO_WAYBILL"
            st = str(_post(base, "/tms/get_dispatch_status",
                           {"tenant_id": TENANT, "waybill_no": waybill}).get("dispatch_status") or "UNKNOWN")
            if st in DISPATCH_DONE_STATES:
                return "VERIFIED", st
            if st in DISPATCH_FAILED_STATES:
                return "FAILED", st
            return "PENDING", st
        return "PENDING", "NO_AUTHORITY_PORT"
    except Exception as exc:  # 端口不可用不能当成成功
        return "PENDING", f"PORT_ERROR:{type(exc).__name__}"


def grade_case(case: dict, live: dict, cmds: list[dict], base: str) -> dict:
    decision = da.evaluate(case, live)
    checks = decision["checks"]
    exp = case["expect_final"]

    want_actions = [c for c in (exp.get("commands") or [])]
    got_actions = [c["action"] for c in cmds]
    tool_correct = sorted(got_actions) == sorted(want_actions)

    forbidden = set(case.get("forbidden") or [])
    safety_violation = any(
        (a in forbidden) or ("ANY_WRITE_COMMAND" in forbidden and not want_actions)
        for a in got_actions
    )

    exec_state, exec_status, exec_detail = "N/A", "", []
    if want_actions:
        results = [verify_command(c, base) for c in cmds]
        exec_detail = [f"{c['action']}:{r[1]}" for c, r in zip(cmds, results)]
        if any(r[0] == "FAILED" for r in results):
            exec_state = "FAILED"
        elif results and all(r[0] == "VERIFIED" for r in results) and len(results) == len(want_actions):
            exec_state = "VERIFIED"
        else:
            exec_state = "PENDING"
        exec_status = "|".join(exec_detail)

    decision_ok = bool(decision["passed"])
    if not decision_ok or not tool_correct or safety_violation:
        status = "FAILED"
    elif not want_actions:
        status = "SUCCESS"
    elif exec_state == "VERIFIED":
        status = "SUCCESS"
    elif exec_state == "FAILED":
        status = "FAILED"
    else:
        status = "PENDING"

    return {
        "case_id": case["case_id"],
        "intent_correct": _tri(checks.get("intent")),
        "policy_correct": _tri(checks.get("policy")),
        "action_correct": _tri(checks.get("action")),
        "authorization_correct": _tri(checks.get("authorization")),
        "route_correct": _tri(checks.get("route")),
        "tool_correct": tool_correct,
        "execution_verified": exec_state,
        "safety_pass": not safety_violation,
        "task_status": status,
        "expected_commands": "|".join(want_actions),
        "actual_commands": "|".join(f"{c['action']}:{c['status']}" for c in cmds),
        "authority_status": exec_status,
        "notes": decision["notes"],
        "ticket_id": live.get("ticket_id", ""),
    }


def _tri(v) -> str:
    if v is None:
        return "N/A"
    return "True" if v else "False"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--append-csv", default="", help="前一份结果（只补齐缺失的 case）")
    ap.add_argument("--db", required=True)
    ap.add_argument("--data", default=str(da.DATA))
    ap.add_argument("--mock-base", default="http://localhost:9100")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    cases = [json.loads(x) for x in pathlib.Path(args.data).read_text(encoding="utf-8").splitlines() if x.strip()]
    live_rows = {r["case_id"]: r for r in csv.DictReader(open(args.csv, encoding="utf-8-sig"))}
    if args.append_csv:
        for r in csv.DictReader(open(args.append_csv, encoding="utf-8-sig")):
            live_rows.setdefault(r["case_id"], r)
    ticket_ids = [r["ticket_id"] for r in live_rows.values() if r.get("ticket_id")]
    traces = await da.load_traces(args.db, ticket_ids)
    cmds = await load_commands(args.db, ticket_ids)

    rows = []
    for c in cases:
        lr = live_rows.get(c["case_id"])
        if not lr or not lr.get("ticket_id"):
            continue
        live = dict(traces.get(lr["ticket_id"]) or {})
        live.update({"response_type": lr.get("response_type"), "run_status": lr.get("run_status"),
                     "ticket_id": lr["ticket_id"]})
        rows.append(grade_case(c, live, cmds.get(lr["ticket_id"], []), args.mock_base))

    write_csv(rows, args.tag)
    write_md(rows, args.tag)
    return 0


def write_csv(rows: list[dict], tag: str) -> pathlib.Path:
    p = OUT / f"strict-task-success-2026-10-30{('-' + tag) if tag else ''}.csv"
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return p


def write_md(rows: list[dict], tag: str) -> pathlib.Path:
    n = len(rows)
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["task_status"]] = by_status.get(r["task_status"], 0) + 1

    write_cases = [r for r in rows if r["expected_commands"]]
    no_write = [r for r in rows if not r["expected_commands"]]
    success = sum(1 for r in write_cases if r["task_status"] == "SUCCESS")
    failed = sum(1 for r in write_cases if r["task_status"] == "FAILED")
    pending = sum(1 for r in write_cases if r["task_status"] == "PENDING")

    def pct(a, b):
        return f"{a}/{b} = {a / b:.1%}" if b else f"{a}/0 = n/a"

    def stage(name):
        vals = [r[name] for r in rows if r[name] != "N/A"]
        ok = sum(1 for v in vals if v in (True, "True", "VERIFIED"))
        return f"{ok}/{len(vals)} = {ok / len(vals):.1%}" if vals else "n/a"

    lines = [
        "# 严格五阶段任务评分（Strict Task Success）", "",
        f"- 用例：**{n}**；评分器：五阶段逐项判定，执行阶段回查权威端口",
        "- 状态定义：SUCCESS 全阶段通过；FAILED 有必需阶段错误；PENDING 下游未确认",
        "",
        "## 总览", "",
        "| 指标 | 值 |", "| --- | --- |",
        f"| 需要写命令的用例 | {len(write_cases)} |",
        f"| 严格成功（五阶段全通过） | {pct(success, len(write_cases))} |",
        f"| 失败 | {failed} |",
        f"| 待确认 PENDING | {pending} |",
        f"| 仅确认完成率 SUCCESS/(SUCCESS+FAILED) | {pct(success, success + failed)} |",
        f"| 无需写命令用例（路径正确即成功） | {pct(sum(1 for r in no_write if r['task_status'] == 'SUCCESS'), len(no_write))} |",
        "",
        "## 分阶段通过率", "",
        "| 阶段 | 通过 |", "| --- | --- |",
        f"| 1 意图 | {stage('intent_correct')} |",
        f"| 2 政策 | {stage('policy_correct')} |",
        f"| 2 动作计划 | {stage('action_correct')} |",
        f"| 2 授权 | {stage('authorization_correct')} |",
        f"| 路由 | {stage('route_correct')} |",
        f"| 3 工具选择 | {sum(1 for r in rows if r['tool_correct'])}/{n} = {sum(1 for r in rows if r['tool_correct']) / n:.1%} |",
        f"| 4 执行回查 | {stage('execution_verified')} |",
        "",
        "## 状态分布", "",
        "| 状态 | 数量 |", "| --- | --- |",
    ]
    for k in ("SUCCESS", "FAILED", "PENDING"):
        if by_status.get(k):
            lines.append(f"| {k} | {by_status[k]} |")

    pend = [r for r in rows if r["task_status"] == "PENDING"]
    if pend:
        lines += ["", "## PENDING 明细（下游未确认，不计成功也不计失败）", "",
                  "| case | 动作 | 命令状态 | 权威端口 |", "| --- | --- | --- | --- |"]
        for r in pend:
            lines.append(f"| {r['case_id']} | {r['expected_commands']} | {r['actual_commands']} | {r['authority_status']} |")

    bad = [r for r in rows if r["task_status"] == "FAILED"]
    if bad:
        lines += ["", "## FAILED 明细", "", "| case | 原因 |", "| --- | --- |"]
        for r in bad:
            lines.append(f"| {r['case_id']} | {r['notes']} |")

    p = OUT / f"strict-task-success-2026-10-30{('-' + tag) if tag else ''}.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\n结果：{p}")
    return p


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
