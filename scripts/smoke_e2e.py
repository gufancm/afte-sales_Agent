"""端到端冒烟：API 路径 + 退款 Saga 路径（用 stub LLM 打通图）。
用法：python scripts/smoke_e2e.py
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = "memory://"
os.environ["LLM_MODEL"] = ""

from app.domain.enums import Action, Intent, SlotSource  # noqa: E402
from app.domain.schemas import ActionPlan, ActionProposal, ExtractedSlots, IntentResult, SlotSources  # noqa: E402


class StubLLM:
    """按 schema 返回固定结构化结果，替代真实模型。"""

    async def structured(self, *, messages, schema_model, prompt_version, temperature=0.0):
        if schema_model is IntentResult:
            return (
                IntentResult(
                    intent=Intent.REFUND_ONLY, confidence=0.95,
                    slots=ExtractedSlots(order_id="o_1002", reason_code="DAMAGED", quantity=1),
                    slot_sources=SlotSources(order_id=SlotSource.USER_STATED, sku_id=None, order_line_id=None,
                                             waybill_no=None, reason_code=SlotSource.USER_STATED,
                                             requested_amount_minor=None, quantity=SlotSource.USER_STATED,
                                             product_category=None),
                ),
                {"request_hash": "sha256:x", "response_hash": "sha256:y", "usage": {"prompt_tokens": 100, "completion_tokens": 50}},
            )
        return (
            ActionPlan(order_id="o_1002",
                       actions=[ActionProposal(action=Action.REQUEST_REFUND_ONLY, order_id="o_1002",
                                               sku_ids=["sku_a1"], reason_code="DAMAGED")],
                       rationale="政策允许仅退款"),
            {"request_hash": "sha256:x", "response_hash": "sha256:y", "usage": {"prompt_tokens": 80, "completion_tokens": 40}},
        )


def api_smoke() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        print("ready:", c.get("/ready").json())
        h = {"X-Tenant-Id": "tenant_demo", "X-User-Id": "u_demo"}
        r = c.post("/api/v1/tickets", json={"order_id": None, "intent": "UNKNOWN", "initial_message": "你好"},
                   headers={**h, "Idempotency-Key": "k1"})
        assert r.status_code == 200, r.text
        tid = r.json()["ticket_id"]
        r2 = c.post(f"/api/v1/tickets/{tid}/messages", json={"text": "生鲜坏了想退款"}, headers=h)
        assert r2.status_code == 200, r2.text
        print("message run:", {k: r2.json()[k] for k in ("run_status", "response")})
        t = c.get(f"/api/v1/tickets/{tid}", headers=h)
        assert t.status_code == 200
        print("ticket:", {k: t.json()[k] for k in ("intent", "status", "event_sequence")})
        print("API_SMOKE_OK")


async def refund_saga_smoke() -> None:
    from app.bootstrap import build_deps
    from app.config import Settings
    from app.graph.build import build_graph

    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    settings = Settings(app_env="test", database_url="memory://", llm_model="stub", llm_model_small="")
    deps = build_deps(settings)
    deps.llm = StubLLM()  # 注入 stub
    graph = build_graph(deps, checkpointer=InMemorySaver())
    state = {"tenant_id": "tenant_demo", "user_id": "u_demo", "ticket_id": "tk_refund",
             "thread_id": "th_refund", "run_id": "r_refund", "trace_id": "tr_refund",
             "messages": [{"role": "user", "content": "衣服有质量问题想退款"}]}
    cfg = {"configurable": {"thread_id": "tk_refund:r_refund"}}
    result = await graph.ainvoke(state, config=cfg)
    # 真实语料下退款质量规则无金额上限 -> 风控 REVIEW -> HITL；审批通过后继续
    if "__interrupt__" in result:
        approval_id = result["__interrupt__"][0].value["approval_id"]
        await deps.persistence.decide_approval(tenant_id="tenant_demo", approval_id=approval_id,
                                              decision="APPROVED", reviewer_id="smoke")
        result = await graph.ainvoke(Command(resume={"approval_id": approval_id, "decision": "APPROVED"}), config=cfg)
    print("saga run:", {"intent": (result.get("intent_result") or {}).get("intent"),
                        "policy": (result.get("policy_decision") or {}).get("decision"),
                        "risk": result.get("risk_decision"),
                        "quote_id": result.get("quote_id"),
                        "amount_minor": result.get("quote_amount_minor"),
                        "status": result.get("response_status")})
    print("commands enqueued:", len(deps.persistence.commands))
    assert len(deps.persistence.commands) == 1, "应恰好产生 1 条 Saga 命令"
    assert result.get("response_status") == "WAITING_EXTERNAL"
    print("REFUND_SAGA_OK")


if __name__ == "__main__":
    api_smoke()
    asyncio.run(refund_saga_smoke())