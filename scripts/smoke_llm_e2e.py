"""接入 LLM 后的联调冒烟：用一个"OpenAI 兼容"的本地假端点，验证整条链路真的能跑通。

目的：证明"填好 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 就能直接用"，
而不只是配置项存在。做法：
  1) 起一个本地假 /v1/chat/completions（按请求里的 response_format.json_schema.name 返回合法 JSON）；
  2) 用真实 app.llm.adapter.OpenAICompatibleLLM 走 OpenAI SDK 调用它（结构化输出 + 审计）；
  3) 跑完整图：意图 -> 政策 -> 证据 -> 计划 -> 报价 -> 风控 -> HITL 审批 -> 生成 Saga 命令。

用法：docker compose run --rm api python scripts/smoke_llm_e2e.py
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import httpx
import uvicorn
from fastapi import FastAPI, Request
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.bootstrap import build_deps
from app.config import Settings
from app.graph.build import build_graph

PORT = 8899
BASE_URL = f"http://127.0.0.1:{PORT}/v1"

INTENT_JSON = {
    "intent": "REFUND_ONLY",
    "secondary_intents": [],
    "confidence": 0.95,
    "slots": {"order_id": "o_1002", "sku_id": "sku_a1", "order_line_id": None, "waybill_no": None,
              "reason_code": "DAMAGED", "requested_amount_minor": None, "quantity": 1,
              "product_category": "APPAREL"},
    "slot_sources": {"order_id": "USER_STATED", "sku_id": "USER_STATED", "order_line_id": None,
                     "waybill_no": None, "reason_code": "USER_STATED", "requested_amount_minor": None,
                     "quantity": "USER_STATED", "product_category": "USER_STATED"},
    "missing_slots": [],
    "safety_flags": [],
    "language": "zh",
    "rationale_short": "用户称衣服有质量问题，要求退款",
}

PLAN_JSON = {
    "order_id": "o_1002",
    "actions": [{"action": "REQUEST_REFUND_ONLY", "order_id": "o_1002", "sku_ids": ["sku_a1"],
                 "waybill_no": None, "reason_code": "DAMAGED", "evidence_ids": [],
                 "requested_amount_minor": None}],
    "rationale": "政策允许质量问题仅退款",
    "needs_more_info": False,
    "missing_information": [],
}

fake = FastAPI()


@fake.post("/v1/chat/completions")
async def chat_completions(request: Request) -> dict:
    body = await request.json()
    rf = body.get("response_format") or {}
    name = (rf.get("json_schema") or {}).get("name") if isinstance(rf, dict) else None
    if name is None:  # json_object 模式：按注入的 schema 文本判断目标模型
        blob = json.dumps(body.get("messages") or [], ensure_ascii=False)
        name = "ActionPlan" if "ActionPlan" in blob else "IntentResult"
    payload = INTENT_JSON if name == "IntentResult" else PLAN_JSON
    return {
        "id": "chatcmpl-fake", "object": "chat.completion", "created": int(time.time()),
        "model": body.get("model", "fake-model"),
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 60, "total_tokens": 180},
    }


def serve_fake_llm() -> threading.Thread:
    server = uvicorn.Server(uvicorn.Config(fake, host="127.0.0.1", port=PORT, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(50):
        try:
            if httpx.get(f"{BASE_URL}/../health", timeout=0.5).status_code < 500:
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    return th


async def main() -> None:
    serve_fake_llm()
    settings = Settings(app_env="test", database_url="memory://", rag_backend="memory",
                        llm_base_url=BASE_URL, llm_api_key="fake-key", llm_model="gpt-fake",
                        llm_context_window=32768, llm_max_output_tokens=4096, llm_model_small="")
    assert not settings.startup_violations(), settings.startup_violations()
    deps = build_deps(settings)
    assert deps.llm is not None, "LLM 未启用"
    print("llm_enabled:", settings.llm_enabled, "base_url:", settings.llm_base_url, "model:", settings.llm_model)

    graph = build_graph(deps, checkpointer=InMemorySaver())
    ticket = await deps.persistence.create_ticket(tenant_id="tenant_demo", user_id="u_demo",
                                                 order_id="o_1002", intent="UNKNOWN")
    tid = ticket["ticket_id"]
    run_id = await deps.persistence.create_run(tenant_id="tenant_demo", ticket_id=tid, thread_id="th_llm")
    state = {"tenant_id": "tenant_demo", "user_id": "u_demo", "ticket_id": tid, "thread_id": "th_llm",
             "run_id": run_id, "trace_id": "tr_llm",
             "messages": [{"role": "user", "content": "我的衣服到货就是破的，要退款"}]}
    cfg = {"configurable": {"thread_id": f"{tid}:{run_id}"}}
    result = await graph.ainvoke(state, config=cfg)
    print("intent:", (result.get("intent_result") or {}).get("intent"),
          "| policy:", (result.get("policy_decision") or {}).get("decision"),
          (result.get("policy_decision") or {}).get("reason_code"),
          "| risk:", result.get("risk_decision"), "| status:", result.get("response_status"))

    if "__interrupt__" in result:
        approval_id = result["__interrupt__"][0].value["approval_id"]
        await deps.persistence.decide_approval(tenant_id="tenant_demo", approval_id=approval_id,
                                              decision="APPROVED", reviewer_id="smoke")
        result = await graph.ainvoke(Command(resume={"approval_id": approval_id, "decision": "APPROVED"}), config=cfg)

    pd = result.get("policy_decision") or {}
    assert pd.get("decision") == "ALLOW", f"应有 ALLOW，实际 {pd}"
    assert ev_ok(result), "证据应闭合"
    assert result.get("response_status") == "WAITING_EXTERNAL", result.get("response_status")
    assert len(deps.persistence.commands) == 1, "应生成 1 条 Saga 命令"
    nodes_called = sorted({c["node"] for c in deps.persistence.llm_calls})
    print("llm_calls:", len(deps.persistence.llm_calls), "nodes:", nodes_called)
    assert "intent_guardrail" in nodes_called and "action_planner" in nodes_called
    print("final_response:", (result.get("final_response") or "")[:60])
    print("LLM_E2E_OK")


def ev_ok(result: dict) -> bool:
    status = result.get("evidence_status") or (result.get("evidence_bundle") or {}).get("status")
    return status == "OK"


if __name__ == "__main__":
    asyncio.run(main())