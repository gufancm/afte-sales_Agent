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
    """确定性 LLM 替身：由用例注入订单/原因/动作，避免真实模型参与链路验证。"""

    def __init__(self, *, order_id: str, sku_id: str, reason_code: str) -> None:
        self.order_id = order_id
        self.sku_id = sku_id
        self.reason_code = reason_code

    async def structured(self, *, messages, schema_model, prompt_version, temperature=0.0):
        if schema_model is IntentResult:
            return (IntentResult(intent=Intent.REFUND_ONLY, confidence=0.95,
                                 slots=ExtractedSlots(order_id=self.order_id, reason_code=self.reason_code,
                                                      quantity=1),
                                 slot_sources=SlotSources(order_id=SlotSource.USER_STATED, sku_id=None,
                                                          order_line_id=None, waybill_no=None,
                                                          reason_code=SlotSource.USER_STATED,
                                                          requested_amount_minor=None,
                                                          quantity=SlotSource.USER_STATED,
                                                          product_category=None)),
                    {"request_hash": "x", "response_hash": "y", "usage": {}})
        return (ActionPlan(order_id=self.order_id,
                           actions=[ActionProposal(action=Action.REQUEST_REFUND_ONLY, order_id=self.order_id,
                                                   sku_ids=[self.sku_id], reason_code=self.reason_code)],
                           rationale="政策允许仅退款"),
                {"request_hash": "x", "response_hash": "y", "usage": {}})


def _summary(result: dict, evidence: dict) -> dict:
    decision = result.get("policy_decision") or {}
    return {"intent": (result.get("intent_result") or {}).get("intent"),
            "policy": decision.get("decision"), "reason_code": decision.get("reason_code"),
            "risk": result.get("risk_decision"), "status": result.get("response_status"),
            "evidence_status": evidence.get("status"), "evidence_items": len(evidence.get("items") or []),
            "refund_request_id": result.get("refund_request_id")}


async def _run_case(deps, *, label: str, order_id: str, sku_id: str, reason_code: str,
                    text: str, auto_approve: bool) -> tuple[dict, dict]:
    """跑一条图用例；auto_approve=True 时自动通过 HITL 审批。"""
    deps.llm = StubLLM(order_id=order_id, sku_id=sku_id, reason_code=reason_code)  # type: ignore[assignment]
    ticket = await deps.persistence.create_ticket(tenant_id="tenant_demo", user_id="u_demo",
                                                  order_id=order_id, intent="UNKNOWN")
    tid = ticket["ticket_id"]
    thread_id = f"th_db_{label}"
    run_id = await deps.persistence.create_run(tenant_id="tenant_demo", ticket_id=tid, thread_id=thread_id)
    graph = build_graph(deps, checkpointer=InMemorySaver())
    state = {"tenant_id": "tenant_demo", "user_id": "u_demo", "ticket_id": tid,
             "thread_id": thread_id, "run_id": run_id, "trace_id": f"tr_{label}",
             "messages": [{"role": "user", "content": text}]}
    cfg = {"configurable": {"thread_id": f"{tid}:{run_id}"}}
    result = await graph.ainvoke(state, config=cfg)
    if auto_approve and "__interrupt__" in result:
        approval_id = result["__interrupt__"][0].value["approval_id"]
        await deps.persistence.decide_approval(tenant_id="tenant_demo", approval_id=approval_id,
                                              decision="APPROVED", reviewer_id="verify")
        result = await graph.ainvoke(Command(resume={"approval_id": approval_id, "decision": "APPROVED"}), config=cfg)
    evidence = result.get("evidence_bundle") or {}
    print(f"[{label}] graph:", _summary(result, evidence))
    return result, evidence


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

    # 用例 A（安全语义）：服饰质量问题 + 仅退款 —— 政策只授权“退货退款”，
    # 请求动作不在白名单时必须澄清，不得创建退款申请或资金命令。
    denied, _ = await _run_case(deps, label="apparel_refund_only", order_id="o_1002", sku_id="sku_a1",
                                reason_code="DAMAGED", text="衣服有质量问题想退款", auto_approve=False)
    pd_a = denied.get("policy_decision") or {}
    assert pd_a.get("decision") == "NEED_INFO", "仅退款未获政策授权时应转澄清"
    assert pd_a.get("reason_code") == "ACTION_NOT_AUTHORIZED_BY_POLICY", pd_a.get("reason_code")
    assert denied.get("refund_request_id") is None, "未授权动作不得创建退款申请"
    assert denied.get("response_status") == "WAITING_USER"
    print("SAFETY_OK 未授权的仅退款 -> 澄清，未产生资金命令")

    # 用例 B（可执行链路）：未出库订单取消退款 —— 规则明确授权 REQUEST_REFUND_ONLY，
    # 用于验证「图 -> 退款申请 -> Saga 命令 -> Worker 派发 -> 资金终态」全链路。
    result, ev = await _run_case(deps, label="unshipped_cancel", order_id="o_1006", sku_id="sku_a2",
                                 reason_code="NO_LONGER_NEEDED", text="还没发货，我不想要了，退钱吧",
                                 auto_approve=True)
    pd_b = result.get("policy_decision") or {}
    assert pd_b.get("decision") == "ALLOW", f"未出库取消退款应判 ALLOW，实际 {pd_b.get('decision')}"
    assert pd_b.get("reason_code") == "UNSHIPPED_CANCEL_REFUND", pd_b.get("reason_code")
    assert ev.get("status") == "OK", "证据应闭合（条款齐全）"

    wp = SqlPersistence(make_sessionmaker(make_engine(Settings(database_url=WORKER_DSN))))
    rid = result.get("refund_request_id")
    assert rid, "政策授权的用例未创建退款申请"

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
