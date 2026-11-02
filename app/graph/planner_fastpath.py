# -*- coding: utf-8 -*-
"""ActionPlanner 快路径（确定性直出）：意图明确 + 单动作 + 政策 ALLOW + 证据 OK + 必需槽位齐。

适用场景（省一次 LLM 调用、消除规划抖动）：
  REFUND_ONLY / RETURN_REFUND / INTERCEPT_REFUND / RESHIPMENT / EXPEDITE 且主意图动作唯一。
任一条件不满足即返回 None，交由 LLM 规划（复杂/多意图诉求）。
"""
from __future__ import annotations

from app.domain import action_registry
from app.domain.enums import Action, EvidenceStatus, Intent
from app.domain.schemas import ActionPlan, ActionProposal

INTENT_PRIMARY_ACTION: dict[Intent, Action] = {
    Intent.REFUND_ONLY: Action.REQUEST_REFUND_ONLY,
    Intent.RETURN_REFUND: Action.REQUEST_RETURN_REFUND,
    Intent.INTERCEPT_REFUND: Action.INTERCEPT_PARCEL,
    Intent.RESHIPMENT: Action.CREATE_RESHIPMENT,
    Intent.EXPEDITE: Action.EXPEDITE_DELIVERY,
}


def _slot_values(state: dict) -> dict[str, str | None]:
    slots = state.get("slots") or {}
    order = state.get("order_snapshot") or {}
    logistics = state.get("logistics_snapshot") or {}
    return {
        "order_id": slots.get("order_id") or order.get("order_id"),
        "reason_code": slots.get("reason_code"),
        "sku_id": _unambiguous_sku(state),  # 与快路径一致：仅唯一/已指明时才可用
        "waybill_no": slots.get("waybill_no") or logistics.get("waybill_no"),
    }


def _unambiguous_sku(state: dict) -> str | None:
    """返回无歧义的 SKU：仅当用户已指明，或订单只有唯一一行时可用；否则 None。"""
    sku = (state.get("slots") or {}).get("sku_id")
    if sku:
        return sku
    lines = (state.get("order_snapshot") or {}).get("lines") or []
    if len(lines) == 1 and lines[0].get("sku_id"):
        return lines[0].get("sku_id")
    return None


def try_fastplan(state: dict, *, enabled: bool = True) -> ActionPlan | None:
    if not enabled:
        return None
    intent = Intent((state.get("intent_result") or {}).get("intent", "UNKNOWN"))
    action = INTENT_PRIMARY_ACTION.get(intent)
    if action is None:
        return None
    policy = state.get("policy_decision") or {}
    if policy.get("decision") != "ALLOW":
        return None
    # 动作级授权：政策只授权了别的动作时，快路径必须放弃（交澄清/人工），不能“就近”执行
    if action.value not in set(policy.get("authorized_actions") or []):
        return None
    if state.get("evidence_status") not in (None, EvidenceStatus.OK.value):
        return None

    values = _slot_values(state)
    order_id = values.get("order_id")
    if not order_id:
        return None
    mspec = action_registry.spec(action)
    missing = [s for s in (mspec.required_slots if mspec else ()) if not values.get(s)]
    if missing:
        return None

    # 退款/补发必须绑定"无歧义"的 SKU（用户已指明，或订单只有唯一一行）；否则交 LLM 规划。
    needs_sku = action in (Action.REQUEST_REFUND_ONLY, Action.REQUEST_RETURN_REFUND,
                           Action.CREATE_RESHIPMENT)
    sku_id = _unambiguous_sku(state)
    if needs_sku and not sku_id:
        return None
    sku_ids = [sku_id] if (sku_id and needs_sku) else []
    proposal = ActionProposal(action=action, order_id=order_id, sku_ids=sku_ids,
                              reason_code=values.get("reason_code") or "",
                              waybill_no=values.get("waybill_no") or None)
    return ActionPlan(order_id=order_id, actions=[proposal],
                      rationale=f"确定性快路径：intent={intent} -> {action.value}", source="fastpath")