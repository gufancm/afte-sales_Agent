# -*- coding: utf-8 -*-
"""Task Success v2 端到端 runner：Workflow 与 Business 两个指标分离。

离线（默认，零 LLM 成本）：
  - 四要素、分组、政策重放（含动作授权 authorized_actions）；
  - 动作授权门禁：ALLOW 但动作不匹配 -> NEED_INFO；无可自动化动作 -> MANUAL；
  - 命令、审批要求、业务终态声明必须自洽。

live（--live，会产生 LLM 调用费用）：
  - 建单 -> 逐轮发消息 -> 断言 response_type / run_status；
  - 审批用例：遇到 WAITING_APPROVAL 必须拿到 approval payload，按 approval_expect 批准/拒绝；
  - 数据库断言命令数量（幂等）、禁止动作 0 次；
  - 业务完成率只统计 business_expect.applicable 的用例，并轮询下游终态。

指标口径：
  Workflow Handling Success = 终态/命令/审批/安全阻断正确（受理即算成功，不等业务完成）
  End-to-End Business Completion = 下游 Saga 命令确认 / 资金状态达到终态
  两者分开输出，绝不把 WAITING_EXTERNAL 算成业务完成。
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.domain.enums import Intent  # noqa: E402
from app.graph.nodes import decide_intent_route  # noqa: E402
from app.policy.engine import PolicyEngine  # noqa: E402
from app.policy.library import load_rules  # noqa: E402
from app.policy.repository import InMemoryRuleRepository  # noqa: E402

DATA = ROOT / "tests" / "eval" / "data" / "frozen" / "task_success_v2.v2.5.jsonl"
HITL_DATA = ROOT / "tests" / "eval" / "data" / "task_success_hitl_v1.jsonl"
OUT = ROOT / "reports"
TARGET = {"仅退款": 20, "退货退款": 20, "在途拦截退款": 15, "补发商品": 15,
          "催发货与物流查询": 10, "投诉和人工转接": 10, "多轮澄清、异常与失败恢复": 10}
API = os.environ.get("API_BASE", "http://localhost:8000").rstrip("/")
ACTION = {"REFUND_ONLY": "REQUEST_REFUND_ONLY", "RETURN_REFUND": "REQUEST_RETURN_REFUND",
          "INTERCEPT_REFUND": "INTERCEPT_PARCEL", "RESHIPMENT": "CREATE_RESHIPMENT",
          "EXPEDITE": "EXPEDITE_DELIVERY"}
REFUND_ACTIONS = {"REQUEST_REFUND_ONLY", "REQUEST_RETURN_REFUND"}
NO_CAP_RULES = {"R-JD-NR-WINDOW-OK-001", "R-JD-NR-ALLOW-001", "R-JD-Q-RETURN-001",
                "R-JD-Q-REPAIR-001", "R-JD-GOLD-WEIGHT-001", "R-JD-GOLD-QUALITY-001",
                "R-JD-EBIKE-3BAO-001", "R-JD-CANCEL-UNSHIPPED-001",
                "R-JD-BAKERY-WINDOW-OK-001"}


def load_cases(path: pathlib.Path = DATA) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _facts(case: dict) -> dict:
    f = json.loads(json.dumps(case["fixture"]))
    f["intent"] = case["intent"]
    return f


def _pct(a: int, b: int) -> float:
    return round(a / b, 4) if b else 0.0


# --------------------------------------------------------------------------- #
# 离线自检
# --------------------------------------------------------------------------- #
def offline(verbose: bool, data: pathlib.Path = DATA, tag: str = "") -> int:
    cases = load_cases(data)
    rules = load_rules()
    engine = PolicyEngine(repo=InMemoryRuleRepository(rules))
    problems: list[dict] = []
    n_policy = n_route = four = 0
    approval_cases = business_cases = 0

    for c in cases:
        e = c["expect_final"]
        errs: list[str] = []
        has_turns = bool(c.get("turns")) and all(t.get("user") for t in c["turns"])
        has_facts = all(k in c["fixture"] for k in ("order", "logistics", "finance", "claim"))
        has_expect = bool(e.get("response_type")) and "commands" in e and bool(c.get("expected_business_result"))
        has_forbidden = bool(c.get("forbidden"))
        if has_turns and has_facts and has_expect and has_forbidden:
            four += 1
        else:
            errs.append("四要素不完整")

        # 1) 政策重放（仅 policy 路由）：decision / reason / rule / clauses / 动作授权
        if e.get("policy_checked"):
            n_policy += 1
            d = engine.decide(tenant_id="tenant_demo", intent=Intent(c["intent"]),
                              facts=_facts(c), as_of=c["fixture"].get("claim", {}).get("claim_at"))
            win = (d.trace or {}).get("winner") or {}
            if d.decision != e["policy_decision"]:
                errs.append(f"decision {d.decision} != {e['policy_decision']}")
            if d.reason_code != e["reason_code"]:
                errs.append(f"reason {d.reason_code} != {e['reason_code']}")
            if e.get("rule_id") and win.get("rule_id") != e["rule_id"]:
                errs.append(f"rule {win.get('rule_id')} != {e['rule_id']}")
            if e.get("required_clause_ids") and e["required_clause_ids"] != list(d.required_clause_ids):
                errs.append(f"clauses {d.required_clause_ids} != {e['required_clause_ids']}")
            if e.get("policy_decision") == "ALLOW":
                if sorted(d.authorized_actions) != sorted(e.get("authorized_actions") or []):
                    errs.append(f"authorized {d.authorized_actions} != {e.get('authorized_actions')}")

        # 2) 动作授权门禁与终态一致
        requested = ACTION.get(c["intent"])
        cmds = e.get("commands") or []
        if e.get("policy_decision") == "ALLOW":
            policy_actions = e.get("policy_actions") or []
            authorized = e.get("authorized_actions") or []
            if not policy_actions:
                if e["response_type"] != "MANUAL" or e.get("gate_reason") != "NO_AUTOMATABLE_ACTION_FOR_POLICY":
                    errs.append("ALLOW 但无可自动化动作，终态应为 MANUAL/NO_AUTOMATABLE_ACTION_FOR_POLICY")
            elif requested not in authorized:
                if e["response_type"] != "NEED_INFO" or e.get("gate_reason") != "ACTION_NOT_AUTHORIZED_BY_POLICY":
                    errs.append("请求动作未被授权，终态应为 NEED_INFO/ACTION_NOT_AUTHORIZED_BY_POLICY")
            else:
                if cmds != [requested] or e["response_type"] != "PROCESSING":
                    errs.append(f"已授权动作应提交 1 条 {requested} 并返回 PROCESSING")
        else:
            if cmds:
                errs.append(f"非 ALLOW 却声明命令 {cmds}")

        # 3) 审批要求与风控规则自洽（离线只校验派生口径）
        if e.get("approval_required"):
            approval_cases += 1
            if not cmds:
                errs.append("approval_required=true 但没有命令")
            if requested in REFUND_ACTIONS and e.get("rule_id") not in NO_CAP_RULES:
                errs.append("有金额上限的退款不应默认要求审批")
        if cmds and requested in REFUND_ACTIONS and e.get("rule_id") in NO_CAP_RULES:
            if not e.get("approval_required"):
                errs.append("无金额上限的退款必须要求人工审批")

        # 4) 业务完成声明
        biz = e.get("business_expect") or {}
        if biz.get("applicable"):
            business_cases += 1
            if not cmds or requested not in REFUND_ACTIONS:
                errs.append("business.applicable 只允许出现在退款命令用例")
            if not biz.get("signal"):
                errs.append("business.applicable 缺少下游终态信号")

        # 5) 只读/安全路由
        if e.get("route") == "readonly" and c["intent"] == "STATUS_QUERY":
            n_route += 1
            route, hint, _ = decide_intent_route(None, {
                "intent": c["intent"], "safety_flags": e.get("safety_flags") or [],
                "secondary_intents": e.get("secondary_intents") or []})
            if route != "readonly":
                errs.append(f"route {route} != readonly")
        # 投诉：必须已转人工待办，不能只回 ANSWER
        if c["intent"] == "COMPLAINT" and not (e.get("safety_flags") or []):
            n_route += 1
            if e["response_type"] not in ("MANUAL", "DENIED"):
                errs.append("普通投诉必须转人工（MANUAL），不能只返回 ANSWER")

        # 6) 禁止动作覆盖
        if not cmds and "ANY_WRITE_COMMAND" not in c["forbidden"]:
            errs.append("无命令但 forbidden 未包含 ANY_WRITE_COMMAND")
        for must in ("DUPLICATE_COMMAND", "UNAPPROVED_FUND_MOVEMENT"):
            if must not in c["forbidden"]:
                errs.append(f"forbidden 缺少 {must}")

        # 7) 末轮与终态一致
        last = c["turns"][-1].get("expect")
        if last and last.get("response_type") and last["response_type"] != e["response_type"]:
            errs.append(f"末轮期望 {last['response_type']} != 终态 {e['response_type']}")

        if errs:
            problems.append({"case_id": c["case_id"], "group": c["group"], "title": c["title"],
                             "expected": f"{e.get('policy_decision')}/{e.get('reason_code')}/{e.get('rule_id')}",
                             "problems": errs})

    dist = Counter(c["group"] for c in cases)
    dist_ok = dict(dist) == TARGET
    metrics = {
        "total": len(cases),
        "four_elements_complete": four,
        "policy_checked": n_policy,
        "route_checked": n_route,
        "multi_turn": sum(1 for c in cases if len(c["turns"]) > 1),
        "approval_cases": approval_cases,
        "business_cases": business_cases,
        "distribution_ok": dist_ok,
        "failed": len(problems),
    }
    ok = not problems and dist_ok and four == len(cases)
    print(f"[TASK-SUCCESS-OFFLINE] {len(cases) - len(problems)}/{len(cases)} 通过 | 四要素 {four}/{len(cases)} "
          f"| 政策重放 {n_policy} | 路由校验 {n_route} | 审批 {approval_cases} | 业务终态 {business_cases} "
          f"| 多轮 {metrics['multi_turn']} | 分组匹配 {'是' if dist_ok else '否'}")
    for g, v in dist.items():
        print(f"  - {g}: {v}（目标 {TARGET.get(g)}）")
    if verbose:
        for p in problems:
            print(f"  FAIL {p['case_id']} {p['title']}: {'; '.join(p['problems'])}")

    OUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.date.today().isoformat()
    suffix = f"-{tag}" if tag else ""
    (OUT / f"task-success-offline-{stamp}{suffix}.json").write_text(json.dumps(
        {"version": "task-success-offline/2", "date": stamp, "metrics": metrics,
         "distribution": dict(dist), "failed": problems}, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Task Success v2 离线自检", "", f"日期：{stamp}　用例：{len(cases)}", "",
             "| 指标 | 结果 |", "| --- | --- |",
             f"| 用例总数 | {len(cases)} |",
             f"| 四要素齐全 | {four}/{len(cases)} |",
             f"| 政策重放（含动作授权） | {n_policy - sum(1 for p in problems if any('decision' in x or 'reason' in x or 'rule' in x or 'clauses' in x or 'authorized' in x for x in p['problems']))}/{n_policy} |",
             f"| 路由/投诉终态校验 | {n_route} 条 |",
             f"| 需审批用例 | {approval_cases} |",
             f"| 可测业务终态用例 | {business_cases} |",
             f"| 自检失败 | {len(problems)} |", "",
             "## 说明", "",
             "- 离线只校验“用例与政策/授权/路由/终态是否自洽”，不代替端到端执行；",
             "- `policy_decision=ALLOW` 只代表业务资格；能否执行由 `authorized_actions` 决定；",
             "- `PROCESSING/WAITING_EXTERNAL` 是受理终态，不计入业务完成率；",
             "- 业务完成率只统计 `business_expect.applicable=true` 的退款用例。"]
    if problems:
        lines += ["", "## 失败明细", "", "| 用例 | 期望 | 问题 |", "| --- | --- | --- |"]
        for p in problems:
            lines.append(f"| {p['case_id']} | {p['expected']} | {'; '.join(p['problems'])} |")
    (OUT / f"task-success-offline-{stamp}{suffix}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"报告：{OUT / f'task-success-offline-{stamp}{suffix}.md'}")
    print("TASK_SUCCESS_OFFLINE_OK" if ok else "TASK_SUCCESS_OFFLINE_FAIL")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# live
# --------------------------------------------------------------------------- #
def _req(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json", "x-tenant-id": "tenant_demo", "x-user-id": "u_demo",
               "x-trace-id": str(uuid.uuid4()), "Idempotency-Key": str(uuid.uuid4())}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode("utf-8")[:300]}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": f"{type(e).__name__}: {e}"}


async def _db_commands(dsn: str, ticket_id: str) -> list[dict]:
    import asyncpg
    conn = await asyncpg.connect(dsn)
    try:
        # RLS：应用角色必须声明租户，否则查询结果为空（会误报“0 条命令”）
        await conn.execute("SELECT set_config('app.tenant_id', $1, false)",
                           os.environ.get("TASK_TENANT", "tenant_demo"))
        rows = await conn.fetch("SELECT action,status FROM saga_commands WHERE ticket_id=$1 ORDER BY created_at", ticket_id)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


def _poll_commands(dsn: str, ticket_id: str, timeout_s: int = 20) -> list[dict]:
    deadline = time.time() + timeout_s
    rows: list[dict] = []
    while time.time() < deadline:
        rows = asyncio.run(_db_commands(dsn, ticket_id))
        if rows and all(r["status"] in ("CONFIRMED", "FAILED_CONFIRMED", "CANCELLED") for r in rows):
            return rows
        time.sleep(1)
    return rows


async def _db_idempotency_counts(dsn: str, ticket_id: str) -> dict:
    """幂等用例需要同时看审批、退款申请与命令三层，不能只看命令。"""
    import asyncpg
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("SELECT set_config('app.tenant_id', $1, false)",
                           os.environ.get("TASK_TENANT", "tenant_demo"))
        row = await conn.fetchrow(
            "SELECT (SELECT count(*) FROM approval_tasks WHERE ticket_id=$1) AS approvals,"
            " (SELECT count(DISTINCT plan_hash) FROM approval_tasks WHERE ticket_id=$1) AS plans,"
            " (SELECT count(*) FROM refund_requests WHERE ticket_id=$1) AS refund_requests,"
            " (SELECT count(*) FROM saga_commands WHERE ticket_id=$1) AS commands", ticket_id)
        return dict(row)
    finally:
        await conn.close()


async def _db_case_meta(dsn: str, ticket_id: str) -> dict:
    """逐条结果需要可追溯：政策裁决 + 命中规则 + 计划指纹。"""
    import asyncpg
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("SELECT set_config('app.tenant_id', $1, false)",
                           os.environ.get("TASK_TENANT", "tenant_demo"))
        pol = await conn.fetchrow(
            "SELECT decision,reason_code,rule_ids FROM policy_decisions WHERE ticket_id=$1 "
            "ORDER BY created_at DESC LIMIT 1", ticket_id)
        plan = await conn.fetchrow(
            "SELECT plan_hash FROM action_plans WHERE ticket_id=$1 ORDER BY created_at DESC LIMIT 1",
            ticket_id)
        return {"policy_decision": pol["decision"] if pol else "",
                "reason_code": pol["reason_code"] if pol else "",
                "rule_ids": "|".join(pol["rule_ids"] or []) if pol else "",
                "plan_hash": plan["plan_hash"] if plan else ""}
    finally:
        await conn.close()


def live(limit: int, dsn: str | None, verbose: bool, data: pathlib.Path = DATA,
         approval_mode: str | None = None, ids: str | None = None, tag: str = "") -> int:
    cases = load_cases(data)
    if ids:
        wanted = {x.strip() for x in ids.split(",") if x.strip()}
        cases = [c for c in cases if c["case_id"] in wanted]
    if limit:
        cases = cases[:limit]
    rows = []
    workflow_ok = business_ok = business_applicable = 0
    approval_ok = approval_total = 0
    dangerous = 0
    skipped = 0
    for c in cases:
        e = c["expect_final"]
        if c.get("live_skip"):
            skipped += 1
            rows.append({"case_id": c["case_id"], "workflow": "SKIP", "business": "N/A",
                         "detail": f"OFFLINE_ONLY: {c['live_skip']}"})
            if verbose:
                print(f"[SKIP] {c['case_id']} {c['title']} -> {c['live_skip']}")
            continue
        oid = c.get("create_order_id", c["fixture"]["order"]["order_id"])
        code, t = _req("POST", "/api/v1/tickets", {"intent": "UNKNOWN", "order_id": oid})
        if code >= 300 or "ticket_id" not in t:
            rows.append({"case_id": c["case_id"], "workflow": "FAIL", "business": "N/A",
                         "detail": f"CREATE_FAIL {str(t.get('error'))[:80]}"})
            continue
        tid = t["ticket_id"]
        t0 = time.monotonic()
        last: dict = {}
        errs: list[str] = []
        expect_approval = bool(e.get("approval_required"))
        mode = approval_mode or e.get("approval_expect") or ("APPROVE" if expect_approval else "NONE")
        got_approval = False
        for turn_no, turn in enumerate(c["turns"], start=1):
            body = {"text": turn["user"]}
            if c["fixture"].get("claim", {}).get("claim_at"):
                body["claim_at"] = c["fixture"]["claim"]["claim_at"]
            code, r = _req("POST", f"/api/v1/tickets/{tid}/messages", body)
            last = r
            if code >= 300:
                errs.append(f"turn{turn_no} http {code}")
                break
            expected_turn = turn.get("expect") or {}
            if expected_turn.get("response_type") and r.get("response_type") != expected_turn["response_type"]:
                errs.append(f"turn{turn_no} type {r.get('response_type')} != {expected_turn['response_type']}")
            if r.get("run_status") == "WAITING_APPROVAL":
                got_approval = True
                approval_total += 1
                ap = (r.get("approval") or {}).get("approval_id")
                if not ap:
                    errs.append(f"turn{turn_no} 审批缺少 approval_id")
                    break
                decision = "APPROVED" if mode == "APPROVE" else "REJECTED"
                code, r = _req("POST", f"/api/v1/tickets/{tid}/approval",
                               {"approval_id": ap, "decision": decision})
                last = r
                if code >= 300 or not r.get("resumed"):
                    errs.append(f"turn{turn_no} 审批恢复失败 {code} {str(r.get('error'))[:60]}")
                else:
                    approval_ok += 1
        got_type = last.get("response_type")
        got_status = last.get("run_status")
        if got_type not in (e.get("response_type_any_of") or [e["response_type"]]):
            errs.append(f"type {got_type} not in {e.get('response_type_any_of') or [e['response_type']]}")
        if got_status not in (e.get("run_status_any_of") or [e["run_status"]]):
            errs.append(f"status {got_status} not in {e.get('run_status_any_of') or [e['run_status']]}")
        if expect_approval and not got_approval and mode == "APPROVE":
            errs.append("期望人工审批但未进入 WAITING_APPROVAL")
        if not expect_approval and got_approval:
            errs.append("未期望审批却触发 WAITING_APPROVAL（审批要求口径不一致）")

        cmds = _poll_commands(dsn, tid) if dsn else []
        dup = c.get("duplicate_submission")
        if dsn and dup:
            counts = asyncio.run(_db_idempotency_counts(dsn, tid))
            want = dup.get("expected") or {}
            for key in ("approvals", "refund_requests", "commands"):
                if key in want and counts.get(key) != want[key]:
                    errs.append(f"幂等 {key} {counts.get(key)} != {want[key]}")
            if counts.get("plans") not in (0, want.get("approvals")):
                errs.append(f"同一诉求产生了 {counts.get('plans')} 个不同 plan_hash")
        if dsn:
            want = len(e.get("commands") or [])
            got = len(cmds)
            if mode == "REJECT" and got != 0:
                errs.append(f"审批拒绝后仍产生 {got} 条命令")
            elif got != want:
                errs.append(f"commands {got} != {want}")
            for r in cmds:
                if r["action"] in c["forbidden"] or ("ANY_WRITE_COMMAND" in c["forbidden"] and not want):
                    dangerous += 1
                    errs.append(f"FORBIDDEN_ACTION_EXECUTED {r['action']}")

        biz = e.get("business_expect") or {}
        biz_result = "N/A"
        if biz.get("applicable"):
            business_applicable += 1
            if not dsn:
                biz_result = "UNMEASURED"
            elif mode == "REJECT":
                biz_result = "N/A"
            elif cmds and all(r["status"] == "CONFIRMED" for r in cmds):
                biz_result = "PASS"
                business_ok += 1
            else:
                biz_result = "FAIL"
                errs.append(f"business terminal not reached: {[r['status'] for r in cmds]}")

        passed = not errs
        workflow_ok += 1 if passed else 0
        meta = asyncio.run(_db_case_meta(dsn, tid)) if dsn else {}
        rows.append({"case_id": c["case_id"], "workflow": "PASS" if passed else "FAIL",
                     "business": biz_result, "detail": "; ".join(errs) or str(got_type),
                     "ticket_id": tid, "response_type": got_type, "run_status": got_status,
                     "elapsed_ms": int((time.monotonic() - t0) * 1000),
                     "policy_decision": meta.get("policy_decision", ""),
                     "reason_code": meta.get("reason_code", ""),
                     "rule_ids": meta.get("rule_ids", ""),
                     "plan_hash": meta.get("plan_hash", ""),
                     "commands": "|".join(f'{r["action"]}:{r["status"]}' for r in cmds)})
        if verbose:
            print(f"[{'PASS' if passed else 'FAIL'}] {c['case_id']} {c['title']} -> {got_type}/{got_status} "
                  f"biz={biz_result} {'; '.join(errs)}")

    n = len(cases) - skipped
    print(f"\n[LIVE] Workflow {workflow_ok}/{n} = {_pct(workflow_ok, n):.1%} | "
          f"Business {business_ok}/{business_applicable} = {_pct(business_ok, business_applicable):.1%} | "
          f"Approval resume {approval_ok}/{approval_total} | Dangerous releases {dangerous}")
    stamp = datetime.date.today().isoformat()
    suffix = f"-{tag}" if tag else ""
    with (OUT / f"task-success-live-{stamp}{suffix}.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["case_id", "workflow", "business", "detail",
                                           "ticket_id", "response_type", "run_status", "elapsed_ms",
                                           "policy_decision", "reason_code", "rule_ids", "plan_hash",
                                           "commands"])
        w.writeheader(); w.writerows(rows)
    (OUT / f"task-success-live-{stamp}{suffix}.json").write_text(json.dumps(
        {"version": "task-success-live/2", "date": stamp, "data": str(data.name),
         "workflow_success": workflow_ok, "total": n, "workflow_rate": _pct(workflow_ok, n),
         "business_success": business_ok, "business_applicable": business_applicable,
         "business_rate": _pct(business_ok, business_applicable),
         "approval_resume_ok": approval_ok, "approval_total": approval_total,
         "dangerous_releases": dangerous, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果：{OUT / f'task-success-live-{stamp}{suffix}.json'}")
    return 0 if workflow_ok == n and dangerous == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--db", default=os.environ.get("TASK_DB_DSN"))
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--hitl", action="store_true", help="运行独立 HITL 审批集（含拒绝）")
    ap.add_argument("--approve", choices=["APPROVE", "REJECT"], default=None)
    ap.add_argument("--ids", default=None, help="只跑指定 case_id，逗号分隔")
    ap.add_argument("--data", default=None, help="金标文件路径（默认冻结集 v2.5，100 条全部可 live 复现）")
    ap.add_argument("--tag", default="", help="报告文件名后缀，如 v2.4")
    args = ap.parse_args()
    data = pathlib.Path(args.data) if args.data else (HITL_DATA if args.hitl else DATA)
    return (live(args.limit, args.db, args.verbose, data, args.approve, args.ids, args.tag)
            if args.live else offline(args.verbose, data, args.tag))


if __name__ == "__main__":
    raise SystemExit(main())
