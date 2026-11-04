# -*- coding: utf-8 -*-
"""确定性风控规则（LLD §4.5）。

原则：
1. 按动作类型选择规则：退款 / 拦截 / 补发 / 催派的风险面与前置信条件不同；
2. **缺少适用规则、或缺少判定所需权威事实时一律 MANUAL，绝不默认 AUTO_APPROVE**
   —— 没有金额不等于没有风险（补发有库存损失，拦截影响履约与赔付）；
3. 只消费 State 中由可信端口（OMS/TMS/Finance/Policy）确认的事实，不读自由文本、不调 LLM。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.enums import RiskDecision

REFUND_ACTIONS = ("REQUEST_REFUND_ONLY", "REQUEST_RETURN_REFUND")
DELIVERED_STATES = ("SIGNED", "DELIVERED", "RECEIVED", "COMPLETED")


@dataclass(frozen=True)
class RiskOutcome:
    decision: RiskDecision
    reason_codes: list[str] = field(default_factory=list)


def _out(decision: RiskDecision, *codes: str) -> RiskOutcome:
    return RiskOutcome(decision, [c for c in codes if c])


def _approve(*codes: str) -> RiskOutcome:
    return _out(RiskDecision.AUTO_APPROVE, *codes)


def _review(*codes: str) -> RiskOutcome:
    return _out(RiskDecision.REVIEW, *codes)


def _deny(*codes: str) -> RiskOutcome:
    return _out(RiskDecision.DENY, *codes)


def _manual(*codes: str) -> RiskOutcome:
    return _out(RiskDecision.MANUAL, *codes)


def _order_snapshot(state: dict) -> dict:
    return state.get("order_snapshot") or {}


def _order_lines(state: dict) -> list[dict]:
    return list(_order_snapshot(state).get("lines") or [])


def _parcel_status(state: dict) -> str | None:
    return (state.get("logistics_snapshot") or {}).get("parcel_status")


def _waybill_no(state: dict) -> str | None:
    return (state.get("logistics_snapshot") or {}).get("waybill_no") or (state.get("slots") or {}).get("waybill_no")


def requested_sku_ids(state: dict) -> list[str]:
    """本次计划中申请操作的 SKU（来自 ActionPlan，不来自自由文本）。"""
    skus: list[str] = []
    for a in (state.get("action_plan") or {}).get("actions", []):
        for s in (a.get("sku_ids") or []):
            if s and s not in skus:
                skus.append(s)
    return skus


def requested_quantity(state: dict) -> int:
    return int((state.get("slots") or {}).get("quantity") or 1)


def _evaluate_refund(state: dict, params: dict | None = None) -> RiskOutcome:
    """退款类：金额必须已由 Finance 报价；超余额/超政策上限拒绝；上限缺失转人审。

    维度（params 非空时启用，生产路径始终传入）：
      - 买家退款历史（频次 / 近 30 天累计金额 / 黑名单）来自 `state["risk_facts"]`；
      - **缺失或降级 -> MANUAL**（不默认放行）；黑名单 -> DENY（可配置）。
    """
    validated = state.get("validated_decision") or {}
    if validated:
        cap = validated.get("amount_cap_minor")
        amount = validated.get("validated_amount_minor")
    else:
        cap = ((state.get("policy_decision") or {}).get("amount_cap") or {}).get("cap_minor")
        amount = state.get("quote_amount_minor")
    if amount is None:
        return _manual("REFUND_QUOTE_MISSING")
    fin = state.get("finance_snapshot") or {}
    balance = fin.get("refundable_balance_minor")
    if balance is None:
        return _manual("FINANCE_BALANCE_UNKNOWN")
    reserved = fin.get("reserved_amount_minor") or 0
    if amount > (balance - reserved):
        return _deny("BALANCE_INSUFFICIENT")
    if cap is None:
        return _review("NO_CAP_DEFINED")
    if amount > cap:
        return _deny("OVER_POLICY_CAP")
    if params is not None:
        stats = state.get("risk_facts") or {}
        if stats.get("degraded") or stats.get("refund_count_30d") is None:
            return _manual("REFUND_STATS_UNAVAILABLE")
        if stats.get("blacklisted") and params.get("blacklist_deny", True):
            return _deny("BUYER_BLACKLISTED")
        count = int(stats.get("refund_count_30d") or 0)
        history = int(stats.get("refund_amount_30d_minor") or 0)
        if (count >= int(params.get("max_refund_count_30d", 5))
                or (history + amount) > int(params.get("max_refund_amount_30d_minor", 500000))):
            return _review("REFUND_FREQUENCY_HIGH")
    return _approve("WITHIN_CAP_AND_BALANCE")


def _evaluate_intercept(state: dict) -> RiskOutcome:
    if not _waybill_no(state):
        return _manual("WAYBILL_MISSING")
    status = _parcel_status(state)
    if status is None or status == "UNKNOWN":
        return _manual("LOGISTICS_STATUS_UNKNOWN")
    if status in DELIVERED_STATES:
        return _deny("ALREADY_DELIVERED")
    if (state.get("logistics_snapshot") or {}).get("intercept_command_status") in ("ACCEPTED", "IN_PROGRESS"):
        return _manual("INTERCEPT_ALREADY_IN_FLIGHT")
    return _approve("IN_TRANSIT_INTERCEPT")


def _evaluate_expedite(state: dict) -> RiskOutcome:
    if not _waybill_no(state):
        return _manual("WAYBILL_MISSING")
    status = _parcel_status(state)
    if status is None or status == "UNKNOWN":
        return _manual("LOGISTICS_STATUS_UNKNOWN")
    if status in DELIVERED_STATES:
        return _deny("ALREADY_DELIVERED")
    return _approve("EXPEDITE_IN_TRANSIT")


def _evaluate_reshipment(state: dict) -> RiskOutcome:
    """补发：核对 SKU 与数量；库存占用/收货地址/异常频次缺少权威事实来源 -> 人工确认。"""
    skus = requested_sku_ids(state)
    if not skus:
        return _manual("SKU_MISSING")
    lines = _order_lines(state)
    if not lines:
        return _manual("ORDER_LINES_UNAVAILABLE")
    by_sku: dict[str, list[dict]] = {}
    for ln in lines:
        by_sku.setdefault(ln.get("sku_id"), []).append(ln)
    qty = requested_quantity(state)
    for s in skus:
        matched = by_sku.get(s)
        if not matched:
            return _deny("SKU_NOT_IN_ORDER")
        if qty > sum(int(ln.get("quantity") or 0) for ln in matched):
            return _deny("QUANTITY_EXCEEDS_ORDER")
    return _review("RESHIPMENT_NEEDS_HUMAN_CONFIRM")


_EVALUATORS = {
    "REQUEST_REFUND_ONLY": _evaluate_refund,
    "REQUEST_RETURN_REFUND": _evaluate_refund,
    "INTERCEPT_PARCEL": _evaluate_intercept,
    "EXPEDITE_DELIVERY": _evaluate_expedite,
    "CREATE_RESHIPMENT": _evaluate_reshipment,
}


def evaluate_risk(action: str | None, state: dict, params: dict | None = None) -> RiskOutcome:
    """按动作取风控结论；无适用规则 -> MANUAL（不得默认放行）。

    params 非空表示启用买家历史维度（退款类）；为 None 时保持核心规则（供单元测试直接调用）。
    """
    if not action:
        return _manual("NO_ACTION_TO_AUTHORIZE")
    evaluator = _EVALUATORS.get(action)
    if evaluator is None:
        return _manual("NO_RISK_RULE_FOR_ACTION")
    if action in REFUND_ACTIONS:
        return evaluator(state, params)  # type: ignore[call-arg]
    return evaluator(state)


def has_rule_for(action: str | None) -> bool:
    return bool(action) and action in _EVALUATORS
