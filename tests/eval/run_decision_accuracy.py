# -*- coding: utf-8 -*-
"""Agent Decision Accuracy：用已保存的 live trace 评估完整业务决策，不重跑模型。

五个维度（只对“该场景真正需要作出的决策”判分）：
  1. Intent        用户业务意图是否正确
  2. Policy        政策资格判断（decision + reason_code + rule_id）
  3. Action        动作计划是否等于用户被授权的动作
  4. Authorization 审批要求与命令集合是否符合金标
  5. Route         最终处理路径（response_type + run_status）

严格判分：一条用例中所有**适用**维度全部正确才算通过。
用法：
  python tests/eval/run_decision_accuracy.py --csv reports/task-success-live-2026-10-29.csv \
      --db postgresql://aftersale_app:***@localhost:5432/aftersale
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import csv
import datetime
import io
import json
import pathlib

import asyncpg

ROOT = pathlib.Path(__file__).resolve().parents[2]
DATA = ROOT / "tests" / "eval" / "data" / "frozen" / "task_success_v2.v2.5.jsonl"
OUT = ROOT / "reports"
TENANT = "tenant_demo"

# --- 判分口径 ---------------------------------------------------------------
# 1) 动作授权门禁会把“业务资格 ALLOW、但请求的动作未被规则授权”改写成
#    NEED_INFO/MANUAL + gate 原因；此时政策维度必须看引擎原始 winner。
ACTION_AUTH_GATES = {"ACTION_NOT_AUTHORIZED_BY_POLICY", "NO_AUTOMATABLE_ACTION_FOR_POLICY"}
# 2) 这些 reason 不是政策资格裁决：缺事实 => 政策引擎未运行；
#    金额超自动阈值 => 风险/授权环节的升级，交给 authorization 维度判。
NON_POLICY_VERDICTS = {"MISSING_FACT", "AUTHORITATIVE_FACT_UNAVAILABLE", "AMOUNT_EXCEEDS_AUTO_THRESHOLD"}
RISK_ESCALATIONS = {"AMOUNT_EXCEEDS_AUTO_THRESHOLD"}
# 3) v2.4 已按 E1 口径统一 TS-006/010/015（裸动词“退”→RETURN_REFUND）；
#    TS-008 经复核仍为 REFUND_ONLY（无退货语义），属系统误判，不再单列。
BARE_TUI_GOLD_PENDING: set[str] = set()


def _json(value):
    """asyncpg 默认把 jsonb 当字符串返回。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value or {}


async def load_traces(dsn: str, ticket_ids: list[str]) -> dict:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("SELECT set_config('app.tenant_id', $1, false)", TENANT)
        out: dict = {t: {"policy": None, "plan": None, "intent": None,
                         "approvals": 0, "commands": []} for t in ticket_ids}
        for r in await conn.fetch(
                "SELECT ticket_id,decision,reason_code,rule_ids,trace FROM policy_decisions "
                "WHERE ticket_id = ANY($1::uuid[]) ORDER BY created_at", ticket_ids):
            trace = _json(r["trace"])
            winner = trace.get("winner") or {}
            out[str(r["ticket_id"])]["policy"] = {
                "raw_decision": winner.get("decision") or r["decision"],
                "raw_reason": winner.get("reason_code") or r["reason_code"],
                "raw_rule": winner.get("rule_id"),
                "stored_decision": r["decision"],
                "stored_reason": r["reason_code"],
                "stored_rules": list(r["rule_ids"] or []),
            }
        for r in await conn.fetch(
                "SELECT ticket_id,plan_json FROM action_plans WHERE ticket_id = ANY($1::uuid[]) "
                "ORDER BY created_at", ticket_ids):
            plan_json = _json(r["plan_json"])
            actions = [a.get("action") for a in (plan_json.get("plan") or {}).get("actions") or []]
            out[str(r["ticket_id"])]["plan"] = [a for a in actions if a]
        for r in await conn.fetch(
                "SELECT ticket_id,event_type,payload FROM agent_audit_events WHERE ticket_id = ANY($1::uuid[]) "
                "AND event_type IN ('INTENT_CLASSIFIED','INTENT_FASTPATH') ORDER BY created_at", ticket_ids):
            payload = _json(r["payload"])
            out[str(r["ticket_id"])]["intent"] = payload.get("intent")
        for r in await conn.fetch(
                "SELECT ticket_id,count(*) c FROM approval_tasks WHERE ticket_id = ANY($1::uuid[]) "
                "GROUP BY ticket_id", ticket_ids):
            out[str(r["ticket_id"])]["approvals"] = int(r["c"])
        for r in await conn.fetch(
                "SELECT ticket_id,action,status FROM saga_commands WHERE ticket_id = ANY($1::uuid[]) "
                "ORDER BY created_at", ticket_ids):
            out[str(r["ticket_id"])]["commands"].append({"action": r["action"], "status": r["status"]})
        return out
    finally:
        await conn.close()


def evaluate(case: dict, live: dict) -> dict:
    exp = case["expect_final"]
    gold_intent = case.get("intent")
    checks: dict[str, bool | None] = {}
    notes: list[str] = []

    checks["intent"] = str(live.get("intent")) == str(gold_intent)
    if not checks["intent"]:
        notes.append(f"intent {live.get('intent')} != {gold_intent}")

    # 政策维度只判“该场景确实需要作出的政策资格裁决”。
    # 缺事实（政策引擎根本没跑）与金额超阈值（风险环节升级）不算政策裁决。
    pol = live.get("policy") or {}
    gold_decision = exp.get("policy_decision")
    gold_reason = exp.get("reason_code")
    if not gold_decision or (gold_reason in NON_POLICY_VERDICTS and not exp.get("rule_id")):
        checks["policy"] = None
    else:
        if (pol.get("stored_reason") or "") in ACTION_AUTH_GATES:
            # 业务资格看引擎原始 winner，门禁改写由 route 维度反映
            actual = (pol.get("raw_decision"), pol.get("raw_reason"), pol.get("raw_rule"))
        else:
            stored_rules = pol.get("stored_rules") or []
            actual = (pol.get("stored_decision"), pol.get("stored_reason"),
                      stored_rules[0] if stored_rules else None)
        ok = actual[0] == gold_decision
        if ok and gold_reason:
            ok = actual[1] == gold_reason
        if ok and exp.get("rule_id"):
            ok = actual[2] == exp["rule_id"]
        checks["policy"] = ok
        if not ok:
            notes.append(f"policy {'/'.join(str(x) for x in actual if x)} "
                         f"!= {gold_decision}/{gold_reason}/{exp.get('rule_id')}")

    # 动作维度只在该场景确实应当产出动作时判分：政策拒绝/不可自动化时不要求调用 planner
    expected_actions = set(exp.get("authorized_actions") or []) | set(exp.get("commands") or [])
    if expected_actions:
        actual = set(live.get("plan") or [])
        checks["action"] = actual == expected_actions
        if not checks["action"]:
            notes.append(f"plan {sorted(actual)} != {sorted(expected_actions)}")
    else:
        checks["action"] = None

    want_cmds = set(exp.get("commands") or [])
    got_cmds = {c["action"] for c in (live.get("commands") or [])}
    if exp.get("policy_decision") == "ALLOW":
        approval_ok = (live.get("approvals", 0) > 0) == bool(exp.get("approval_required"))
        checks["authorization"] = approval_ok and got_cmds <= want_cmds
        if not checks["authorization"]:
            notes.append(f"auth approval={live.get('approvals')} (want {exp.get('approval_required')}), "
                         f"commands {sorted(got_cmds)} vs {sorted(want_cmds)}")
    elif gold_reason in RISK_ESCALATIONS:
        # 超出自动执行阈值：必须拦住，不得直接产生资金命令
        checks["authorization"] = got_cmds <= want_cmds
        if not checks["authorization"]:
            notes.append(f"auth risk escalation executed {sorted(got_cmds)} (want {sorted(want_cmds)})")
    else:
        checks["authorization"] = None

    want_types = exp.get("response_type_any_of") or [exp.get("response_type")]
    want_status = exp.get("run_status_any_of") or [exp.get("run_status")]
    checks["route"] = bool(live.get("response_type") in want_types
                           and live.get("run_status") in want_status)
    if not checks["route"]:
        notes.append(f"route {live.get('response_type')}/{live.get('run_status')} "
                     f"!= {want_types}/{want_status}")

    applicable = [v for v in checks.values() if v is not None]
    return {"checks": checks, "passed": all(applicable), "notes": "; ".join(notes)}


def classify(case_id: str, checks: dict) -> str:
    """失败归因：G=金标口径待统一，I=纯意图映射，S=系统缺陷（含政策/授权/路由）。"""
    bad = [k for k, v in checks.items() if v is False]
    if not bad:
        return ""
    if case_id in BARE_TUI_GOLD_PENDING:
        return "G 金标口径待统一"
    if bad == ["intent"]:
        return "I 意图映射"
    return "S 系统缺陷:" + "+".join(bad)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(OUT / f"task-success-live-{datetime.date.today().isoformat()}.csv"))
    ap.add_argument("--db", required=True)
    ap.add_argument("--data", default=str(DATA), help="金标文件（默认 v2.4）")
    ap.add_argument("--tag", default="", help="输出文件名后缀，如 v2.4")
    a = ap.parse_args()

    data_path = pathlib.Path(a.data)
    cases = [json.loads(line) for line in data_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    live_rows = {r["case_id"]: r for r in csv.DictReader(open(a.csv, encoding="utf-8-sig"))}
    picked = [(c, live_rows.get(c["case_id"])) for c in cases]
    picked = [(c, r) for c, r in picked if r and r.get("workflow") != "SKIP" and r.get("ticket_id")]
    traces = await load_traces(a.db, [r["ticket_id"] for _, r in picked])

    rows = []
    dims = ["intent", "policy", "action", "authorization", "route"]
    dim_ok = collections.Counter()
    dim_n = collections.Counter()
    for case, live_row in picked:
        tid = live_row["ticket_id"]
        live = {**traces.get(tid, {}),
                "response_type": live_row.get("response_type"),
                "run_status": live_row.get("run_status")}
        res = evaluate(case, live)
        for d in dims:
            v = res["checks"][d]
            if v is not None:
                dim_n[d] += 1
                dim_ok[d] += int(bool(v))
        rows.append({"case_id": case["case_id"], "group": case["group"],
                     "decision_pass": "PASS" if res["passed"] else "FAIL",
                     **{d: ("" if res["checks"][d] is None else ("OK" if res["checks"][d] else "NG")) for d in dims},
                     "intent_expected": str(case.get("intent")),
                     "intent_actual": str(live.get("intent")),
                     "cause": classify(case["case_id"], res["checks"]),
                     "detail": res["notes"]})

    total = len(rows)
    passed = sum(r["decision_pass"] == "PASS" for r in rows)
    stamp = datetime.date.today().isoformat()
    suffix = f"-{a.tag}" if a.tag else ""
    csv_path = OUT / f"decision-accuracy-{stamp}{suffix}.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["case_id", "group", "decision_pass", *dims,
                                           "intent_expected", "intent_actual", "cause", "detail"])
        w.writeheader(); w.writerows(rows)

    lines = [f"# Agent Decision Accuracy（{stamp}）", "",
             f"- 数据：`{data_path.name}`",
             f"- 有效用例：**{total}**（SKIP 与缺少 trace 的不计入）", "",
             "| 维度 | 准确率 | 适用样本 | 不适用 |", "| --- | --- | --- | --- |",
             f"| **Decision（严格全对）** | **{passed}/{total} = {passed / total:.1%}** | {total} | 0 |"]
    for d in dims:
        n = dim_n[d]
        lines.append(f"| {d} | {dim_ok[d]}/{n} = {(dim_ok[d] / n if n else 0):.1%} | {n} | {total - n} |")
    fails = [r for r in rows if r["decision_pass"] == "FAIL"]
    pending = [r for r in fails if r["case_id"] in BARE_TUI_GOLD_PENDING]
    if pending:
        projected = passed + len(pending)
        lines += ["", f"敏感性：这 {len(pending)} 条仅因裸动词“退”的意图口径与 E1 金标冲突"
                  f"（`{'`, `'.join(r['case_id'] for r in pending)}`），若按 E1 口径统一，"
                  f"Decision Accuracy 为 {projected}/{total} = {projected / total:.1%}。"]
    lines += ["", "## 失败归因", ""]
    by_cause = collections.OrderedDict()
    for r in fails:
        by_cause.setdefault(r["cause"], []).append(r["case_id"])
    for cause, ids in by_cause.items():
        lines.append(f"- {cause}：{len(ids)} 条（`{'`, `'.join(ids)}`）")
    lines += ["", f"## 失败用例明细（{len(fails)}）", ""]
    for r in fails:
        lines.append(f"- `{r['case_id']}`（{r['group']}）：{r['detail']}")
    (OUT / f"decision-accuracy-{stamp}{suffix}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\n结果：{csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
