import asyncio, asyncpg
async def main():
    c = await asyncpg.connect("postgresql://aftersale_app:app_pw_change_me@localhost:5432/aftersale")
    rows = await c.fetch("select ticket_id,status,order_id,intent,created_at from aftersale_tickets order by created_at desc limit 5")
    for r in rows:
        print(dict(r))
    for r in rows[:2]:
        tid = r["ticket_id"]
        print("---", tid, r["status"])
        ev = await c.fetch("select event_type,payload from ticket_events where ticket_id=$1 order by created_at desc limit 5", tid)
        for e in ev:
            print(" ev", e["event_type"], str(e["payload"])[:400])
        au = await c.fetch("select event,payload from audit_events where ticket_id=$1 order by created_at desc limit 10", tid)
        for a in au:
            print(" au", a["event"], str(a["payload"])[:400])
        cm = await c.fetch("select action,status from saga_commands where ticket_id=$1", tid)
        print(" cmds", [dict(x) for x in cm])
    await c.close()
asyncio.run(main())