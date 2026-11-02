"""DB 版端到端验证：真实 Postgres（app + worker 角色）+ pg RAG + Saga 命令 + Worker 派发。

前置：docker compose up -d 已就绪，且迁移已应用；政策规则/条款已入库
      （python scripts/ingest_policy.py --rules --replace --database-url <APP_DSN>；真实语料见 policy/clauses_jd.json）。

用法：python scripts/verify_db_saga.py
说明：若 aftsale worker 容器在运行，命令可能由容器 worker 领取并派发；脚本会先等待其终态，
      否则本地领取并派发，保证在 compose 常驻 worker 环境下同样可验证。
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import text

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.bootstrap import build_deps  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db.engine import make_engine, make_sessionmaker, system_transaction  # noqa: E402
from app.db.persistence import SqlPersistence  # noqa: E402
from app.domain.enums import Action, Intent, SlotSource  # noqa: E402
from app.domain.schemas import (  # noqa: E402
    ActionPlan, ActionProposal, ExtractedSlots, IntentResult, SlotSources,
)
from app.graph.build import build_graph  # noqa: E402
from app.saga.dispatcher import SagaDispatcher  # noqa: E402

APP_DSN = os.environ.get("POLICY_APP_DSN",
                         "postgresql+asyncpg://aftersale_app:app_pw_change_me@localhost:5432/aftersale")
WORKER_DSN = os.environ.get("POLICY_WORKER_DSN",
                            "postgresql+asyncpg://aftersale_worker:worker_pw_change_me@localhost:5432/aftersale")
MOCK = os.environ.get("MCP_MOCK_URL", "http://localhost:9100")


class StubLLM:
    async def structured(self, *, messages, schema_model, prompt_version, temperature=0.0):
        if schema_model is IntentResult:
            return (IntentResult(intent=Intent.REFUND_ONLY, confidence=0.95,
                                 slots=ExtractedSlots(order_id="o_1002", reason_code="DAMAGED", quantity=1),
                                 slot_sources=SlotSources(order_id=SlotSource.USER_STATED, sku_id=None,
                                                          order_line_id=None, waybill_no=None,
                                                          reason_code=SlotSource.USER_STATED,
                                                          requested_amount_minor=None,
                                                          quantity=SlotSource.USER_STATED,
                                                          product_category=None)),
                    {"request_hash": "x", "response_hash": "y", "usage": {}})
        return (ActionPlan(order_id="o_1002",
                           actions=[ActionProposal(action=Action.REQUEST_REFUND_ONLY, order_id="o_1002",
                                                   sku_ids=["sku_a1"], reason_code="DAMAGED")],
                           rationale="政策允许仅退款"),
                {"request_hash": "x", "response_hash": "y", "usage": {}})


async def _command_status(sf, refund_request_id: str) -> str | None:
    async with system_transaction(sf) as s:
        return await s.scalar(text("SELECT status FROM saga_commands WHERE refund_request_id=CAST(:rid AS uuid)"),
                              {"rid": refund_request_id})


async def main() -> None:
    settings = Settings(app_env="development", database_url=APP_DSN, worker_database_url=WORKER_DSN,
                        oms_mcp_url=MOCK, tms_mcp_url=MOCK, finance_mcp_url=MOCK,
                        llm_model="stub", llm_model_small="", llm_intent_small_model_enabled=False,
                        rag_backend="pg")
    deps = build_deps(settings)
    deps.llm = StubLLM()  # type: ignore[assignment]

    ticket = await deps.persistence.create_ticket(tenant_id="tenant_demo", user_id="u_demo",
                                                  order_id="o_1002", intent="UNKNOWN")
    tid = ticket["ticket_id"]
    run_id = await deps.persistence.create_run(tenant_id="tenant_demo", ticket_id=tid, thread_id="th_db")
    graph = build_graph(deps, checkpointer=InMemorySaver())
    state = {"tenant_id": "tenant_demo", "user_id": "u_demo", "ticket_id": tid,
             "thread_id": "th_db", "run_id": run_id, "trace_id": "tr_db",
             "messages": [{"role": "user", "content": "衣服有质量问题想退款"}]}
    cfg = {"configurable": {"thread_id": f"{tid}:{run_id}"}}
    result = await graph.ainvoke(state, config=cfg)
    # 真实语料未给退款金额上限 -> 风控 REVIEW -> HITL；审批通过后继续
    if "__interrupt__" in result:
        approval_id = result["__interrupt__"][0].value["approval_id"]
        await deps.persistence.decide_approval(tenant_id="tenant_demo", approval_id=approval_id,
                                              decision="APPROVED", reviewer_id="verify")
        result = await graph.ainvoke(Command(resume={"approval_id": approval_id, "decision": "APPROVED"}), config=cfg)
    ev = (result.get("evidence_bundle") or {})
    print("graph:", {"intent": (result.get("intent_result") or {}).get("intent"),
                     "policy": (result.get("policy_decision") or {}).get("decision"),
                     "reason_code": (result.get("policy_decision") or {}).get("reason_code"),
                     "risk": result.get("risk_decision"), "status": result.get("response_status"),
                     "evidence_status": ev.get("status"), "evidence_items": len(ev.get("items") or []),
                     "refund_request_id": result.get("refund_request_id")})
    assert (result.get("policy_decision") or {}).get("decision") == "ALLOW", "服饰质量问题应判 ALLOW"
    assert ev.get("status") == "OK", "证据应闭合（条款齐全）"

    wp = SqlPersistence(make_sessionmaker(make_engine(Settings(database_url=WORKER_DSN))))
    rid = result.get("refund_request_id")
    assert rid, "graph 未创建退款申请"

    if "--enqueue-only" in sys.argv:
        print("ENQUEUE_OK refund_request_id=%s" % rid)
        return

    # 常驻 worker 环境：等待容器 worker 处理到终态
    status = None
    for _ in range(20):
        status = await _command_status(wp.sf, rid)
        if status in ("CONFIRMED", "FAILED_CONFIRMED", "UNKNOWN"):
            break
        await asyncio.sleep(1)

    if status not in ("CONFIRMED", "UNKNOWN"):
        # 无常驻 worker（或未被领取）：本地领取并派发
        command = await wp.claim_next_command()
        assert command, "worker 未领到命令"
        print("claimed:", {"action": command["action"], "refund_request_id": command["refund_request_id"],
                           "quote_id": command["quote_id"], "quote_hash": bool(command["quote_hash"])})
        dispatcher = SagaDispatcher(orders=deps.tools.orders, logistics=deps.tools.logistics, finance=deps.tools.finance)
        res = await dispatcher.dispatch(command)
        status = await wp.mark_command_result(command_id=command["command_id"], tenant_id=command["tenant_id"],
                                              result=res)
        print("dispatch:", {"mcp_result": res.get("status"), "command_status": status})
    else:
        print("worker(container):", {"command_status": status})

    fin = await deps.finance.get_refund_status(tenant_id="tenant_demo", refund_request_id=rid)
    print("finance:", {"refund_status": fin.get("status")})
    assert status == "CONFIRMED" and fin.get("status") == "SUCCEEDED"
    print("DB_SAGA_OK")


if __name__ == "__main__":
    asyncio.run(main())