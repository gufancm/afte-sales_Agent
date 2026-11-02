# -*- coding: utf-8 -*-
"""本地假 OpenAI 兼容端点（仅开发/联调）：给"接入 LLM"做无外网、无密钥的链路验证。

按请求里的 response_format.json_schema.name 返回合法 JSON（IntentResult / ActionPlan）。

用法（容器内）：
  docker run -d --name fake-llm --network aftersale-agent_default \
    -v E:/agent/aftersale-agent:/app -w /app aftersale-agent-api \
    python -m uvicorn --app-dir scripts --host 0.0.0.0 --port 8899 fake_openai_server:fake
"""
from __future__ import annotations

import json
import time

from fastapi import FastAPI, Request

fake = FastAPI(title="fake-openai")

INTENT_JSON = {
    "intent": "REFUND_ONLY", "secondary_intents": [], "confidence": 0.95,
    "slots": {"order_id": "o_1002", "sku_id": "sku_a1", "order_line_id": None, "waybill_no": None,
              "reason_code": "DAMAGED", "requested_amount_minor": None, "quantity": 1,
              "product_category": "APPAREL"},
    "slot_sources": {"order_id": "USER_STATED", "sku_id": "USER_STATED", "order_line_id": None,
                     "waybill_no": None, "reason_code": "USER_STATED", "requested_amount_minor": None,
                     "quantity": "USER_STATED", "product_category": "USER_STATED"},
    "missing_slots": [], "safety_flags": [], "language": "zh",
    "rationale_short": "用户称衣服有质量问题，要求退款",
}

PLAN_JSON = {
    "order_id": "o_1002",
    "actions": [{"action": "REQUEST_REFUND_ONLY", "order_id": "o_1002", "sku_ids": ["sku_a1"],
                 "waybill_no": None, "reason_code": "DAMAGED", "evidence_ids": [],
                 "requested_amount_minor": None}],
    "rationale": "政策允许质量问题仅退款", "needs_more_info": False, "missing_information": [],
}


@fake.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "fake-openai"}


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