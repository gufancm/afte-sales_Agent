# -*- coding: utf-8 -*-
"""动作注册表（Action Registry）：每个动作的结构化元数据。

单一元数据源，供 ActionPlanner（选动作）、计划 critic（校验槽位）、
风控（风险级别）、执行前复核（前置条件）复用，避免"动作语义散落在
prompt / 风控 / 节点里各写一份"（LLD §2.4/§4.5）。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.enums import Action

WRITE_REFUND = "WRITE_REFUND"
WRITE_LOGISTICS = "WRITE_LOGISTICS"
HUMAN = "HUMAN"
NOOP = "NOOP"

FINANCIAL = "FINANCIAL"
LOGISTICS = "LOGISTICS"
RISK_HUMAN = "HUMAN"
RISK_NONE = "NONE"


@dataclass(frozen=True)
class ActionSpec:
    action: Action
    kind: str
    risk_class: str
    required_slots: tuple[str, ...] = ()
    idempotency_template: str = ""
    preconditions: tuple[str, ...] = ()
    description: str = ""
    # 命令派发优先级（数字越小越先派发）；资金类高于物流类
    priority: int = 100

    @property
    def is_external_write(self) -> bool:
        return self.kind in (WRITE_REFUND, WRITE_LOGISTICS)

    @property
    def is_refund_family(self) -> bool:
        return self.kind == WRITE_REFUND


_SPECS: dict[Action, ActionSpec] = {
    Action.REQUEST_REFUND_ONLY: ActionSpec(
        Action.REQUEST_REFUND_ONLY, WRITE_REFUND, FINANCIAL, ("order_id",),
        "refund:{ticket_id}:{order_id}", ("policy_allow", "order_exists", "quote_required"),
        "仅退款（不退货）", priority=10),
    Action.REQUEST_RETURN_REFUND: ActionSpec(
        Action.REQUEST_RETURN_REFUND, WRITE_REFUND, FINANCIAL, ("order_id", "reason_code"),
        "refund:{ticket_id}:{order_id}", ("policy_allow", "order_exists", "reason_code", "quote_required"),
        "退货退款", priority=10),
    Action.INTERCEPT_PARCEL: ActionSpec(
        Action.INTERCEPT_PARCEL, WRITE_LOGISTICS, LOGISTICS, ("order_id", "waybill_no"),
        "intercept:{ticket_id}:{waybill_no}", ("parcel_in_transit", "waybill_owned"),
        "拦截包裹", priority=20),
    Action.CREATE_RESHIPMENT: ActionSpec(
        Action.CREATE_RESHIPMENT, WRITE_LOGISTICS, LOGISTICS, ("order_id", "sku_id"),
        "reship:{ticket_id}:{order_id}:{sku_id}", ("sku_in_order", "stock_available"),
        "补发商品", priority=20),
    Action.EXPEDITE_DELIVERY: ActionSpec(
        Action.EXPEDITE_DELIVERY, WRITE_LOGISTICS, LOGISTICS, ("order_id",),
        "expedite:{ticket_id}:{waybill_no}", ("parcel_not_delivered",),
        "催派/加急", priority=30),
    Action.APPROVE_RETURN: ActionSpec(Action.APPROVE_RETURN, HUMAN, RISK_HUMAN, (), "", (), "人工批准退货"),
    Action.REJECT_RETURN: ActionSpec(Action.REJECT_RETURN, HUMAN, RISK_HUMAN, (), "", (), "人工驳回退货"),
    Action.PROVIDE_RETURN_INSTRUCTIONS: ActionSpec(
        Action.PROVIDE_RETURN_INSTRUCTIONS, HUMAN, RISK_HUMAN, (), "", (), "提供退货指引"),
    Action.REGISTER_RETURN_SHIPMENT: ActionSpec(
        Action.REGISTER_RETURN_SHIPMENT, HUMAN, RISK_HUMAN, (), "", (), "登记退货运单"),
    Action.RECEIVE_RETURN: ActionSpec(Action.RECEIVE_RETURN, HUMAN, RISK_HUMAN, (), "", (), "收货入库"),
    Action.RECORD_INSPECTION: ActionSpec(Action.RECORD_INSPECTION, HUMAN, RISK_HUMAN, (), "", (), "质检记录"),
    Action.ESCALATE_MANUAL: ActionSpec(Action.ESCALATE_MANUAL, NOOP, RISK_NONE, (), "", (), "转人工"),
    Action.CLOSE_TICKET: ActionSpec(Action.CLOSE_TICKET, NOOP, RISK_NONE, (), "", (), "关闭工单"),
}


def spec(action: Action | str | None) -> ActionSpec | None:
    if action is None:
        return None
    try:
        return _SPECS.get(Action(action))
    except ValueError:
        return None


def is_external_write(action: Action | str | None) -> bool:
    s = spec(action)
    return bool(s and s.is_external_write)


def is_refund_family(action: Action | str | None) -> bool:
    s = spec(action)
    return bool(s and s.is_refund_family)


def required_slots(action: Action | str | None) -> tuple[str, ...]:
    s = spec(action)
    return s.required_slots if s else ()


def priority_of(action: Action | str | None) -> int:
    s = spec(action)
    return s.priority if s else 100


def external_write_actions() -> frozenset[str]:
    return frozenset(a.value for a, s in _SPECS.items() if s.is_external_write)


def all_specs() -> list[ActionSpec]:
    return list(_SPECS.values())