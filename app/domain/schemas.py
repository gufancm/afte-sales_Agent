"""结构化输出模型（LLD §2.4 / §2.4.1-§2.4.6）。
所有被程序消费的模型：extra=forbid、枚举用 StrEnum/Literal、金额用 int(分)。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.enums import (
    INTENT_ALLOWED_ACTIONS,
    Action,
    EvidenceStatus,
    Intent,
    RefundMethod,
    RiskDecision,
    SafetyFlag,
    SlotSource,
    SpeechAct,
)


class StrictOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExtractedSlots(StrictOutput):
    """唯一权威定义（§2.4）。值为用户口述线索，须由 ContextAggregator 复核。"""

    order_id: str | None = Field(default=None, max_length=64)
    sku_id: str | None = Field(default=None, max_length=64)
    order_line_id: str | None = Field(default=None, max_length=64)
    waybill_no: str | None = Field(default=None, max_length=64)
    reason_code: str | None = Field(default=None, max_length=64)
    requested_amount_minor: int | None = Field(default=None, ge=0)
    quantity: int | None = Field(default=None, ge=1)
    product_category: str | None = Field(default=None, max_length=64)


class SlotSources(StrictOutput):
    """固定字段模型，适配 strict JSON Schema（§19.3）。值=来源枚举或 null。"""

    @field_validator("*", mode="before")
    @classmethod
    def _tolerant_source(cls, value):
        """来源只是溯源元数据，不是业务事实：模型自造 MISSNG/UNKNOWN 时记 null，不得让整条意图失败。"""
        if isinstance(value, str) and value not in SlotSource._value2member_map_:
            return None
        return value

    order_id: SlotSource | None
    sku_id: SlotSource | None
    order_line_id: SlotSource | None
    waybill_no: SlotSource | None
    reason_code: SlotSource | None
    requested_amount_minor: SlotSource | None
    quantity: SlotSource | None
    product_category: SlotSource | None


class IntentResult(StrictOutput):
    # 端点不支持严格 JSON Schema 时 schema 只是文本提示，模型会偶发多给字段（如 rationale_long）。
    # 多给的键不影响任何业务判定，忽略即可；业务字段本身仍受类型/枚举/范围约束。
    model_config = ConfigDict(extra="ignore")

    intent: Intent
    secondary_intents: list[Intent] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    slots: ExtractedSlots
    slot_sources: SlotSources
    missing_slots: list[str] = Field(default_factory=list)
    safety_flags: list[SafetyFlag] = Field(default_factory=list)
    speech_act: SpeechAct = SpeechAct.DECIDE
    subject: str | None = Field(default=None, max_length=32)
    language: str = Field(default="zh", max_length=16)
    rationale_short: str = Field(default="", max_length=200)

    @field_validator("subject", mode="before")
    @classmethod
    def _trim_subject(cls, value):
        return value[:32] if isinstance(value, str) else value

    @field_validator("rationale_short", mode="before")
    @classmethod
    def _trim_rationale(cls, value):
        return value[:200] if isinstance(value, str) else value

    @model_validator(mode="after")
    def _validate_intent(self) -> "IntentResult":
        # UNKNOWN 且没有任何更具体的旗标时，才把 AMBIGUOUS 作为「原因」补上；
        # 若已明确是注入/法律/自伤等安全旗标，不应再叠加 AMBIGUOUS（否则旗标评测会把安全样本判成多标）。
        if self.intent == Intent.UNKNOWN and not self.safety_flags:
            self.safety_flags.append(SafetyFlag.AMBIGUOUS)
        # AMBIGUOUS 允许与主意图并存：语义上表示“业务意图可判、但执行方案未定”
        # （如“换或者退都行”），由 decide_intent_route 路由到 clarify 先与用户确认。
        if self.intent in self.secondary_intents:
            raise ValueError("secondary_intents 不得重复主意图")
        return self


class ActionProposal(StrictOutput):
    action: Action
    order_id: str | None = None
    sku_ids: list[str] = Field(default_factory=list)
    waybill_no: str | None = None
    reason_code: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    requested_amount_minor: int | None = None  # 仅用户主张，非授权金额
    depends_on: list[int] = Field(default_factory=list)  # 依赖的动作下标（须 < 自身下标，构成 DAG）


class ActionPlan(StrictOutput):
    order_id: str | None = None
    actions: list[ActionProposal] = Field(default_factory=list)
    rationale: str = ""
    needs_more_info: bool = False
    missing_information: list[str] = Field(default_factory=list)
    source: str = "llm"  # llm | fastpath（由系统写入，非 LLM 产出）

    @model_validator(mode="after")
    def validate_actions(self) -> "ActionPlan":
        if len(self.actions) > 3:
            raise ValueError("最多 3 个逻辑动作")
        if not self.actions and not self.needs_more_info:
            raise ValueError("无动作时必须澄清或进入显式手工分支")
        if self.actions and self.order_id is None:
            raise ValueError("写动作必须具备订单 ID")
        if any(a.order_id != self.order_id for a in self.actions):
            raise ValueError("动作订单 ID 不一致")
        # 依赖必须指向更早的动作下标：既保证有向无环，又给出确定的执行顺序
        for i, a in enumerate(self.actions):
            for dep in a.depends_on:
                if dep < 0 or dep >= i:
                    raise ValueError(f"依赖下标非法（须 0<=dep<自身下标）：{a.action} depends_on={dep}")
        refunds = [a for a in self.actions if a.action in (Action.REQUEST_REFUND_ONLY, Action.REQUEST_RETURN_REFUND)]
        if len(refunds) > 1:
            raise ValueError("退款类动作最多一个")
        acts = {a.action for a in self.actions}
        if Action.REQUEST_RETURN_REFUND in acts and Action.CREATE_RESHIPMENT in acts:
            raise ValueError("REQUEST_RETURN_REFUND 不得与 CREATE_RESHIPMENT 同时出现")
        return self

    def allowed_for(self, intent: Intent) -> bool:
        allow = INTENT_ALLOWED_ACTIONS.get(intent, frozenset())
        return all(a.action in allow for a in self.actions)


class EvidenceItem(StrictOutput):
    clause_id: str
    clause_group_id: str | None = None
    doc_id: str
    version: str
    text: str
    score: float | None = None
    is_exception: bool = False


class EvidenceBundle(StrictOutput):
    status: EvidenceStatus
    items: list[EvidenceItem] = Field(default_factory=list)
    required_clause_ids: list[str] = Field(default_factory=list)
    missing_clause_ids: list[str] = Field(default_factory=list)
    policy_version: str | None = None
    as_of: str | None = None
    diagnostics: dict = Field(default_factory=dict)


class AmountCap(StrictOutput):
    """金额上限。``cap_type`` 决定超限后的语义：
    - POLICY_LIMIT：政策规定的业务金额上限，超限即政策不允许（DENY）；
    - AUTO_EXEC_THRESHOLD：自动执行阈值，超限仍有资格，但必须人工审批（MANUAL）。
    """

    cap_minor: int | None = None
    currency: str = "CNY"
    basis: str = ""
    cap_type: Literal["POLICY_LIMIT", "AUTO_EXEC_THRESHOLD"] = "AUTO_EXEC_THRESHOLD"


class PolicyDecision(StrictOutput):
    decision: Literal["ALLOW", "DENY", "MANUAL", "NEED_INFO"]
    # 动作级授权：由胜出规则 allowed_actions ∩ 意图动作白名单求出。
    # decision=ALLOW 只表示“业务资格成立”，不代表该意图请求的动作一定被授权。
    authorized_actions: list[str] = Field(default_factory=list)
    # 动作授权留痕：policy_actions / intent_actions / authorized / reason
    action_authorization: dict = Field(default_factory=dict)
    # 若节点因动作不可执行而降级，保留原始资格裁决（ALLOW）以便审计与申诉
    eligibility_decision: str | None = None
    reason_code: str
    reason_codes: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    required_clause_ids: list[str] = Field(default_factory=list)
    amount_cap: AmountCap | None = None
    amount_check: dict = Field(default_factory=dict)   # WITHIN / EXCEEDED / UNKNOWN + 金额来源
    missing_facts: list[str] = Field(default_factory=list)
    policy_version: str | None = None
    ruleset_version: str | None = None      # 规则集版本（多版本拼接）
    ruleset_hash: str | None = None         # 规范化规则内容指纹，可复现
    facts_snapshot_id: str | None = None    # 本次裁决使用的事实快照指纹
    evaluated_at: str | None = None         # 裁决基准时点（= as_of / claim_at）
    requires_manual_approval: bool = False
    execution_blocked: bool = False         # 金额等关键事实缺失时阻断执行（不改变业务资格）
    time_basis: str | None = None
    as_of: str | None = None
    policy_decision_id: str | None = None
    scope: dict = Field(default_factory=dict)
    trace: dict = Field(default_factory=dict)   # 决策 trace：载入/命中/冲突/胜出规则与条件取值


class QuoteView(StrictOutput):
    """Finance 只读报价返回（§8.6）。

    报价同时**冻结一笔可用余额预占（hold）**：`reservation_reference` 是资金侧的预占凭证，
    执行前复核与 Saga 执行都绑定该凭证；`available_balance_minor` 为本次预占后的剩余可用额度，
    `reservation_expires_at` 为预占有效期（与报价同 TTL）。
    """

    refund_request_id: str
    quote_id: str
    quote_hash: str
    amount_minor: int = Field(gt=0)
    currency: str
    order_version: str
    policy_decision_id: str
    expires_at: str
    reservation_reference: str | None = None
    reservation_expires_at: str | None = None
    available_balance_minor: int | None = None
    breakdown: dict = Field(default_factory=dict)
    refund_method: RefundMethod = RefundMethod.ORIGINAL_PAYMENT
    reservation_reference: str | None = None


class RiskOutcome(StrictOutput):
    decision: RiskDecision
    reason_codes: list[str] = Field(default_factory=list)


def validate_plan_against_intent(plan: ActionPlan, intent: Intent) -> None:
    """计划级校验的第二处（§2.4）：动作必须落在意图白名单内。"""
    if not plan.allowed_for(intent):
        raise ValueError(f"动作不在 intent={intent} 的允许白名单内")


def unknown_intent(reason: SafetyFlag = SafetyFlag.AMBIGUOUS, **kw) -> IntentResult:
    """构造低置信/未知意图结果（§2.4.4）。"""
    return IntentResult(
        intent=Intent.UNKNOWN,
        confidence=kw.pop("confidence", 0.0),
        slots=ExtractedSlots(),
        slot_sources=SlotSources(
            order_id=None, sku_id=None, order_line_id=None, waybill_no=None,
            reason_code=None, requested_amount_minor=None, quantity=None, product_category=None,
        ),
        safety_flags=[reason],
        **kw,
    )
