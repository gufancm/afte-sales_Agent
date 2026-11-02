# -*- coding: utf-8 -*-
"""记忆系统自检：打印某工单将回放的多轮历史 + 滚动摘要 + 情景/语义记忆。

用法（容器内或宿主）：
  python scripts/check_memory.py <ticket_id> [tenant_id] [user_id]
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.bootstrap import build_deps  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.memory.service import render_memory_note  # noqa: E402


async def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python scripts/check_memory.py <ticket_id> [tenant_id] [user_id]")
        return
    ticket_id = sys.argv[1]
    tenant_id = sys.argv[2] if len(sys.argv) > 2 else "tenant_demo"
    user_id = sys.argv[3] if len(sys.argv) > 3 else "u_demo"
    deps = build_deps(get_settings())
    mem = deps.memory

    conv = await mem.load_conversation(tenant_id=tenant_id, ticket_id=ticket_id)
    print(f"== 短期跨轮记忆（工单 {ticket_id}）==")
    print(f"回放消息数={len(conv.messages)}  统计={conv.stats}")
    for i, m in enumerate(conv.messages, 1):
        print(f"  [{i}] {m['role']}: {str(m['content'])[:90]}")
    print(f"滚动摘要={conv.summary!r}")

    ctx = await mem.build_context(tenant_id=tenant_id, user_id=user_id,
                                  ticket_id=ticket_id, query="订单状态查询 退款")
    print("== 情景/语义记忆 ==")
    print(f"画像={ctx.profile}")
    print(f"情景命中={len(ctx.episodes)}  meta={ctx.meta}")
    for e in ctx.episodes:
        print(f"  * ({e['outcome_type']}/{e['intent']}, score={e['score']}) {str(e['summary'])[:90]}")

    state = {"conversation_summary": {"text": conv.summary} if conv.summary else None,
             "memory_context": {"profile": ctx.profile, "episodes": ctx.episodes}}
    print("== 注入意图提示的记忆片段（非权威） ==")
    print(render_memory_note(state) or "(空)")


if __name__ == "__main__":
    asyncio.run(main())