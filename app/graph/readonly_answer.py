# -*- coding: utf-8 -*-
"""只读自助答复（混合式）：事实用模板，措辞用 LLM，后置校验，失败回退纯模板。

行业做法（客服机器人）：结构化事实（物流/订单状态）用**确定性模板**组装，保证准确、可审计；
LLM 仅在「已给定事实」范围内做措辞润色（多语言/语气），且必须通过**后置校验**：
  - 不得引入模板之外的数字/日期/承诺；
  - 关键实体（订单号/运单号）必须原样保留；
  - 长度受限，不得编造时限。
任一校验失败即**回退纯模板**（`mode=template`），仍可正常回复。

本模块不直接访问外部系统：事实由 `context_aggregator` 经 ToolGateway 取回后传入。
"""
from __future__ import annotations

import re
from typing import Any

PROMPT_VERSION_READONLY = "readonly-rephrase-v1"

# 订单/包裹状态 -> 中文（与 mock/真实枚举对齐；未知则原样）
_ORDER_STATUS_CN = {
    "PAID_UNSHIPPED": "已付款，待发货",
    "PAID": "已付款",
    "SHIPPED": "已发货",
    "IN_TRANSIT": "运输中",
    "DELIVERED": "已送达",
    "SIGNED": "已签收",
    "COMPLETED": "已完成",
    "CANCELLED": "已取消",
    "CLOSED": "已关闭",
    "REFUNDING": "退款处理中",
}
_PARCEL_STATUS_CN = {
    "IN_TRANSIT": "运输中",
    "OUT_FOR_DELIVERY": "派送中",
    "DELIVERED": "已送达",
    "SIGNED": "已签收",
    "UNKNOWN": "暂未获取到物流信息",
    "PICKED_UP": "已揽收",
    "EXCEPTION": "物流异常",
}
# 禁止出现的「承诺/时限」类词（只读答复不得给承诺）
_FORBIDDEN_PROMISES = ("保证", "一定", "100%", "百分之", "立即到账", "秒退", "赔付", "赔偿")
_ENTITY_RE = re.compile(r"\b(?:o_[A-Za-z0-9]+|wb_[A-Za-z0-9]+)\b")


def _order_cn(status: str | None) -> str:
    return _ORDER_STATUS_CN.get(status or "", status or "未知")


def _parcel_cn(status: str | None) -> str:
    return _PARCEL_STATUS_CN.get(status or "", status or "未知")


def render_fact_template(intent: Any, *, order: Any = None, logistics: Any = None,
                         degraded: dict | None = None) -> str:
    """确定性事实陈述（模板）。这是唯一的事实来源，LLM 只能在此范围内措辞。"""
    degraded = degraded or {}
    if str(intent) == "COMPLAINT":
        parts = ["已收到您的反馈，我们已记录并会尽快核实；如需人工介入，我们会安排专员跟进。"]
        if order is not None:
            parts.append(f"涉及订单 {order.order_id}。")
        return "".join(parts)

    parts = []
    if order is not None:
        parts.append(f"您的订单 {order.order_id} 当前状态：{_order_cn(order.status)}。")
    if logistics is not None and (logistics.waybill_no or logistics.parcel_status):
        if logistics.waybill_no:
            parts.append(f"运单号 {logistics.waybill_no}，物流状态：{_parcel_cn(logistics.parcel_status)}。")
        else:
            parts.append(f"物流状态：{_parcel_cn(logistics.parcel_status)}。")
    elif "logistics" in degraded:
        parts.append("暂时无法获取物流信息，请稍后再试。")
    else:
        parts.append("暂未查询到物流轨迹，请稍后再试。")
    return "".join(parts) if parts else "已收到您的查询，我们正在为您核实。"


def _digit_runs(text: str) -> set[str]:
    return set(re.findall(r"\d+", text or ""))


def _allowed_numbers(order: Any, logistics: Any) -> set[str]:
    allowed: set[str] = set()
    if order is not None:
        allowed |= _digit_runs(str(getattr(order, "order_id", "")))
    if logistics is not None:
        allowed |= _digit_runs(str(getattr(logistics, "waybill_no", "") or ""))
    return allowed


def verify_grounded(candidate: str, template: str, allowed_numbers: set[str]) -> tuple[bool, str]:
    """后置校验：LLM 输出不得越出模板事实范围。返回 (是否通过, 原因)。"""
    text = (candidate or "").strip()
    if not text:
        return False, "empty"
    if len(text) > max(200, len(template) * 2):
        return False, "too_long"
    new_numbers = _digit_runs(text) - allowed_numbers
    if new_numbers:
        return False, "new_number:" + ",".join(sorted(new_numbers)[:3])
    for word in _FORBIDDEN_PROMISES:
        if word in text:
            return False, "promise:" + word
    for entity in _ENTITY_RE.findall(template):
        if entity not in text:
            return False, "missing_entity:" + entity
    return True, ""


async def compose_readonly_answer(deps, state, *, intent: Any, order: Any = None, logistics: Any = None,
                                  degraded: dict | None = None) -> tuple[str, dict]:
    """模板 + 可选 LLM 润色（带后置校验）；返回 (最终文案, meta)。"""
    template = render_fact_template(intent, order=order, logistics=logistics, degraded=degraded)
    enabled = getattr(deps.settings, "readonly_llm_rephrase_enabled", True)
    if deps.llm is None or not enabled:
        return template, {"mode": "template", "verified": False, "reason": "llm_disabled"}
    prompt = (getattr(deps, "prompts", None) or {}).get("readonly_rephrase")
    if not prompt:
        return template, {"mode": "template", "verified": False, "reason": "no_prompt"}

    messages = [{"role": "system", "content": prompt},
                {"role": "user", "content": "事实陈述：" + template}]
    try:
        out, audit = await deps.llm.text(messages=messages, prompt_version=PROMPT_VERSION_READONLY)
    except Exception as exc:  # 润色失败绝不影响回复：回退模板
        await deps.persistence.record_llm_call(
            tenant_id=state["tenant_id"], ticket_id=state.get("ticket_id"), run_id=state.get("run_id"),
            trace_id=state.get("trace_id"), node="readonly_answer", model=deps.settings.llm_model,
            prompt_version=PROMPT_VERSION_READONLY, status="ERROR", error_code=str(exc)[:200])
        return template, {"mode": "template", "verified": False, "reason": "llm_error"}

    await deps.persistence.record_llm_call(
        tenant_id=state["tenant_id"], ticket_id=state.get("ticket_id"), run_id=state.get("run_id"),
        trace_id=state.get("trace_id"), node="readonly_answer", model=audit.get("model"),
        prompt_version=PROMPT_VERSION_READONLY, status="SUCCESS",
        request_hash=audit.get("request_hash"), response_hash=audit.get("response_hash"),
        input_tokens=(audit.get("usage") or {}).get("prompt_tokens"),
    output_tokens=(audit.get("usage") or {}).get("completion_tokens"),
    latency_ms=audit.get("latency_ms"))

    ok, reason = verify_grounded(out or "", template, _allowed_numbers(order, logistics))
    if not ok:
        return template, {"mode": "template", "verified": False, "reason": reason}
    return (out or "").strip(), {"mode": "llm", "verified": True}
