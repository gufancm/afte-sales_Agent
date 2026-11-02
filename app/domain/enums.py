"""领域枚举与状态（LLD §2.4 / §3 / §7.4.1）。"""
from __future__ import annotations

from enum import StrEnum


class Intent(StrEnum):
    REFUND_ONLY = "REFUND_ONLY"
    RETURN_REFUND = "RETURN_REFUND"
    INTERCEPT_REFUND = "INTERCEPT_REFUND"
    RESHIPMENT = "RESHIPMENT"
    EXPEDITE = "EXPEDITE"
    STATUS_QUERY = "STATUS_QUERY"
    COMPLAINT = "COMPLAINT"
    UNKNOWN = "UNKNOWN"


class SpeechAct(StrEnum):
    """言语行为：区分"我要办"和"我想知道能不能办"。

    咨询资格（ELIGIBILITY_QUERY）不是执行申请：可以走政策查询并给出依据，
    但不得生成任何写动作或资金命令。
    """

    DECIDE = "DECIDE"
    ELIGIBILITY_QUERY = "ELIGIBILITY_QUERY"


class SlotSource(StrEnum):
    USER_STATED = "USER_STATED"
    CONTEXT_INFERRED = "CONTEXT_INFERRED"
    SYSTEM_CONFIRMED = "SYSTEM_CONFIRMED"


class SafetyFlag(StrEnum):
    PROMPT_INJECTION = "PROMPT_INJECTION"
    TOOL_OR_PERMISSION_REQUEST = "TOOL_OR_PERMISSION_REQUEST"
    PII_OR_CREDENTIAL = "PII_OR_CREDENTIAL"
    ABUSE = "ABUSE"
    LEGAL_OR_REGULATORY_THREAT = "LEGAL_OR_REGULATORY_THREAT"
    SELF_HARM_OR_SAFETY = "SELF_HARM_OR_SAFETY"
    OFF_TOPIC = "OFF_TOPIC"
    AMBIGUOUS = "AMBIGUOUS"


class Action(StrEnum):
    REQUEST_REFUND_ONLY = "REQUEST_REFUND_ONLY"
    REQUEST_RETURN_REFUND = "REQUEST_RETURN_REFUND"
    APPROVE_RETURN = "APPROVE_RETURN"
    REJECT_RETURN = "REJECT_RETURN"
    PROVIDE_RETURN_INSTRUCTIONS = "PROVIDE_RETURN_INSTRUCTIONS"
    REGISTER_RETURN_SHIPMENT = "REGISTER_RETURN_SHIPMENT"
    RECEIVE_RETURN = "RECEIVE_RETURN"
    RECORD_INSPECTION = "RECORD_INSPECTION"
    INTERCEPT_PARCEL = "INTERCEPT_PARCEL"
    CREATE_RESHIPMENT = "CREATE_RESHIPMENT"
    EXPEDITE_DELIVERY = "EXPEDITE_DELIVERY"
    ESCALATE_MANUAL = "ESCALATE_MANUAL"
    CLOSE_TICKET = "CLOSE_TICKET"


class RiskDecision(StrEnum):
    DENY = "DENY"
    AUTO_APPROVE = "AUTO_APPROVE"
    REVIEW = "REVIEW"
    MANUAL = "MANUAL"
    NEED_INFO = "NEED_INFO"


class EvidenceStatus(StrEnum):
    OK = "OK"
    INSUFFICIENT = "INSUFFICIENT"
    CONFLICT = "CONFLICT"
    STALE = "STALE"


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    WAITING_USER = "WAITING_USER"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TicketStatus(StrEnum):
    NEW = "NEW"
    NEED_INFO = "NEED_INFO"
    POLICY_REVIEW = "POLICY_REVIEW"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    PROCESSING = "PROCESSING"
    PENDING_RECONCILE = "PENDING_RECONCILE"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    MANUAL = "MANUAL"
    CLOSED = "CLOSED"


class RefundStatus(StrEnum):
    CREATED = "CREATED"
    QUOTED = "QUOTED"
    AUTHORIZED = "AUTHORIZED"
    PREPARED = "PREPARED"
    IN_FLIGHT = "IN_FLIGHT"
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"
    SUCCEEDED = "SUCCEEDED"
    SETTLED = "SETTLED"
    FAILED_CONFIRMED = "FAILED_CONFIRMED"
    MANUAL_RECONCILE = "MANUAL_RECONCILE"
    CANCELLED = "CANCELLED"


class CommandStatus(StrEnum):
    PREPARED = "PREPARED"
    CLAIMED = "CLAIMED"
    IN_FLIGHT = "IN_FLIGHT"
    CONFIRMED = "CONFIRMED"
    FAILED_CONFIRMED = "FAILED_CONFIRMED"
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"
    CANCELLED = "CANCELLED"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class RefundMethod(StrEnum):
    ORIGINAL_PAYMENT = "ORIGINAL_PAYMENT"
    STORE_CREDIT = "STORE_CREDIT"
    BANK_TRANSFER = "BANK_TRANSFER"
    OTHER = "OTHER"


# 意图 → 允许动作白名单（LLD §2.4.6）
INTENT_ALLOWED_ACTIONS: dict[Intent, frozenset[Action]] = {
    Intent.REFUND_ONLY: frozenset({Action.REQUEST_REFUND_ONLY, Action.ESCALATE_MANUAL, Action.CLOSE_TICKET}),
    Intent.RETURN_REFUND: frozenset({
        Action.REQUEST_RETURN_REFUND, Action.APPROVE_RETURN, Action.REJECT_RETURN,
        Action.PROVIDE_RETURN_INSTRUCTIONS, Action.REGISTER_RETURN_SHIPMENT,
        Action.RECEIVE_RETURN, Action.RECORD_INSPECTION, Action.ESCALATE_MANUAL, Action.CLOSE_TICKET,
    }),
    Intent.INTERCEPT_REFUND: frozenset({Action.INTERCEPT_PARCEL, Action.ESCALATE_MANUAL, Action.CLOSE_TICKET}),
    Intent.RESHIPMENT: frozenset({Action.CREATE_RESHIPMENT, Action.ESCALATE_MANUAL, Action.CLOSE_TICKET}),
    Intent.EXPEDITE: frozenset({Action.EXPEDITE_DELIVERY, Action.ESCALATE_MANUAL, Action.CLOSE_TICKET}),
    Intent.STATUS_QUERY: frozenset({Action.ESCALATE_MANUAL, Action.CLOSE_TICKET}),
    Intent.COMPLAINT: frozenset({Action.ESCALATE_MANUAL, Action.CLOSE_TICKET}),
    Intent.UNKNOWN: frozenset(),
}

class Category(StrEnum):
    """售后品类受控词表：与 policy/rules.json 中 order.category 取值一一对应。

    未识别的品类一律归一为 UNKNOWN，由政策引擎按 fail-closed 转人工，
    绝不因为拼写不一致而静默落到通用规则。
    """

    APPAREL = "APPAREL"
    FRESH = "FRESH"
    CUSTOM = "CUSTOM"
    DIGITAL = "DIGITAL"
    APPLIANCE = "APPLIANCE"
    VIRTUAL = "VIRTUAL"
    BOOK_MEDIA = "BOOK_MEDIA"
    GOLD = "GOLD"
    JEWELRY = "JEWELRY"
    EBIKE = "EBIKE"
    BAKERY = "BAKERY"
    GROCERY = "GROCERY"
    UNKNOWN = "UNKNOWN"


_CATEGORY_ALIASES: dict[str, str] = {
    "服饰": "APPAREL", "服装": "APPAREL", "鞋靴": "APPAREL", "apparel": "APPAREL",
    "生鲜": "FRESH", "水果": "FRESH", "fresh": "FRESH",
    "定制": "CUSTOM", "定做": "CUSTOM", "custom": "CUSTOM",
    "数码": "DIGITAL", "3c": "DIGITAL", "digital": "DIGITAL",
    "家电": "APPLIANCE", "appliance": "APPLIANCE",
    "虚拟": "VIRTUAL", "virtual": "VIRTUAL",
    "图书": "BOOK_MEDIA", "音像": "BOOK_MEDIA", "book_media": "BOOK_MEDIA",
    "黄金": "GOLD", "gold": "GOLD",
    "珠宝": "JEWELRY", "首饰": "JEWELRY", "jewelry": "JEWELRY",
    "电动车": "EBIKE", "ebike": "EBIKE",
    "烘焙": "BAKERY", "bakery": "BAKERY",
    "超市": "GROCERY", "食品": "GROCERY", "grocery": "GROCERY",
}


def normalize_category(raw: object) -> str:
    """把外部传入的品类归一为受控取值；无法识别返回 UNKNOWN。"""
    if raw is None:
        return Category.UNKNOWN.value
    text = str(raw).strip()
    if not text:
        return Category.UNKNOWN.value
    upper = text.upper().replace("-", "_").replace(" ", "_")
    if upper in Category.__members__:
        return upper
    mapped = _CATEGORY_ALIASES.get(text) or _CATEGORY_ALIASES.get(text.lower())
    if mapped:
        return mapped
    return Category.UNKNOWN.value
