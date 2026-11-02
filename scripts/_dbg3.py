import asyncio, asyncpg, json
async def main():
    c = await asyncpg.connect("postgresql://agent_user:change-me@localhost:5432/aftersale")
    tid = "21e778fe-c19f-4f1f-9178-4f60a50fa6d6"
    r = await c.fetchrow("select trace from policy_decisions where ticket_id=$1 order by created_at desc limit 1", tid)
    tr = r["trace"]
    if isinstance(tr, str):
        tr = json.loads(tr)
    print("time_basis", tr.get("time_basis"), "missing", tr.get("missing_facts"))
    for b in tr.get("blocked_rules", []):
        print("BLOCKED", b)
    for e in tr.get("evaluated", []):
        if e["rule_id"] == "R-JD-NR-EXPIRED-001" or e["rule_id"].startswith("R-JD-NR-WINDOW"):
            print("EVAL", e["rule_id"], e["matched"], e["blocked"], e["conditions"])
    await c.close()
asyncio.run(main())