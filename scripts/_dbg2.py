import asyncio, asyncpg
async def main():
    c = await asyncpg.connect("postgresql://agent_user:change-me@localhost:5432/aftersale")
    rows = await c.fetch("select table_name from information_schema.tables where table_schema='public' order by 1")
    print([r["table_name"] for r in rows])
    tid = "21e778fe-c19f-4f1f-9178-4f60a50fa6d6"
    for t in ("audit_log","audits","event_log","ticket_audit"):
        try:
            rs = await c.fetch(f"select * from {t} where ticket_id=$1 order by 1 desc limit 5", tid)
            print(t, len(rs), [dict(x) for x in rs][:2])
        except Exception as e:
            pass
    pd = await c.fetch("select decision,reason_code,rule_ids,trace from policy_decisions where ticket_id=$1 order by created_at desc limit 2", tid)
    for r in pd:
        d=dict(r); print("PD", d["decision"], d["reason_code"], d["rule_ids"]); print("TRACE", str(d["trace"])[:800])
    cm = await c.fetch("select action,status,ticket_id from saga_commands order by created_at desc limit 5")
    print("CMDS", [dict(x) for x in cm])
    await c.close()
asyncio.run(main())