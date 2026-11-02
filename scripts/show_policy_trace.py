"""查看某工单的政策裁决 trace（可解释性取证）。

用法：
  # 容器内（主机名 postgres）
  docker compose run --rm api python scripts/show_policy_trace.py <ticket_id>
  # 宿主机（默认 localhost）
  python scripts/show_policy_trace.py <ticket_id>

不传 ticket_id 时打印最近一条裁决。
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402

from app.config import Settings  # noqa: E402

DSN = os.environ.get("POLICY_APP_DSN",
                     "postgresql+asyncpg://aftersale_app:app_pw_change_me@localhost:5432/aftersale")
TENANT = os.environ.get("POLICY_TENANT", "tenant_demo")


def _dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def main() -> None:
    dsn = DSN
    if not os.environ.get("POLICY_APP_DSN"):
        dsn = _dsn(Settings(_env_file=None).database_url)
    ticket = sys.argv[1] if len(sys.argv) > 1 else None
    conn = await asyncpg.connect(dsn)
    try:
        # policy_decisions 强制 RLS：直连时需注入租户上下文
        await conn.execute("SELECT set_config('app.tenant_id', $1, false)", TENANT)
        cols = ("SELECT policy_decision_id::text AS pid, decision, reason_code, rule_ids, "
                "policy_version, as_of, trace FROM policy_decisions ")
        if ticket:
            rows = await conn.fetch(cols + "WHERE ticket_id = $1::uuid ORDER BY created_at DESC", ticket)
        else:
            rows = await conn.fetch(cols + "ORDER BY created_at DESC LIMIT 1")
    finally:
        await conn.close()
    if not rows:
        print("no policy decision found")
        return
    for r in rows:
        trace = r["trace"]
        if isinstance(trace, str):
            trace = json.loads(trace)
        print("=" * 88)
        print("decision:", r["decision"], "| reason_code:", r["reason_code"])
        print("rule_ids:", list(r["rule_ids"] or []), "| policy_version:", r["policy_version"], "| as_of:", r["as_of"])
        print("policy_decision_id:", r["pid"])
        print("loaded_rule_count:", trace.get("loaded_rule_count"), "| matched:", trace.get("matched_rule_ids"))
        if trace.get("conflict"):
            print("CONFLICT:", json.dumps(trace["conflict"], ensure_ascii=False))
        if trace.get("missing_facts"):
            print("missing_facts:", trace["missing_facts"])
        print("winner:", json.dumps(trace.get("winner"), ensure_ascii=False))
        print("-" * 88)
        for ev in trace.get("evaluated", []):
            flag = "HIT " if ev.get("matched") else "miss"
            print("[%s] %-32s pri=%-3s spec=%-2s %-9s %s" % (
                flag, ev["rule_id"], ev["priority"], ev["specificity"], ev["decision"], ev["reason_code"]))
            for c in ev.get("conditions", []):
                print("        %s %s %s -> actual=%s  [%s]" % (
                    c["fact"], c["op"], c["expected"], c["actual"], "ok" if c["ok"] else "FAIL"))
    print("=" * 88)


if __name__ == "__main__":
    asyncio.run(main())