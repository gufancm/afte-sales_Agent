# -*- coding: utf-8 -*-
"""导出逐条执行轨迹：给失败用例留可复核证据（审计事件/工具调用/裁决/计划/命令/LLM 调用）。

用法：
  python tests/eval/export_trace.py --csv reports/task-success-live-2026-10-29.csv \
      --out reports/traces --db postgresql://aftersale_app:***@localhost:5432/aftersale
默认只导出 workflow=FAIL 的用例；加 --all 导出全部。
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime
import json
import pathlib
import uuid

import asyncpg

TABLES = [
    ("ticket_runs", "SELECT run_id,status,created_at FROM ticket_runs WHERE ticket_id=$1 ORDER BY created_at"),
    ("policy_decisions", "SELECT decision,reason_code,rule_ids,required_clause_ids,trace,created_at "
                         "FROM policy_decisions WHERE ticket_id=$1 ORDER BY created_at"),
    ("action_plans", "SELECT plan_id,plan_hash,status,order_version,quote_id,created_at "
                     "FROM action_plans WHERE ticket_id=$1 ORDER BY created_at"),
    ("approval_tasks", "SELECT approval_id,plan_hash,status,expires_at,created_at "
                       "FROM approval_tasks WHERE ticket_id=$1 ORDER BY created_at"),
    ("saga_commands", "SELECT command_id,action,status,idempotency_key,attempt_count,last_error,created_at "
                      "FROM saga_commands WHERE ticket_id=$1 ORDER BY created_at"),
    ("agent_audit_events", "SELECT event_type,payload,created_at FROM agent_audit_events "
                           "WHERE ticket_id=$1 ORDER BY created_at"),
    ("tool_calls", "SELECT tool,status,error_code,duration_ms,created_at FROM tool_calls "
                   "WHERE ticket_id=$1 ORDER BY created_at"),
    ("llm_calls", "SELECT node,model,status,error_code,input_tokens,output_tokens,created_at FROM llm_calls "
                  "WHERE ticket_id=$1 ORDER BY created_at"),
]


def _jsonable(value):
    if isinstance(value, (uuid.UUID, datetime.datetime, datetime.date)):
        return str(value)
    return value


async def dump(dsn: str, ticket_id: str) -> dict:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("SELECT set_config('app.tenant_id', $1, false)", "tenant_demo")
        out: dict = {"ticket_id": ticket_id}
        for name, sql in TABLES:
            try:
                rows = await conn.fetch(sql, ticket_id)
                out[name] = [{k: _jsonable(v) for k, v in dict(r).items()} for r in rows]
            except Exception as exc:  # noqa: BLE001 - 单表失败不影响其它证据
                out[name] = {"error": str(exc)[:200]}
        return out
    finally:
        await conn.close()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--out", default="reports/traces")
    ap.add_argument("--db", required=True)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--case-ids", default="", help="逗号分隔；指定后只导出这些用例")
    ap.add_argument("--detail-csv", default="", help="decision-accuracy csv，用于附上判分结论")
    a = ap.parse_args()
    rows = list(csv.DictReader(open(a.csv, encoding="utf-8-sig")))
    extra = {}
    if a.detail_csv:
        for d in csv.DictReader(open(a.detail_csv, encoding="utf-8-sig")):
            extra[d["case_id"]] = {k: d.get(k, "") for k in ("group", "decision_pass", "cause", "detail")}
    wanted = {x.strip() for x in a.case_ids.split(",") if x.strip()}
    picked = [r for r in rows if r["case_id"] in wanted] if wanted else \
        [r for r in rows if a.all or r.get("workflow") == "FAIL"]
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for r in picked:
        tid = (r.get("ticket_id") or "").strip()
        if not tid:
            continue
        data = await dump(a.db, tid)
        data["case_id"] = r["case_id"]
        data["detail"] = r.get("detail", "")
        if r["case_id"] in extra:
            data["decision_accuracy"] = extra[r["case_id"]]
        (out / f"{r['case_id']}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print("wrote", out / f"{r['case_id']}.json")
    print(f"done: {len(picked)} case(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
