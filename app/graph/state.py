"""LangGraph State（LLD §7.3）。大字段只存指针；tenant/user 只来自认证上下文。"""
from __future__ import annotations

from typing import Annotated, NotRequired, TypedDict

from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    tenant_id: str
    user_id: str
    ticket_id: str
    thread_id: str
    run_id: str
    trace_id: str

    messages: Annotated[list, add_messages]
    # 用户提出本次售后申请的时刻（政策时效判断的唯一基准，必须是可重放的固定时点）
    claim_at: NotRequired[str]
    conversation_summary: NotRequired[dict]
    current_message_id: NotRequired[str]
    # 记忆系统（非权威，仅供理解与措辞）：情景/语义召回 + 统计
    memory_context: NotRequired[dict]
    memory_meta: NotRequired[dict]

    intent_result: NotRequired[dict]
    slots: NotRequired[dict]
    slot_sources: NotRequired[dict]
    order_snapshot: NotRequired[dict]
    logistics_snapshot: NotRequired[dict]
    finance_snapshot: NotRequired[dict]
    facts_version: NotRequired[str]
    context_meta: NotRequired[dict]

    policy_decision: NotRequired[dict]
    evidence_bundle: NotRequired[dict]
    evidence_status: NotRequired[str]

    action_plan: NotRequired[dict]
    plan_source: NotRequired[str]
    plan_critic: NotRequired[dict]
    replan_feedback: NotRequired[list[str]]
    plan_id: NotRequired[str]
    plan_hash: NotRequired[str]
    plan_frozen_facts_version: NotRequired[str]
    refund_request_id: NotRequired[str]
    quote_id: NotRequired[str]
    quote_hash: NotRequired[str]
    quote_amount_minor: NotRequired[int]
    quote_currency: NotRequired[str]
    quote_expires_at: NotRequired[str]
    quote_order_version: NotRequired[str]
    quote_policy_decision_id: NotRequired[str]
    reservation_ref: NotRequired[str]
    reservation_expires_at: NotRequired[str]
    reservation_amount_minor: NotRequired[int]
    requote_count: NotRequired[int]
    risk_decision: NotRequired[str]
    risk_reason_codes: NotRequired[list[str]]
    risk_by_action: NotRequired[dict]

    approval_id: NotRequired[str]
    approval_status: NotRequired[str]
    command_ids: NotRequired[list[str]]
    command_results: NotRequired[list[dict]]

    wait_token: NotRequired[str]
    wait_reason: NotRequired[str]
    wait_timeout_at: NotRequired[str]

    response_status: NotRequired[str]
    response_type: NotRequired[str]
    final_response: NotRequired[str]
    reconciliation_task_id: NotRequired[str]
    replanning_count: NotRequired[int]
    error_code: NotRequired[str]
    intent_route: NotRequired[str]
    intent_meta: NotRequired[dict]
    response_type_hint: NotRequired[str]
    response_text_hint: NotRequired[str]
    readonly_answer: NotRequired[dict]
    manual_queue: NotRequired[str]
    # ---- V4：九节点工作流契约与后台政策核实任务 ----
    workflow_version: NotRequired[str]
    facts_snapshot: NotRequired[dict]
    retrieval_bundle: NotRequired[dict]
    decision_proposal: NotRequired[dict]
    validation_result: NotRequired[dict]
    validated_actions: NotRequired[list[str]]
    validated_amount_minor: NotRequired[int]
    validated_evidence_ids: NotRequired[list[str]]
    risk_authorization: NotRequired[dict]
    public_status: NotRequired[str]
    manual_review_task_id: NotRequired[str]
