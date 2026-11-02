"""真实 LLM 端到端探针（内存后端，不写 DB）。打印完整图状态，便于定位路由分支，并自动完成 HITL 审批。

用法：
    docker compose run --rm api python scripts/llm_live_probe.py
    docker compose run --rm api python scripts/llm_live_probe.py "订单 o_1002，衣服破洞，退货退款" o_1002
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.bootstrap import build_deps
from app.config import get_settings
from app.graph.build import build_graph

DEFAULT_TEXT = "订单 o_1002，这件衣服有破洞，属于质量问题，要求退货退款，金额 500 元"


async def main() -> int:
    text = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TEXT
    order_id = sys.argv[2] if len(sys.argv) > 2 else "o_1002"
    s = get_settings()
    print(f"llm_enabled={s.llm_enabled} model={s.llm_model} base_url={s.llm_base_url} "
          f"timeout_ms={s.llm_timeout_ms}")
    if not s.llm_enabled:
        print("LLM_DISABLED")
        return 2
    settings = s.model_copy(update={"app_env": "test", "database_url": "memory://", "rag_backend": "memory"})
    deps = build_deps(settings)
    graph = build_graph(deps, checkpointer=InMemorySaver())
    ticket = await deps.persistence.create_ticket(tenant_id="tenant_demo", user_id="u_demo",
                                                 order_id=order_id, intent="UNKNOWN")
    tid = ticket["ticket_id"]
    run_id = await deps.persistence.create_run(tenant_id="tenant_demo", ticket_id=tid, thread_id="th_live")
    state = {"tenant_id": "tenant_demo", "user_id": "u_demo", "ticket_id": tid, "thread_id": "th_live",
             "run_id": run_id, "trace_id": "tr_live", "messages": [{"role": "user", "content": text}]}
    cfg = {"configurable": {"thread_id": f"{tid}:{run_id}"}}
    res = await graph.ainvoke(state, config=cfg)

    if "__interrupt__" in res:
        approval_id = res["__interrupt__"][0].value["approval_id"]
        print("HITL interrupt, approval_id =", approval_id)
        await deps.persistence.decide_approval(tenant_id="tenant_demo", approval_id=approval_id,
                                              decision="APPROVED", reviewer_id="probe")
        res = await graph.ainvoke(Command(resume={"approval_id": approval_id, "decision": "APPROVED"}), config=cfg)
        print("resumed.")

    print("\n--- 图输出关键状态 ---")
    for key in ("error_code", "evidence_status", "risk_decision", "response_type", "response_status", "final_response"):
        print(f"{key} = {res.get(key)!r}")
    print("intent =", (res.get("intent_result") or {}).get("intent"),
          "conf =", (res.get("intent_result") or {}).get("confidence"))
    print("action_plan =", json.dumps(res.get("action_plan"), ensure_ascii=False))
    print("policy_decision =", json.dumps({k: v for k, v in (res.get("policy_decision") or {}).items()
                                          if k in ("decision", "reason_code", "rule_ids")}, ensure_ascii=False))
    print("commands =", len(getattr(deps.persistence, "commands", []) or []))
    print("llm_calls =", [(c.get("node"), c.get("status"), c.get("error_code")) for c in deps.persistence.llm_calls])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))