# -*- coding: utf-8 -*-
"""12 个 LangGraph 节点（LLD §7.1-§7.4）。节点只提案/只读；外部写由 Saga Dispatcher 执行。

v3.6 结构调整（依据评审意见）：
  - PolicyRetrieval + EvidenceGate        -> policy_evidence
    （原实现先后调用 discover() 两次，既重复耗时又无法保证同一版本快照；现为一次检索 + 确定性校验）
  - ClarificationWriter + AuditResponder  -> response_composer
    （统一回复出口，按 response_type 分发，避免部分澄清路径漏记审计）
  - ReconciliationScheduler               -> 移出图
    （长跑对账不是一次 Agent 决策；由 result_verifier 幂等创建任务，交独立 Worker 扫描回查）
  - MCPExecutor                           -> 改名 command_submitter
    （它只写持久化命令与 Outbox，不发外部写调用）
"""
from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime, timezone

from pydantic import ValidationError

from app.domain import action_registry
from app.domain.canonical import payload_hash
from app.domain.enums import (Action, Category, EvidenceStatus, Intent, RiskDecision, SafetyFlag,
                              SlotSource, SpeechAct, normalize_category)
from app.domain.schemas import (ActionPlan, ExtractedSlots, IntentResult, SlotSources, unknown_intent,
                                validate_plan_against_intent)
from app.policy.facts import claim_time_facts
from app.graph.deps import Deps
from app.graph.intent_fastpath import FastPathResult, estimate_complexity, try_fastpath
from app.graph.eligibility import (derive_intent_from_alternatives, detect_eligibility_query,
                                   derive_intent_from_verbs, demote_bare_complaint,
                                   sanitize_safety_flags,
                                   refund_only_tiebreak, restore_return_from_bare_tui,
                                   sanitize_ambiguous)
from app.domain.reason_codes import resolve_reason_code
from app.graph.plan_critic import (FATAL, PROMPT_VERSION_PLAN_CRITIC, deterministic_critique,
                                  llm_critique)
from app.graph.planner_fastpath import INTENT_PRIMARY_ACTION, try_fastplan
from app.graph.readonly_answer import compose_readonly_answer
from app.memory.service import render_memory_note
from app.mcp.gateway import ToolAuthorizationError, ToolNotFound, ToolTimeout
from app.mcp.toolctx import set_tool_context
from app.policy.messages import render_decision_message
from app.rag.evidence import EvidenceService
from app.risk_rules import RiskOutcome, evaluate_risk

try:
    from langgraph.types import interrupt
except Exception:  # pragma: no cover
    def interrupt(payload):  # type: ignore
        raise RuntimeError("langgraph interrupt 不可用；HITL 需要 langgraph>=0.2")

PROMPT_VERSION_INTENT = "intent-v3.8"   # v3.8：退款/退货边界 + 原因码客观优先
PROMPT_VERSION_PLANNER = "planner-v3.6"
PROMPT_VERSION_CLARIFY = "clarify-v3.5"
PROMPT_VERSION_RESPONDER = "responder-v3.5"

REFUND_ACTIONS = (Action.REQUEST_REFUND_ONLY.value, Action.REQUEST_RETURN_REFUND.value)
EXTERNAL_WRITE_ACTIONS = action_registry.external_write_actions()  # frozenset[str]，单一元数据源
DELIVERED_PARCEL_STATES = ("SIGNED", "DELIVERED", "RECEIVED", "COMPLETED")
INTERCEPT_INFLIGHT_STATES = ("ACCEPTED", "IN_PROGRESS", "REQUESTED", "SUBMITTED")
INTERCEPT_DONE_STATES = ("INTERCEPTED", "SUCCESS", "SUCCEEDED", "COMPLETED")
RESHIPMENT_DONE_STATES = ("SHIPPED", "DELIVERED", "RECEIVED", "COMPLETED")
RESHIPMENT_FAILED_STATES = ("FAILED", "CANCELLED", "REJECTED")
DISPATCH_DONE_STATES = ("ACCEPTED", "EXPEDITED", "IN_PROGRESS", "COMPLETED")
DISPATCH_FAILED_STATES = ("FAILED", "REJECTED", "CANCELLED")
INTERCEPT_FAILED_STATES = ("FAILED", "REJECTED", "CANCELLED")
PENDING_COMMAND_STATES = ("PREPARED", "CLAIMED", "IN_FLIGHT", "UNKNOWN", "RECONCILING")

FALLBACK_TEXTS = {
    "NEED_INFO": "为了帮您处理，请补充订单号或其他必要信息。",
    "DENIED": "抱歉，该情形不符合当前售后政策，已转人工为您进一步核实。",
    "MANUAL": "您的申请需要人工进一步核实，我们已转交人工处理。",
    "ACCEPTED": "已受理您的申请，我们将尽快处理。",
    "PROCESSING": "您的申请已受理，正在处理中，可在工单内查看进度。",
    "SUCCEEDED": "您的申请已处理完成。",
    "FAILED": "本次处理未成功完成，已转人工核实。",
    "UNKNOWN": "您的申请已收到，正在核实。",
    "ANSWER": "按当前政策，该诉求需要结合订单信息核实；您可在订单页发起申请，系统会按政策校验。",
    "LLM_DISABLED": "当前自动处置暂不可用，已为您转人工处理。",
    "SECURITY_DENIED": "抱歉，为保护账户与资金安全，本次请求无法自动处理。如有正常售后需求，请重新描述。",
    "PROMPT_INJECTION": "抱歉，检测到请求中包含试图改变系统行为的指令，本次不予处理。如有售后需求，请直接描述您的问题。",
}
# i18n 兜底文案（版本化政策模板仍是事实来源；此处仅多语言措辞）
I18N_FALLBACK = {
    "en-US": {
        "NEED_INFO": "To proceed, please provide your order number or other required information.",
        "DENIED": "Sorry, this case does not meet the current after-sales policy; it has been passed to a human.",
        "MANUAL": "Your request needs manual review; we have forwarded it to a human agent.",
        "ACCEPTED": "Your request has been accepted; we will process it shortly.",
        "PROCESSING": "Your request has been accepted and is being processed.",
        "SUCCEEDED": "Your request has been completed.",
        "FAILED": "This request was not completed successfully; it has been passed to a human.",
        "UNKNOWN": "We have received your request and are verifying it.",
    },
}
DEFAULT_LOCALE = "zh-CN"
DEFAULT_CHANNELS = ("app",)
_STATUS_BY_TYPE = {"NEED_INFO": "WAITING_USER", "PROCESSING": "WAITING_EXTERNAL", "FAILED": "FAILED"}


def _llm_failure_status(exc: Exception) -> str:
    """上游失败映射到 llm_calls.status 词表（LLD §10.7：SUCCESS/SCHEMA_ERROR/TIMEOUT/ERROR）。

    历史上这里写成 UPSTREAM_ERROR，与 DB CHECK 约束不一致，一旦上游报错就 500。
    """
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "timeout" in name or "timeout" in text or "timed out" in text:
        return "TIMEOUT"
    return "ERROR"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_ts(value) -> datetime | None:
    """ISO8601 -> 时区感知 datetime。非法/缺失返回 None（禁止字符串比较时间）。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _plan_actions(state) -> list[str]:
    """计划中的全部动作（保持顺序）。"""
    return [a.get("action") for a in (state.get("action_plan") or {}).get("actions", [])]


def _write_actions(state) -> list[str]:
    """计划中的**全部**外部写动作（去重、保持顺序）。

    风控与执行前复核必须逐动作覆盖。历史实现只取“第一个外部写动作”，
    而 command_submitter 会提交全部写动作 —— 计划里一旦出现第二个写动作，
    它就会绕过风控与执行前复核（资金/物流漂移漏检）。安全判定一律用本函数。
    """
    out: list[str] = []
    for action in _plan_actions(state):
        if action in EXTERNAL_WRITE_ACTIONS and action not in out:
            out.append(action)
    return out


def _command_idempotency_key(action: str, proposal: dict, state: dict) -> str:
    """按动作注册表的模板生成幂等键；缺失字段以空串兜底，避免 KeyError。

    模板渲染失败时回退为 <ticket>:<action>，保证仍可幂等（同一工单同一动作只提交一次）。
    """
    mspec = action_registry.spec(action)
    order = state.get("order_snapshot") or {}
    logistics = state.get("logistics_snapshot") or {}
    sku_ids = proposal.get("sku_ids") or []
    values = {"ticket_id": state.get("ticket_id") or "",
              "order_id": proposal.get("order_id") or order.get("order_id") or "",
              "waybill_no": (state.get("slots") or {}).get("waybill_no")
              or logistics.get("waybill_no") or "",
              "sku_id": (sku_ids[0] if sku_ids else "")}
    tpl = mspec.idempotency_template if mspec else ""
    try:
        scope = tpl.format(**values) if tpl else f"{values['ticket_id']}:{action}"
    except (KeyError, IndexError):
        scope = f"{values['ticket_id']}:{action}"
    # 幂等范围 = 租户 : 模板渲染（工单/订单/运单/SKU） : 操作类型 : 计划指纹。
    # 带操作类型：退款与退货退款共用 refund:{ticket}:{order} 模板，
    #            同一工单先后申请两种退款时，旧键会把第二条命令静默丢弃。
    # 带 plan_hash：金额/行项目变化必须重新走执行前复核，不能被旧命令复用。
    return (f"{state.get('tenant_id') or ''}:{scope}:{action}:"
            f"{state.get('plan_hash') or ''}")


def _topo_external_writes(plan: dict) -> list[dict]:
    """按 depends_on 的 DAG 对计划动作做拓扑序展开（依赖只指向更早下标，故稳定排序即可）。

    保证被依赖的动作先提交；无依赖时保持原顺序。越界/自依赖等非法依赖由 schema 已拦截。
    """
    actions = list(plan.get("actions") or [])
    order: list[int] = []
    seen: set[int] = set()

    def visit(i: int) -> None:
        if i in seen:
            return
        for dep in (actions[i].get("depends_on") or []):
            if isinstance(dep, int) and 0 <= dep < len(actions):
                visit(dep)
        seen.add(i)
        order.append(i)

    for i in range(len(actions)):
        visit(i)
    return [actions[i] for i in order]


def _primary_action(state) -> str | None:
    """首个外部写动作（仅用于展示/兼容；授权判定必须用 _write_actions）。"""
    writes = _write_actions(state)
    if writes:
        return writes[0]
    actions = _plan_actions(state)
    return actions[0] if actions else None


def _user_query(state) -> str:
    """检索 Query = 当前用户问题 + 已确认槽位要点；不拼接整段会话。"""
    for m in reversed(state.get("messages") or []):
        content = getattr(m, "content", None) or (m.get("content") if isinstance(m, dict) else None)
        if content and str(content).strip():
            return str(content)[:500]
    slots = state.get("slots") or {}
    return str(slots.get("reason_code") or (state.get("intent_result") or {}).get("rationale_short") or "售后")


def _history_snippets(state, limit: int = 4) -> list[str]:
    """取最近几轮**用户**话术作为检索上下文锚点（不含当前问题，不含助手话术）。

    只用于消解“这个/那个/刚才”的指代，属于零成本扩展；不参与任何政策/风控裁决。
    """
    out: list[str] = []
    msgs = list(state.get("messages") or [])
    for m in msgs[:-1]:
        role = getattr(m, "type", None) or (m.get("role") if isinstance(m, dict) else None)
        if role not in ("user", "human"):
            continue
        content = getattr(m, "content", None) or (m.get("content") if isinstance(m, dict) else None)
        text = " ".join(str(content or "").split())[:200]
        if text:
            out.append(text)
    return out[-max(1, int(limit)):]


def _intent_messages(state, deps: Deps) -> list[dict]:
    """保留工具调用与结果的配对：ToolMessage 必须携带 tool_call_id，AI 消息保留 tool_calls。"""
    history = (state.get("messages") or [])[-8:]
    msgs: list[dict] = [{"role": "system", "content": deps.prompts.get("intent_guardrail", "")}]
    note = render_memory_note(state)
    if note:
        # 记忆提示：非权威，仅供理解上下文；不得作为政策/风控/资金裁决依据
        msgs.append({"role": "system", "content": note})
    for m in history:
        if isinstance(m, dict):
            role = m.get("role", "user")
            item: dict = {"role": role, "content": m.get("content") or ""}
            if m.get("tool_calls"):
                item["tool_calls"] = m["tool_calls"]
            if m.get("tool_call_id"):
                item["tool_call_id"] = m["tool_call_id"]
            msgs.append(item)
            continue
        mtype = getattr(m, "type", None)
        content = getattr(m, "content", "") or ""
        if mtype == "tool":
            msgs.append({"role": "tool", "content": content,
                         "tool_call_id": getattr(m, "tool_call_id", None)})
        elif mtype in ("ai", "assistant"):
            item = {"role": "assistant", "content": content}
            tool_calls = getattr(m, "tool_calls", None)
            if tool_calls:
                item["tool_calls"] = tool_calls
            msgs.append(item)
        else:
            msgs.append({"role": "user", "content": content})
    return msgs


async def _persist_intent(state, deps: Deps, result: IntentResult) -> None:
    """意图权威回写：以读到的 ticket version 做 CAS；未落库时只记审计，不影响本轮决策。"""
    ticket = await deps.persistence.get_ticket(tenant_id=state["tenant_id"], ticket_id=state["ticket_id"])
    expected = (ticket or {}).get("version")
    applied = await deps.persistence.upsert_intent(
        tenant_id=state["tenant_id"], ticket_id=state["ticket_id"],
        intent=str(result.intent), expected_version=expected,
    )
    if not applied:
        await deps.persistence.append_audit(
            tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], event="INTENT_WRITE_SKIPPED",
            payload={"intent": str(result.intent), "expected_version": expected})

# --------------------------------------------------------------------------- #
# 1) IntentGuardrail
# --------------------------------------------------------------------------- #
def _latest_user_text(state) -> str:
    """最近一条非空用户消息原文（快路径与复杂度评分只看当前这句话）。"""
    for m in reversed(state.get("messages") or []):
        content = getattr(m, "content", None) or (m.get("content") if isinstance(m, dict) else None)
        if content and str(content).strip():
            return str(content).strip()
    return ""


INTENT_LABELS = {
    "REFUND_ONLY": "仅退款",
    "RETURN_REFUND": "退货退款",
    "INTERCEPT_REFUND": "拦截退款",
    "RESHIPMENT": "补发",
    "EXPEDITE": "催发货/加急",
    "STATUS_QUERY": "物流查询",
    "COMPLAINT": "投诉",
}
# 会产生外部写操作（或引发写操作）的意图：作为次要意图时一律先让用户确认
WRITE_CAPABLE_INTENTS = frozenset({
    Intent.REFUND_ONLY, Intent.RETURN_REFUND, Intent.INTERCEPT_REFUND,
    Intent.RESHIPMENT, Intent.EXPEDITE,
})
MULTI_INTENT_TEMPLATE = (
    "检测到您同时提出了多项诉求：{items}。为避免误操作，请确认本次希望优先处理哪一项？"
    "其余诉求可在本条处理完成后继续提交。"
)


def _fastpath_intent_result(fp: FastPathResult) -> IntentResult:
    """构造快路径的意图结果：confidence=1.0，槽位只取确定命中的订单号。"""
    order_id = fp.order_id
    slots = ExtractedSlots(order_id=order_id)
    sources = SlotSources(order_id=SlotSource.USER_STATED if order_id else None, sku_id=None,
                          order_line_id=None, waybill_no=None, reason_code=None,
                          requested_amount_minor=None, quantity=None, product_category=None)
    return IntentResult(
        intent=fp.intent, confidence=1.0, slots=slots, slot_sources=sources,
        missing_slots=[] if order_id else ["order_id"],
        rationale_short="fastpath:" + fp.matched_rule,
    )


def _apply_eligibility(text: str, result: IntentResult) -> IntentResult:
    """资格咨询覆盖 LLM 的写意图：同一句话可以是"问能不能"，而不是"要你办"。

    只在确定性规则命中时覆盖，且不改八类意图枚举本身；路由与动作侧靠 speech_act 区分。
    """
    det = detect_eligibility_query(text)
    if det is None:
        forced = refund_only_tiebreak(text, result.intent)
        if forced is not None:
            return result.model_copy(update={"intent": forced})
        restored = restore_return_from_bare_tui(text, result.intent)
        if restored is not None:
            return result.model_copy(update={"intent": restored})
        return result
    return result.model_copy(update={"intent": det.intent, "subject": det.subject,
                                     "speech_act": SpeechAct.ELIGIBILITY_QUERY,
                                     "secondary_intents": []})


# 与快路径同口径：带标签的订单号（“订单 ts006 / 订单号：o_1002”）
_LABELLED_ORDER_RE = re.compile(r"(?:订单号|订单编号|订单|单号)\s*[:：]?\s*([A-Za-z0-9_\-]{4,32})")


def _backfill_order_id(text: str, result: IntentResult) -> IntentResult:
    """LLM 偶发漏抽订单号时的确定性兜底。

    模型（尤其小模型）会整段照抄却把 order_id 留空，用户明明写了订单号仍被要求补单号。
    订单号是可用正则无歧义抽取的字段，不必依赖模型。
    """
    if result.slots.order_id:
        return result
    match = _LABELLED_ORDER_RE.search(text or "")
    if not match:
        return result
    slots = result.slots.model_copy(update={"order_id": match.group(1)})
    sources = result.slot_sources.model_copy(update={"order_id": SlotSource.USER_STATED})
    return result.model_copy(update={"slots": slots, "slot_sources": sources})


def _finalize_intent(text: str, result: IntentResult, state) -> tuple[IntentResult, dict]:
    """意图/原因码的确定性后处理（两条路径共用）。

    1) AMBIGUOUS 只影响路由，不抹掉语义：意图若被判成 UNKNOWN，按用户**首次提出的方案**还原，
       例如“退货还是补发你们看着办”-> RETURN_REFUND + AMBIGUOUS（仍走澄清）。
    2) 原因码按 权威事实 > 原文客观证据 > LLM 槽位 解析，避免“不想要了”盖过“与描述不符”。
    """
    # 模型判 UNKNOWN 但用户已明确说出动作（退/换/补发）时，按动词确定性回收，
    # 否则会出现“用户写了订单号和‘想退’，系统却让补订单号”的荒谬回复。
    _soft = {SafetyFlag.AMBIGUOUS, SafetyFlag.OFF_TOPIC}
    if result.intent == Intent.UNKNOWN and not (set(result.safety_flags) - _soft):
        from_verbs = derive_intent_from_verbs(text)
        if from_verbs is not None:
            result = result.model_copy(update={"intent": from_verbs})

    cleaned = sanitize_ambiguous(text, result.intent, result.safety_flags)
    if len(cleaned) != len(result.safety_flags):
        result = result.model_copy(update={"safety_flags": cleaned})

    # 抱怨语气不等于投诉：“迟到太久，赔付 3000 元”被判成 COMPLAINT + 次意图 REFUND_ONLY
    # 时会走进多意图澄清，真正的赔付诉求反而没被处理。无投诉字面证据时归位主意图。
    demoted, kept_secondaries = demote_bare_complaint(
        text, result.intent, list(result.secondary_intents or []))
    if demoted != result.intent or len(kept_secondaries) != len(result.secondary_intents or []):
        result = result.model_copy(
            update={"intent": demoted, "secondary_intents": kept_secondaries})

    # 只保留有原文证据的安全旗标：模型会把“投诉/举报”误判成法律威胁、把“退差价”误判成跑题。
    pured = sanitize_safety_flags(text, result.safety_flags)
    if len(pured) != len(result.safety_flags):
        result = result.model_copy(update={"safety_flags": pured})

    if result.intent == Intent.UNKNOWN and SafetyFlag.AMBIGUOUS in result.safety_flags:
        derived = derive_intent_from_alternatives(text)
        if derived is not None:
            result = result.model_copy(update={"intent": derived})

    # 权威事实只能来自 OMS/售后单等真实业务系统（state 由 ContextAggregator 填充）；
    # 评测夹具不得注入，否则等于答案泄漏。当前 mock 不提供该事实，故恒为 None。
    code, source = resolve_reason_code(text, result.slots.reason_code,
                                       state.get("claim_reason_code"))
    if code != result.slots.reason_code:
        slots = result.slots.model_copy(update={"reason_code": code})
        sources = result.slot_sources.model_copy(
            update={"reason_code": SlotSource.USER_STATED if code else None})
        result = result.model_copy(update={"slots": slots, "slot_sources": sources})
    result = _backfill_order_id(text, result)
    return result, {"reason_code": code, "reason_source": source}


def decide_intent_route(error_code: str | None, intent_result: dict) -> tuple[str, str | None, str | None]:
    """意图路由的唯一判定（纯函数）：返回 (route, response_type_hint, response_text_hint)。

    安全 flag 优先于业务意图：注入/越权/隐私 -> deny；法律/自伤/辱骂 -> 人工；跑题 -> 澄清。
    多意图若含写操作诉求 -> clarify（最稳，避免一次误操作）。
    route_after_intent 与 intent_guardrail 共用本函数，保证路由与文案一致。
    """
    flags = set(intent_result.get("safety_flags") or [])
    if error_code == "LLM_DISABLED":
        return "manual", "MANUAL", FALLBACK_TEXTS["LLM_DISABLED"]
    if SafetyFlag.PROMPT_INJECTION in flags:
        return "deny", "DENIED", FALLBACK_TEXTS["PROMPT_INJECTION"]
    if SafetyFlag.TOOL_OR_PERMISSION_REQUEST in flags or SafetyFlag.PII_OR_CREDENTIAL in flags:
        return "deny", "DENIED", FALLBACK_TEXTS["SECURITY_DENIED"]
    if SafetyFlag.LEGAL_OR_REGULATORY_THREAT in flags or SafetyFlag.SELF_HARM_OR_SAFETY in flags:
        return "manual", "MANUAL", None
    if SafetyFlag.ABUSE in flags:
        return "manual", "MANUAL", None
    if SafetyFlag.OFF_TOPIC in flags:
        return "clarify", "NEED_INFO", FALLBACK_TEXTS["NEED_INFO"]
    if SafetyFlag.AMBIGUOUS in flags:
        return "clarify", "NEED_INFO", None
    write_secondaries = [str(i) for i in (intent_result.get("secondary_intents") or [])
                         if str(i) in {str(x) for x in WRITE_CAPABLE_INTENTS}]
    if write_secondaries:
        items = "、".join(INTENT_LABELS.get(i, i) for i in write_secondaries)
        return "clarify", "NEED_INFO", MULTI_INTENT_TEMPLATE.format(items=items)
    intent = intent_result.get("intent")
    if intent in (None, Intent.UNKNOWN):
        return "clarify", "NEED_INFO", None
    if intent in (Intent.STATUS_QUERY, Intent.COMPLAINT):
        return "readonly", None, None
    return "aggregate", None, None


def _intent_route_fields(error_code: str | None, result: IntentResult) -> dict:
    route, type_hint, text_hint = decide_intent_route(error_code, result.model_dump(mode="json"))
    # 始终写回（未命中时置 None）：多轮复用同一 state 时不得残留上一轮的路由提示
    return {"intent_route": route, "response_type_hint": type_hint,
            "response_text_hint": text_hint}


def _intent_state_update(result: IntentResult, error_code: str | None, meta: dict) -> dict:
    update = {
        "intent_result": result.model_dump(mode="json"),
        "slots": result.slots.model_dump(mode="json"),
        "slot_sources": result.slot_sources.model_dump(mode="json"),
        "intent_meta": meta,
    }
    update.update(_intent_route_fields(error_code, result))
    return update


async def intent_guardrail(state, deps: Deps) -> dict:
    """意图识别与安全分类：LLM 关闭 -> 人工；否则 规则快路径 -> 分级 LLM -> 安全/多意图路由。"""
    if deps.llm is None:
        result = unknown_intent(SafetyFlag.AMBIGUOUS)
        update = _intent_state_update(result, "LLM_DISABLED",
                                      {"tier": "disabled", "model": None, "reason": "LLM_DISABLED"})
        update["error_code"] = "LLM_DISABLED"
        return update

    text = _latest_user_text(state)

    # (1) 确定性快路径：高频低危意图用规则直出，不调用 LLM
    if deps.settings.llm_intent_fastpath_enabled:
        fast = try_fastpath(text)
        if fast is not None:
            result = _apply_eligibility(text, _fastpath_intent_result(fast))
            result, extra = _finalize_intent(text, result, state)
            await deps.persistence.append_audit(
                tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], event="INTENT_FASTPATH",
                payload={"intent": str(result.intent), "matched_rule": fast.matched_rule,
                         "order_id": fast.order_id, "prompt_version": PROMPT_VERSION_INTENT,
                         **extra})
            await _persist_intent(state, deps, result)
            return _intent_state_update(result, None, {
                "tier": "fastpath", "model": None, "matched_rule": fast.matched_rule,
                "complexity": 0, "confidence": 1.0, "secondary_intents": [], **extra})

    # (2) 复杂度分级：简单 -> 小模型，复杂 -> 大模型（未配置小模型时一律走主模型）
    complexity = estimate_complexity(text)
    use_small = (deps.llm_small is not None and deps.settings.llm_intent_small_model_enabled
                 and complexity < deps.settings.llm_intent_complexity_threshold)
    tier = "small" if use_small else "large"
    client = deps.llm_small if use_small else deps.llm

    messages = _intent_messages(state, deps)
    try:
        parsed, audit = await client.structured(
            messages=messages, schema_model=IntentResult,
            prompt_version=PROMPT_VERSION_INTENT, temperature=0.0,
        )
    except ValidationError as exc:
        await deps.persistence.record_llm_call(
            tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], run_id=state["run_id"],
            trace_id=state["trace_id"], node="intent_guardrail", model=deps.settings.llm_model,
            prompt_version=PROMPT_VERSION_INTENT, status="SCHEMA_ERROR", error_code=str(exc)[:200])
        result = unknown_intent(SafetyFlag.AMBIGUOUS)
        update = _intent_state_update(result, None, {"tier": tier, "complexity": complexity,
                                                     "error": "INTENT_SCHEMA_ERROR"})
        update["error_code"] = "INTENT_SCHEMA_ERROR"
        return update
    except Exception as exc:  # 上游超时/授权/网络错误：与 schema 错误分开记录
        await deps.persistence.record_llm_call(
            tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], run_id=state["run_id"],
            trace_id=state["trace_id"], node="intent_guardrail", model=deps.settings.llm_model,
            prompt_version=PROMPT_VERSION_INTENT, status=_llm_failure_status(exc), error_code=str(exc)[:200])
        result = unknown_intent(SafetyFlag.AMBIGUOUS)
        update = _intent_state_update(result, None, {"tier": tier, "complexity": complexity,
                                                     "error": "INTENT_UPSTREAM_ERROR"})
        update["error_code"] = "INTENT_UPSTREAM_ERROR"
        return update

    model_name = audit.get("model") or deps.settings.llm_model
    await deps.persistence.record_llm_call(
        tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], run_id=state["run_id"],
        trace_id=state["trace_id"], node="intent_guardrail", model=model_name,
        prompt_version=PROMPT_VERSION_INTENT, status="SUCCESS",
        request_hash=audit.get("request_hash"), response_hash=audit.get("response_hash"),
        input_tokens=(audit.get("usage") or {}).get("prompt_tokens"),
    output_tokens=(audit.get("usage") or {}).get("completion_tokens"),
    latency_ms=audit.get("latency_ms"))

    if parsed.confidence < deps.settings.llm_intent_confidence_threshold:
        result = parsed.model_copy(update={"intent": Intent.UNKNOWN})
        if SafetyFlag.AMBIGUOUS not in result.safety_flags:
            result.safety_flags.append(SafetyFlag.AMBIGUOUS)
    else:
        result = parsed

    # speech_act/subject 只能由确定性规则给出：LLM 若自行标记"资格咨询"，
    # 会把明确的执行诉求（如"直接退款"）降级成只回答不执行。这里无条件复位。
    if result.speech_act != SpeechAct.DECIDE or result.subject is not None:
        result = result.model_copy(update={"speech_act": SpeechAct.DECIDE, "subject": None})
    result = _apply_eligibility(text, result)
    result, extra = _finalize_intent(text, result, state)

    secondary = [str(i) for i in result.secondary_intents]
    await deps.persistence.append_audit(
        tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], event="INTENT_CLASSIFIED",
        payload={"intent": str(result.intent), "secondary_intents": secondary,
                 "confidence": result.confidence, "safety_flags": [str(f) for f in result.safety_flags],
                 "tier": tier, "model": model_name, "complexity": complexity,
                 "prompt_version": PROMPT_VERSION_INTENT, **extra})
    await _persist_intent(state, deps, result)
    return _intent_state_update(result, None, {
        "tier": tier, "model": model_name, "complexity": complexity,
        "confidence": result.confidence, "secondary_intents": secondary, **extra})


def route_after_intent(state) -> str:
    cached = state.get("intent_route")
    if cached:
        return cached
    route, _type_hint, _text_hint = decide_intent_route(state.get("error_code"),
                                                        state.get("intent_result") or {})
    return route


# --------------------------------------------------------------------------- #
# 2) ContextAggregator（只读可信事实）
# --------------------------------------------------------------------------- #
REFUND_FAMILY_INTENTS = frozenset({Intent.REFUND_ONLY, Intent.RETURN_REFUND, Intent.INTERCEPT_REFUND})
LOGISTICS_CRITICAL_INTENTS = frozenset({Intent.INTERCEPT_REFUND, Intent.EXPEDITE})
READONLY_INTENTS = frozenset({Intent.STATUS_QUERY, Intent.COMPLAINT})


def _context_error_code(exc: BaseException) -> str:
    """把工具层异常映射为上下文错误码（供路由与审计使用）。"""
    msg = str(exc)
    if isinstance(exc, ToolNotFound) or "ORDER_NOT_FOUND" in msg:
        return "ORDER_NOT_FOUND"
    if isinstance(exc, ToolAuthorizationError) or "FORBIDDEN" in msg:
        return "READ_FORBIDDEN"
    if isinstance(exc, ToolTimeout) or "TOOL_TIMEOUT" in msg:
        return "READ_TIMEOUT"
    return "READ_FAILED"


async def _readonly_answer(state, deps: Deps, *, intent: Intent, order=None, logistics=None,
                           degraded: dict | None = None, snapshots: dict | None = None) -> dict:
    """只读自助答复：模板事实 + LLM 仅润色（后置校验，失败回退模板），走同一回复出口。"""
    text, meta = await compose_readonly_answer(deps, state, intent=intent, order=order,
                                               logistics=logistics, degraded=degraded or {})
    await deps.persistence.append_audit(
        tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], event="READONLY_ANSWER",
        payload={"intent": str(intent), "mode": meta.get("mode"), "verified": meta.get("verified"),
                 "reason": meta.get("reason"), "degraded": degraded or {}, "text": text})
    out = {"final_response": text, "readonly_answer": meta, "response_type_hint": "ANSWER"}
    if snapshots:
        out.update(snapshots)
    return out


async def context_aggregator(state, deps: Deps) -> dict:
    """事实采集 + 只读自助答复（§7.4.2）。

    - 业务处置（聚合）路径：并发只读取订单/物流/资金事实、归属校验、关键/次要事实分级降级；
    - 只读自助路径（STATUS_QUERY/COMPLAINT）：取必要事实后用模板渲染答案，
      LLM 仅在事实范围内润色且经后置校验（越界回退模板），不进入政策/风控/执行链路；
    - 每次只读调用经 ToolGateway 落 tool_calls 审计。
    """
    slots = state.get("slots") or {}
    order_id = slots.get("order_id")
    tenant_id = state["tenant_id"]
    intent = Intent((state.get("intent_result") or {}).get("intent", "UNKNOWN"))

    # ---- 投诉：受理并创建人工待办（不是“只读答复”）----
    # 投诉的价值在于进入人工处理队列；仅返回 ANSWER 会让投诉看起来“已处理”，
    # 实际没有任何人接手。这里把工单状态置为 MANUAL 并落审计，作为人工待办凭据。
    if intent == Intent.COMPLAINT:
        try:
            await deps.persistence.set_ticket_status(
                tenant_id=tenant_id, ticket_id=state["ticket_id"], status="MANUAL")
        except Exception:  # noqa: BLE001 - 状态更新失败不得吞掉投诉
            await deps.persistence.append_audit(
                tenant_id=tenant_id, ticket_id=state["ticket_id"], event="COMPLAINT_STATUS_UPDATE_FAILED",
                payload={"target_status": "MANUAL"})
        await deps.persistence.append_audit(
            tenant_id=tenant_id, ticket_id=state["ticket_id"], event="COMPLAINT_ESCALATED",
            payload={"manual_queue": "after_sales_complaint", "ticket_status": "MANUAL"})
        text = ("已受理您的投诉，并转交人工专员跟进。工单号："
                f"{state['ticket_id']}。我们会尽快与您联系，请保持联系方式畅通。")
        return {"final_response": text, "readonly_answer": {"mode": "template",
                "reason": "COMPLAINT_ESCALATED"}, "response_type_hint": "MANUAL",
                "manual_queue": "after_sales_complaint"}

    if not order_id:
        # 澄清语义：请用户补充订单号（否则会被回复层误判为人工）
        return {"error_code": "NEED_ORDER_ID", "response_type_hint": "NEED_INFO"}
    finance_critical = intent in REFUND_FAMILY_INTENTS
    logistics_critical = intent in LOGISTICS_CRITICAL_INTENTS

    set_tool_context(tenant_id=tenant_id, ticket_id=state.get("ticket_id"), run_id=state.get("run_id"),
                     trace_id=state.get("trace_id"), node="context_aggregator")

    # ---- 只读自助：状态查询（订单 + 物流）----
    if intent == Intent.STATUS_QUERY:
        order_r, logi_r = await asyncio.gather(
            deps.orders.get_order_detail(tenant_id=tenant_id, order_id=order_id),
            deps.logistics.get_tracking_info(tenant_id=tenant_id, order_id=order_id,
                                             waybill_no=slots.get("waybill_no")),
            return_exceptions=True,
        )
        if isinstance(order_r, BaseException):
            return {"error_code": _context_error_code(order_r)}
        if order_r.owner_user_id != state["user_id"]:
            return {"error_code": "NOT_OWNER"}
        degraded: dict = {}
        logistics = None
        if isinstance(logi_r, BaseException):
            degraded["logistics"] = _context_error_code(logi_r)
        else:
            logistics = logi_r
        logi_snap = ({"waybill_no": logistics.waybill_no, "parcel_status": logistics.parcel_status,
                      "version": logistics.version, "source": "TMS", "as_of": _now()}
                     if logistics is not None else
                     {"waybill_no": slots.get("waybill_no"), "parcel_status": "UNKNOWN", "version": "",
                      "source": "TMS", "as_of": _now(), "degraded": True,
                      "error_code": degraded.get("logistics")})
        return await _readonly_answer(
            state, deps, intent=intent, order=order_r, logistics=logistics, degraded=degraded,
            snapshots={"order_snapshot": {"order_id": order_r.order_id, "status": order_r.status,
                                          "version": order_r.version, "source": "OMS", "as_of": _now()},
                       "logistics_snapshot": logi_snap})

    # ---- 业务处置（聚合）路径 ----
    order_r, logi_r, fin_r, stats_r = await asyncio.gather(
        deps.orders.get_order_detail(tenant_id=tenant_id, order_id=order_id),
        deps.logistics.get_tracking_info(tenant_id=tenant_id, order_id=order_id,
                                         waybill_no=slots.get("waybill_no")),
        deps.finance.get_refundable_balance(tenant_id=tenant_id, order_id=order_id),
        deps.orders.get_buyer_refund_stats(tenant_id=tenant_id, order_id=order_id),
        return_exceptions=True,
    )
    if isinstance(order_r, BaseException):
        return {"error_code": _context_error_code(order_r)}
    if order_r.owner_user_id != state["user_id"]:
        return {"error_code": "NOT_OWNER"}

    order = order_r
    lines = [{"order_line_id": ln.get("order_line_id"), "sku_id": ln.get("sku_id"),
              "quantity": ln.get("quantity")} for ln in (order.lines or [])]
    update: dict = {
        "order_snapshot": {"order_id": order.order_id, "status": order.status, "version": order.version,
                           "category": order.category, "currency": order.currency, "lines": lines,
                           "delivered_at": order.delivered_at,
                           "source": "OMS", "as_of": _now()},
    }

    degraded = {}
    if isinstance(logi_r, BaseException):
        code = _context_error_code(logi_r)
        if logistics_critical:
            return {"error_code": code}
        degraded["logistics"] = code
        update["logistics_snapshot"] = {"waybill_no": slots.get("waybill_no"), "parcel_status": "UNKNOWN",
                                        "intercept_command_status": None, "version": "", "source": "TMS",
                                        "as_of": _now(), "degraded": True, "error_code": code}
    else:
        tracking = logi_r
        update["logistics_snapshot"] = {"waybill_no": tracking.waybill_no,
                                        "parcel_status": tracking.parcel_status,
                                        "intercept_command_status": tracking.intercept_command_status,
                                        "version": tracking.version, "source": "TMS", "as_of": _now()}

    if isinstance(fin_r, BaseException):
        code = _context_error_code(fin_r)
        if finance_critical:
            return {"error_code": code}
        degraded["finance"] = code
        update["finance_snapshot"] = {"order_id": order_id, "refundable_balance_minor": None,
                                      "reserved_amount_minor": None, "currency": order.currency,
                                      "source": "Finance", "as_of": _now(), "version": "",
                                      "degraded": True, "error_code": code}
    else:
        finance = fin_r
        update["finance_snapshot"] = {"order_id": finance.order_id,
                                      "refundable_balance_minor": finance.refundable_balance_minor,
                                      "reserved_amount_minor": finance.reserved_amount_minor,
                                      "currency": finance.currency, "source": "Finance",
                                      "as_of": finance.as_of or _now(), "version": finance.version}

    # 买家退换/退款统计：风控维度（次要事实）；失败仅降级，风控层对缺失一律 fail-closed 到人工
    if isinstance(stats_r, BaseException):
        code = _context_error_code(stats_r)
        degraded["risk_stats"] = code
        update["risk_facts"] = {"degraded": True, "error_code": code}
    else:
        stats = stats_r
        update["risk_facts"] = {"order_id": stats.order_id, "refund_count_30d": stats.refund_count_30d,
                                "refund_amount_30d_minor": stats.refund_amount_30d_minor,
                                "blacklisted": stats.blacklisted, "source": "OMS",
                                "as_of": stats.as_of or _now(), "version": stats.version}

    logi_ver = update["logistics_snapshot"].get("version") or "unknown"
    fin_ver = update["finance_snapshot"].get("version") or "unknown"
    update["facts_version"] = f"{order.version}:{logi_ver}:{fin_ver}"
    update["context_meta"] = {"degraded": degraded, "finance_critical": finance_critical,
                              "logistics_critical": logistics_critical}
    await deps.persistence.append_audit(
        tenant_id=tenant_id, ticket_id=state["ticket_id"], event="CONTEXT_AGGREGATED",
        payload={"facts_version": update["facts_version"], "degraded": degraded,
                 "finance_critical": finance_critical, "logistics_critical": logistics_critical})
    return update


def route_after_context(state) -> str:
    code = state.get("error_code")
    if code == "NEED_ORDER_ID":
        return "clarify"
    if code == "NOT_OWNER":
        return "deny"
    if code in ("READ_TIMEOUT", "ORDER_CONFLICT", "READ_FAILED", "ORDER_NOT_FOUND", "READ_FORBIDDEN"):
        return "manual"
    if state.get("readonly_answer"):
        return "respond"
    return "policy"

# 3) PolicyEngine（确定性，不调 LLM；裁决必须落库）
# --------------------------------------------------------------------------- #
async def policy_engine(state, deps: Deps) -> dict:
    intent = Intent((state.get("intent_result") or {}).get("intent", "UNKNOWN"))
    as_of = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    # 申请时点必须是“用户提出申请的时刻”（可重放），不能用裁决时刻现取系统时间。
    claim_at = state.get("claim_at") or as_of
    order = dict(state.get("order_snapshot") or {})
    order["category"] = normalize_category(order.get("category"))
    slots = state.get("slots") or {}
    finance = dict(state.get("finance_snapshot") or {})
    if finance.get("requested_amount_minor") is None:
        finance["requested_amount_minor"] = slots.get("requested_amount_minor")
    if finance.get("amount_minor") is None:
        finance["amount_minor"] = slots.get("requested_amount_minor")
    facts = {
        "order": order,
        "logistics": state.get("logistics_snapshot") or {},
        "finance": finance,
        # 时间事实：保存原始 delivered_at/claim_at，派生 day_index_signed / day_index_next
        # （口径定义集中在 app/policy/facts.py，DSL 只做纯比较）
        "claim": {
            "reason_code": slots.get("reason_code"),
            "quantity": slots.get("quantity"),
            **claim_time_facts(delivered_at=order.get("delivered_at"), claim_at=claim_at),
        },
        "intent": str(intent),
    }
    decision = deps.policy.decide(tenant_id=state["tenant_id"], intent=intent, facts=facts,
                                  as_of=as_of, scope={"category": order["category"]})
    # ---- 动作级授权闸门（核心不变量）----
    # PolicyEngine 的 ALLOW 只代表业务资格成立；具体动作必须落在 authorized_actions 内。
    # 1) ALLOW 但规则没有可自动化动作（如“维修”，当前动作空间未接入）-> 转人工；
    # 2) ALLOW 但当前意图请求的动作不在白名单（如用 7 天无理由规则直接仅退款）->
    #    澄清：说明可支持的处理方式，由用户确认，绝不自动换动作执行。
    gate_text = None
    if decision.decision == "ALLOW":
        primary = INTENT_PRIMARY_ACTION.get(intent)
        authorized = list(decision.authorized_actions or [])
        policy_actions = list((decision.action_authorization or {}).get("policy_actions")
                              or decision.authorized_actions or [])
        if not policy_actions:
            decision.eligibility_decision = "ALLOW"
            decision.decision = "MANUAL"
            decision.reason_code = "NO_AUTOMATABLE_ACTION_FOR_POLICY"
            decision.reason_codes = ["NO_AUTOMATABLE_ACTION_FOR_POLICY", *decision.reason_codes]
            decision.requires_manual_approval = True
        elif primary is not None and primary.value not in authorized:
            decision.eligibility_decision = "ALLOW"
            decision.decision = "NEED_INFO"
            decision.reason_code = "ACTION_NOT_AUTHORIZED_BY_POLICY"
            decision.reason_codes = ["ACTION_NOT_AUTHORIZED_BY_POLICY", *decision.reason_codes]
            decision.action_authorization = {**(decision.action_authorization or {}),
                                             "reason": "REQUESTED_ACTION_NOT_AUTHORIZED"}
            def _label(value: str) -> str:
                try:
                    spec = action_registry.spec(Action(value))
                except (ValueError, KeyError):
                    spec = None
                return spec.description if spec else value

            labels = [_label(a) for a in policy_actions]
            gate_text = ("您的情况符合售后条件，但当前政策只授权以下处理方式："
                         + "、".join(labels) +
                         "。请确认是否按该方式处理；如不同意，可转人工进一步沟通。")
    # 真实裁决落库：后续 Finance/审批/审计引用同一个 policy_decision_id
    policy_decision_id = await deps.persistence.record_policy_decision(
        policy_decision_id=str(uuid.uuid4()), tenant_id=state["tenant_id"], ticket_id=state["ticket_id"],
        decision=decision.decision, reason_code=decision.reason_code, rule_ids=decision.rule_ids,
        required_clause_ids=decision.required_clause_ids,
        amount_cap_minor=(decision.amount_cap.cap_minor if decision.amount_cap else None),
        policy_version=decision.policy_version, time_basis=decision.time_basis, as_of=decision.as_of,
        scope=decision.scope, trace=decision.trace)
    decision.policy_decision_id = policy_decision_id
    trace_summary = {
        "loaded_rule_count": decision.trace.get("loaded_rule_count"),
        "matched_rule_ids": decision.trace.get("matched_rule_ids"),
        "blocked_rules": decision.trace.get("blocked_rules"),
        "missing_facts": decision.trace.get("missing_facts"),
        "conflict": decision.trace.get("conflict"),
        "winner": decision.trace.get("winner"),
        "amount_check": decision.trace.get("amount_check"),
    }
    await deps.persistence.append_audit(
        tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], event="POLICY_DECIDED",
        payload={"policy_decision_id": policy_decision_id, "decision": decision.decision,
                 "reason_code": decision.reason_code, "rule_ids": decision.rule_ids,
                 "policy_version": decision.policy_version, "as_of": decision.as_of,
                 # 可复现三件套：规则集内容指纹 + 事实快照指纹 + 裁决时点
                 "ruleset_version": decision.ruleset_version,
                 "ruleset_hash": decision.ruleset_hash,
                 "facts_snapshot_id": decision.facts_snapshot_id,
                 "time_basis": decision.time_basis,
                 "requires_manual_approval": decision.requires_manual_approval,
                 "execution_blocked": decision.execution_blocked,
                 "eligibility_decision": decision.eligibility_decision,
                 "authorized_actions": decision.authorized_actions,
                 "trace": trace_summary})
    out = {"policy_decision": decision.model_dump(mode="json")}
    if gate_text:
        out["response_text_hint"] = gate_text
    return out


def route_after_policy(state) -> str:
    d = state.get("policy_decision") or {}
    if d.get("decision") == "DENY":
        return "deny"
    if d.get("decision") == "NEED_INFO":
        return "clarify"
    if d.get("decision") == "MANUAL":
        return "manual"
    return "retrieve"

# --------------------------------------------------------------------------- #
# 4) PolicyEvidence（合并 PolicyRetrieval + EvidenceGate）
# --------------------------------------------------------------------------- #
async def policy_evidence(state, deps: Deps) -> dict:
    decision = state.get("policy_decision") or {}
    bundle = await EvidenceService(deps.retriever).build_and_validate(
        tenant_id=state["tenant_id"], policy_decision=decision, query=_user_query(state),
        filters={"category": (state.get("order_snapshot") or {}).get("category")},
        history=_history_snippets(state, getattr(deps.settings, "rag_adaptive_context_messages", 4)))
    # 检索期 LLM 调用（多查询改写 / HyDE）落审计：node=policy_retrieval
    for call in ((bundle.diagnostics.get("retrieval") or {}).get("llm_calls") or []):
        await deps.persistence.record_llm_call(
            tenant_id=state["tenant_id"], ticket_id=state.get("ticket_id"), run_id=state.get("run_id"),
            trace_id=state.get("trace_id"), node="policy_retrieval", model=call.get("model"),
            prompt_version=call.get("prompt_version"), status="SUCCESS",
            request_hash=call.get("request_hash"), response_hash=call.get("response_hash"),
            input_tokens=(call.get("usage") or {}).get("prompt_tokens"),
    output_tokens=(call.get("usage") or {}).get("completion_tokens"),
    latency_ms=call.get("latency_ms"))
    await deps.persistence.append_audit(
        tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], event="EVIDENCE_BUILT",
        payload={"status": bundle.status.value, "required_clause_ids": bundle.required_clause_ids,
                 "missing_clause_ids": bundle.missing_clause_ids, "diagnostics": bundle.diagnostics,
                 "policy_decision_id": decision.get("policy_decision_id")})
    return {"evidence_bundle": bundle.model_dump(mode="json"), "evidence_status": bundle.status.value}


def route_after_evidence(state) -> str:
    status = state.get("evidence_status")
    if status == EvidenceStatus.INSUFFICIENT.value:
        return "clarify"
    if status in (EvidenceStatus.CONFLICT.value, EvidenceStatus.STALE.value):
        return "manual"
    # 资格咨询：证据齐了就直接给结论，不进入 planner/prepare/risk/command
    if (state.get("intent_result") or {}).get("speech_act") == SpeechAct.ELIGIBILITY_QUERY.value:
        return "answer"
    return "plan"


# --------------------------------------------------------------------------- #
# 5) ActionPlanner（LLM 只提案，喂真实事实）
# --------------------------------------------------------------------------- #
def _facts_digest(state) -> dict:
    order = state.get("order_snapshot") or {}
    return {
        "order": {"order_id": order.get("order_id"), "status": order.get("status"),
                  "version": order.get("version"), "category": order.get("category"),
                  "currency": order.get("currency"),
                  "lines": [{"order_line_id": ln.get("order_line_id"), "sku_id": ln.get("sku_id"),
                             "quantity": ln.get("quantity")} for ln in (order.get("lines") or [])]},
        "logistics": state.get("logistics_snapshot") or {},
        "finance": state.get("finance_snapshot") or {},
        "slots": state.get("slots") or {},
    }


def _evidence_digest(state) -> dict:
    bundle = state.get("evidence_bundle") or {}
    items = [{"clause_id": it.get("clause_id"), "is_exception": it.get("is_exception"),
              "text": (it.get("text") or "")[:300]} for it in (bundle.get("items") or [])[:5]]
    return {"status": bundle.get("status"), "required_clause_ids": bundle.get("required_clause_ids"),
            "missing_clause_ids": bundle.get("missing_clause_ids"), "items": items}


async def action_planner(state, deps: Deps) -> dict:
    """动作提案：确定性快路径优先，否则 LLM 规划；随后确定性 critic（+可选 LLM 复核）。

    只提案不授权：金额上限 / 证据要求 / 条款由 PolicyDecision、Evidence 决定，planner 不得修改。
    """
    intent = Intent((state.get("intent_result") or {}).get("intent", "UNKNOWN"))
    feedback = list(state.get("replan_feedback") or [])
    source = "llm"

    # 1) 快路径：意图明确 + 单动作 + 政策 ALLOW + 证据 OK + 必需槽位齐 -> 跳过 LLM
    plan = None
    if not feedback:
        plan = try_fastplan(state, enabled=deps.settings.planner_fastpath_enabled)
        if plan is not None:
            source = "fastpath"
            await deps.persistence.append_audit(
                tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], event="PLAN_FASTPATH",
                payload={"intent": str(intent),
                         "action": plan.actions[0].action.value if plan.actions else None})

    # 2) LLM 规划（快路径未命中 / 需要重规划）
    if plan is None:
        if deps.llm is None:
            plan = ActionPlan(order_id=(state.get("slots") or {}).get("order_id"), actions=[],
                              needs_more_info=True, missing_information=["LLM_DISABLED"], source="none")
            return {"action_plan": plan.model_dump(mode="json"), "plan_source": "none"}
        user = (f"PolicyDecision={state.get('policy_decision')}\n"
                f"Evidence={_evidence_digest(state)}\n"
                f"Facts={_facts_digest(state)}\n"
                f"Intent={state.get('intent_result')}")
        if feedback:
            user += "\n上一轮计划被审查驳回，请据此修正：" + "; ".join(feedback)
        try:
            plan, audit = await deps.llm.structured(
                messages=[{"role": "system", "content": deps.prompts.get("action_planner", "")},
                          {"role": "user", "content": user}],
                schema_model=ActionPlan, prompt_version=PROMPT_VERSION_PLANNER, temperature=0.0)
        except ValidationError as exc:  # 输出不合 schema
            await deps.persistence.record_llm_call(
                tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], run_id=state["run_id"],
                trace_id=state["trace_id"], node="action_planner", model=deps.settings.llm_model,
                prompt_version=PROMPT_VERSION_PLANNER, status="SCHEMA_ERROR", error_code=str(exc)[:200])
            plan = ActionPlan(order_id=(state.get("slots") or {}).get("order_id"), actions=[],
                              needs_more_info=True, missing_information=["PLANNER_SCHEMA_ERROR"])
            return {"action_plan": plan.model_dump(mode="json"), "error_code": "PLANNER_SCHEMA_ERROR"}
        except Exception as exc:  # 上游不可用：转人工，不掩盖为 schema 问题
            await deps.persistence.record_llm_call(
                tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], run_id=state["run_id"],
                trace_id=state["trace_id"], node="action_planner", model=deps.settings.llm_model,
                prompt_version=PROMPT_VERSION_PLANNER, status=_llm_failure_status(exc), error_code=str(exc)[:200])
            plan = ActionPlan(order_id=(state.get("slots") or {}).get("order_id"), actions=[],
                              needs_more_info=True, missing_information=["PLANNER_UPSTREAM_ERROR"])
            return {"action_plan": plan.model_dump(mode="json"), "error_code": "PLANNER_FAILED"}
        plan.source = "llm"
        await deps.persistence.record_llm_call(
            tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], run_id=state["run_id"],
            trace_id=state["trace_id"], node="action_planner", model=deps.settings.llm_model,
            prompt_version=PROMPT_VERSION_PLANNER, status="SUCCESS",
            request_hash=audit.get("request_hash"), response_hash=audit.get("response_hash"),
            input_tokens=(audit.get("usage") or {}).get("prompt_tokens"),
    output_tokens=(audit.get("usage") or {}).get("completion_tokens"),
    latency_ms=audit.get("latency_ms"))
        try:
            validate_plan_against_intent(plan, intent)
        except ValueError:
            return {"action_plan": plan.model_dump(mode="json"), "error_code": "PLAN_NOT_ALLOWED_FOR_INTENT"}
        # 政策动作授权：LLM 不得提出未被政策授权的写动作（fail-closed，转人工）
        authorized = set((state.get("policy_decision") or {}).get("authorized_actions") or [])
        unauthorized = [a.action.value for a in plan.actions
                        if a.action.value in EXTERNAL_WRITE_ACTIONS and a.action.value not in authorized]
        if unauthorized:
            await deps.persistence.append_audit(
                tenant_id=state["tenant_id"], ticket_id=state["ticket_id"],
                event="PLAN_ACTION_NOT_AUTHORIZED",
                payload={"actions": unauthorized, "authorized_actions": sorted(authorized),
                         "policy_decision_id": (state.get("policy_decision") or {}).get("policy_decision_id")})
            return {"action_plan": plan.model_dump(mode="json"),
                    "error_code": "PLAN_ACTION_NOT_AUTHORIZED"}

    # 3) 计划审查：确定性 critic（始终）+ 可选 LLM 复核（默认关闭）
    critique = deterministic_critique(state, plan, intent)
    if deps.settings.planner_llm_critic_enabled and critique.ok and plan.actions and deps.llm is not None:
        crit_out, crit_audit = await llm_critique(deps, state, plan)
        if crit_audit:
            await deps.persistence.record_llm_call(
                tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], run_id=state["run_id"],
                trace_id=state["trace_id"], node="plan_critic", model=deps.settings.llm_model,
                prompt_version=PROMPT_VERSION_PLAN_CRITIC, status="SUCCESS",
                request_hash=crit_audit.get("request_hash"), response_hash=crit_audit.get("response_hash"),
                input_tokens=(crit_audit.get("usage") or {}).get("prompt_tokens"),
                output_tokens=(crit_audit.get("usage") or {}).get("completion_tokens"))
        if crit_out is not None and (not crit_out.ok) and crit_out.severity == "HIGH":
            critique.add("LLM_CRITIC_HIGH", FATAL, "; ".join(crit_out.issues)[:200])

    out_state: dict = {"action_plan": plan.model_dump(mode="json"), "plan_source": source,
                       "plan_critic": critique.to_dict(), "replan_feedback": []}
    attempts = int(state.get("replanning_count") or 0)
    # 有界重规划：仅在"本轮不是重规划轮"且未达上限时，才发出新一轮反馈并计数
    if critique.needs_replan and not feedback and attempts < int(deps.settings.planner_max_replans or 0):
        out_state["replan_feedback"] = critique.feedback()
        out_state["replanning_count"] = attempts + 1

    # 4) 计划草稿落库：plan_id 前移（审批/风控/命令引用同一 id）
    if plan.actions and not critique.fatal:
        draft = {"plan": plan.model_dump(mode="json"),
                 "policy_decision_id": (state.get("policy_decision") or {}).get("policy_decision_id"),
                 "rule_ids": (state.get("policy_decision") or {}).get("rule_ids") or [],
                 "evidence_clause_ids": [it.get("clause_id")
                                         for it in ((state.get("evidence_bundle") or {}).get("items") or [])],
                 "order_version": (state.get("order_snapshot") or {}).get("version")}
        # plan_hash 必须是**内容指纹**：policy_decision_id 每次运行随机生成，
        # 若参与哈希，同一诉求重复提交就会得到不同 plan_hash，导致审批任务与退款申请
        # 各生成一份（只有命令层靠 operation 幂等键兜住）。这里只哈希可复现的业务内容。
        ph = payload_hash({k: v for k, v in draft.items() if k != "policy_decision_id"})
        plan_id = await deps.persistence.create_plan_draft(
            tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], plan_json=draft, plan_hash=ph)
        out_state["plan_id"] = plan_id
        out_state["plan_hash"] = ph

    await deps.persistence.append_audit(
        tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], event="PLAN_PROPOSED",
        payload={"source": source, "plan_id": out_state.get("plan_id"), "plan_hash": out_state.get("plan_hash"),
                 "actions": [a.action.value for a in plan.actions], "critique": critique.to_dict(),
                 "replan": bool(out_state.get("replan_feedback")),
                 "replanning_count": attempts})
    return out_state


def route_after_plan(state) -> str:
    if state.get("error_code") in ("PLANNER_FAILED", "PLANNER_SCHEMA_ERROR", "PLAN_NOT_ALLOWED_FOR_INTENT",
                                    "PLAN_ACTION_NOT_AUTHORIZED"):
        return "manual"
    critique = state.get("plan_critic") or {}
    if critique.get("fatal"):
        return "manual"
    if critique.get("needs_replan") and state.get("replan_feedback"):
        return "replan"
    if critique.get("needs_clarify"):
        return "clarify"
    plan = state.get("action_plan") or {}
    if plan.get("needs_more_info") or not plan.get("actions"):
        return "clarify"
    return "prepare"


# --------------------------------------------------------------------------- #
# 6) ActionPreparation（0 金额申请 -> 只读报价 -> 冻结计划，并回写 plan_id）
# --------------------------------------------------------------------------- #
def _quote_items(state, plan: ActionPlan) -> tuple[list[dict], str | None]:
    """用 OMS 校验过的订单行构造报价明细；无法映射时返回错误码，绝不用证据 ID 冒充订单行。"""
    lines = (state.get("order_snapshot") or {}).get("lines") or []
    if not lines:
        return [], "ORDER_LINES_UNAVAILABLE"
    by_sku: dict[str, list[dict]] = {}
    for ln in lines:
        if ln.get("sku_id"):
            by_sku.setdefault(ln["sku_id"], []).append(ln)
    quantity = int((state.get("slots") or {}).get("quantity") or 1)
    items: list[dict] = []
    for a in plan.actions:
        if a.action.value not in REFUND_ACTIONS:
            continue
        for sku in (a.sku_ids or [None]):
            if sku is None:
                return [], "SKU_MISSING"
            matched = by_sku.get(sku)
            if not matched:
                return [], "SKU_NOT_IN_ORDER"
            available = sum(int(ln.get("quantity") or 0) for ln in matched)
            if quantity > available:
                return [], "QUANTITY_EXCEEDS_ORDER"
            items.append({"order_line_id": matched[0].get("order_line_id"), "sku_id": sku,
                          "quantity": quantity})
    if not items:
        return [], "REFUND_ITEMS_EMPTY"
    return items, None


def _plan_fingerprint(plan: ActionPlan, items: list | None, quote: dict | None,
                      order_version: str | None) -> dict:
    """plan_hash 的稳定输入：只含业务内容与金额，不含随机 ID 与时间戳。

    同一诉求重复提交必须得到同一个 plan_hash，才能让审批、退款申请、命令三层同时幂等。
    """
    # order_id 显式入指纹：两个订单即使行项目结构、金额完全相同，也绝不能被合并成同一计划
    fp = {"order_id": plan.order_id, "plan": plan.model_dump(mode="json"),
          "order_version": order_version}
    if items is not None:
        fp["items"] = items
    if quote is not None:
        fp["quote"] = {k: quote.get(k) for k in ("amount_minor", "currency", "order_version")}
    return fp


async def action_preparation(state, deps: Deps) -> dict:
    set_tool_context(tenant_id=state.get("tenant_id"), ticket_id=state.get("ticket_id"),
                     run_id=state.get("run_id"), trace_id=state.get("trace_id"), node="action_preparation")
    plan = ActionPlan.model_validate(state.get("action_plan") or {})
    decision = state.get("policy_decision") or {}
    refund_actions = [a for a in plan.actions if a.action.value in REFUND_ACTIONS]
    if refund_actions:
        # 金额/关键事实未核验时不允许进入资金执行（业务资格可以保留，但执行必须阻断）
        if decision.get("execution_blocked"):
            await deps.persistence.append_audit(
                tenant_id=state["tenant_id"], ticket_id=state["ticket_id"],
                event="EXECUTION_BLOCKED", payload={"reason": "POLICY_EXECUTION_BLOCKED",
                                                    "policy_decision_id": decision.get("policy_decision_id")})
            return {"error_code": "POLICY_EXECUTION_BLOCKED"}
        items, err = _quote_items(state, plan)
        if err:
            return {"error_code": err}
        refund_op = "+".join(sorted({a.action.value for a in refund_actions}))
        refund_request_id = await deps.persistence.create_refund_request(
            tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], order_id=plan.order_id,
            policy_decision_id=decision.get("policy_decision_id"),
            currency=(state.get("finance_snapshot") or {}).get("currency", "CNY"),
            # 幂等范围：租户 + 工单 + 操作类型 + 稳定计划指纹（plan_hash 已含 order_id/金额）
            idempotency_key=(f"refund:{state.get('tenant_id') or ''}:{state['ticket_id']}:"
                             f"{refund_op}:{state.get('plan_hash') or ''}"),
            payload_hash=payload_hash({"plan": plan.model_dump(mode="json"), "items": items}))
        quote = await deps.finance.calculate_refund_amount(
            tenant_id=state["tenant_id"], refund_request_id=refund_request_id, order_id=plan.order_id,
            items=items, policy_decision_id=decision.get("policy_decision_id") or "",
            order_version=(state.get("order_snapshot") or {}).get("version", ""))
        await deps.persistence.insert_quote(tenant_id=state["tenant_id"],
                                           refund_request_id=refund_request_id, quote=quote)
        # 报价即预占：拿不到 hold 说明可用余额不足 -> fail-closed，绝不"先放行后兜底"
        reservation_ref = quote.get("reservation_reference")
        if not reservation_ref:
            await deps.persistence.append_audit(
                tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], event="REFUND_RESERVATION_FAILED",
                payload={"refund_request_id": refund_request_id, "amount_minor": quote.get("amount_minor"),
                         "available_balance_minor": quote.get("available_balance_minor"),
                         "reason": (quote.get("breakdown") or {}).get("reason")})
            return {"error_code": "BALANCE_INSUFFICIENT", "refund_request_id": refund_request_id}
        await deps.persistence.append_audit(
            tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], event="REFUND_RESERVED",
            payload={"refund_request_id": refund_request_id, "reservation_ref": reservation_ref,
                     "amount_minor": quote.get("amount_minor"),
                     "expires_at": quote.get("reservation_expires_at"),
                     "available_balance_minor": quote.get("available_balance_minor")})
        plan_json = {"plan": plan.model_dump(mode="json"), "items": items,
                     "quote": {k: quote.get(k) for k in ("quote_id", "quote_hash", "amount_minor", "currency",
                                                          "reservation_reference", "reservation_expires_at")},
                     "order_version": (state.get("order_snapshot") or {}).get("version"),
                     "policy_decision_id": decision.get("policy_decision_id")}
        # plan_hash 是幂等键：只取业务内容。若掺入 policy_decision_id / quote_id /
        # reservation_reference / 过期时间等每次易变的值，重复提交会得到不同哈希，
        # 直接导致重复建审批、重复退款申请与重复命令。
        ph = payload_hash(_plan_fingerprint(plan, items, quote,
                                           (state.get("order_snapshot") or {}).get("version")))
        plan_id = await deps.persistence.freeze_plan(
            tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], plan_json=plan_json,
            plan_hash=ph, status="VALIDATED", plan_id=state.get("plan_id"))
        frozen_fv = state.get("facts_version")
        update = {"refund_request_id": refund_request_id, "plan_id": plan_id, "plan_hash": ph,
                  "plan_frozen_facts_version": frozen_fv,
                  "quote_id": quote.get("quote_id"), "quote_hash": quote.get("quote_hash"),
                  "quote_amount_minor": quote.get("amount_minor"), "quote_currency": quote.get("currency"),
                  "quote_expires_at": quote.get("expires_at"), "quote_order_version": quote.get("order_version"),
                  "quote_policy_decision_id": quote.get("policy_decision_id"),
                  "reservation_ref": reservation_ref,
                  "reservation_expires_at": quote.get("reservation_expires_at"),
                  "reservation_amount_minor": quote.get("amount_minor")}
    else:
        plan_json = {"plan": plan.model_dump(mode="json"),
                     "order_version": (state.get("order_snapshot") or {}).get("version"),
                     "policy_decision_id": decision.get("policy_decision_id")}
        ph = payload_hash(_plan_fingerprint(plan, None, None,
                                           (state.get("order_snapshot") or {}).get("version")))
        plan_id = await deps.persistence.freeze_plan(
            tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], plan_json=plan_json,
            plan_hash=ph, status="VALIDATED", plan_id=state.get("plan_id"))
        update = {"plan_id": plan_id, "plan_hash": ph,
                  "plan_frozen_facts_version": state.get("facts_version")}
    await deps.persistence.append_audit(
        tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], event="PLAN_FROZEN",
        payload={"plan_id": update.get("plan_id"), "plan_hash": update.get("plan_hash"),
                 "quote_id": update.get("quote_id"), "amount_minor": update.get("quote_amount_minor"),
                 "refund_request_id": update.get("refund_request_id"),
                 "reservation_ref": update.get("reservation_ref")})
    return update

# --------------------------------------------------------------------------- #
# 7) RiskAuthorization（按动作类型选择确定性风控规则）
# --------------------------------------------------------------------------- #
# 风控结论严格程度：逐动作取最严者（DENY > MANUAL/NEED_INFO > REVIEW > AUTO_APPROVE）
_RISK_SEVERITY = {
    RiskDecision.DENY.value: 4,
    RiskDecision.MANUAL.value: 3,
    RiskDecision.NEED_INFO.value: 3,
    RiskDecision.REVIEW.value: 2,
    RiskDecision.AUTO_APPROVE.value: 1,
}


def _combine_risk(outcomes: list[tuple[str, RiskOutcome]]) -> RiskOutcome:
    """把逐动作风控合并为单一结论：任一动作被拒/转人工，整体即被拒/转人工。

    reason_codes 统一加 ``ACTION:`` 前缀，便于审计定位是哪个动作触发。
    """
    if not outcomes:
        return evaluate_risk(None, {})  # 无可授权动作 -> MANUAL(NO_ACTION_TO_AUTHORIZE)
    worst = max(outcomes, key=lambda item: _RISK_SEVERITY.get(item[1].decision.value, 3))[1]
    codes = [f"{action}:{code}" for action, outcome in outcomes for code in outcome.reason_codes]
    return RiskOutcome(decision=worst.decision, reason_codes=codes)


def _risk_params(settings) -> dict:
    """风控阈值（来自配置）：频次/累计金额/黑名单。"""
    return {
        "max_refund_count_30d": int(getattr(settings, "risk_max_refund_count_30d", 5) or 5),
        "max_refund_amount_30d_minor": int(getattr(settings, "risk_max_refund_amount_30d_minor", 500000) or 500000),
        "blacklist_deny": bool(getattr(settings, "risk_blacklist_deny", True)),
    }


async def risk_authorization(state, deps: Deps) -> dict:
    """逐动作确定性风控；全部写动作取最严结论，任一动作不通过即不进入执行。"""
    actions = _write_actions(state)
    params = _risk_params(deps.settings)
    outcomes = [(action, evaluate_risk(action, state, params)) for action in actions]
    combined = _combine_risk(outcomes)
    risk_id = await deps.persistence.record_risk_decision(
        tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], plan_hash=state.get("plan_hash"),
        quote_id=state.get("quote_id"), decision=combined.decision.value, reason_codes=combined.reason_codes)
    await deps.persistence.append_audit(
        tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], event="RISK_DECIDED",
        payload={"actions": actions, "action": _primary_action(state),
                 "decision": combined.decision.value, "reason_codes": combined.reason_codes,
                 "by_action": {action: {"decision": outcome.decision.value,
                                        "reason_codes": outcome.reason_codes}
                               for action, outcome in outcomes},
                 "plan_id": state.get("plan_id"), "risk_decision_id": risk_id,
                 "quote_amount_minor": state.get("quote_amount_minor")})
    return {"risk_decision": combined.decision.value, "risk_reason_codes": combined.reason_codes,
            "risk_decision_id": risk_id,
            "risk_by_action": {action: outcome.decision.value for action, outcome in outcomes}}


def route_after_risk(state) -> str:
    d = state.get("risk_decision")
    if d == RiskDecision.DENY.value:
        return "deny"
    if d == RiskDecision.REVIEW.value:
        return "hitl"
    if d in (RiskDecision.MANUAL.value, RiskDecision.NEED_INFO.value):
        return "manual"
    return "guard"


# --------------------------------------------------------------------------- #
# 8) HITLReview（interrupt 边界见 §18.3；interrupt 之前的代码必须幂等）
# --------------------------------------------------------------------------- #
async def hitl_review(state, deps: Deps) -> dict:
    approval = await deps.persistence.ensure_approval_task(
        tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], plan_id=state.get("plan_id"),
        plan_hash=state.get("plan_hash"), quote_id=state.get("quote_id"),
        expected_version=state.get("facts_version"))
    # interrupt() 恢复会重放本节点：只有“本次真的新建了审批任务”时才落审计，避免重复事件
    if approval.get("created"):
        await deps.persistence.append_audit(
            tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], event="APPROVAL_REQUIRED",
            payload={"approval_id": approval.get("approval_id"), "plan_id": state.get("plan_id"),
                     "plan_hash": approval.get("plan_hash"), "expires_at": approval.get("expires_at")})
    interrupt({"approval_id": approval.get("approval_id"), "plan_hash": approval.get("plan_hash"),
               "plan_id": state.get("plan_id"), "expires_at": approval.get("expires_at")})
    verified = await deps.persistence.load_and_verify_approval(
        approval_id=approval.get("approval_id"), tenant_id=state["tenant_id"])
    # 审批不得跨工单复用：即使 plan_hash 相同，也必须是本工单的审批
    if verified.get("ticket_id") and verified["ticket_id"] != str(state["ticket_id"]):
        await deps.persistence.append_audit(
            tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], event="APPROVAL_TICKET_MISMATCH",
            payload={"approval_id": verified.get("approval_id"),
                     "approval_ticket_id": verified["ticket_id"]})
        return {"approval_id": verified.get("approval_id"), "approval_status": "REJECTED",
                "error_code": "APPROVAL_TICKET_MISMATCH"}
    # 绑定校验：审批必须对应本计划的 plan_hash，且未超时（审批事实不得被"换计划"复用）
    vph, sph = verified.get("plan_hash"), state.get("plan_hash")
    if verified.get("status") == "APPROVED" and vph and sph and vph != sph:
        await deps.persistence.append_audit(
            tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], event="APPROVAL_PLAN_MISMATCH",
            payload={"approval_plan_hash": vph, "state_plan_hash": sph})
        return {"approval_id": verified.get("approval_id"), "approval_status": "REJECTED",
                "error_code": "APPROVAL_PLAN_MISMATCH"}
    if verified.get("status") == "APPROVED" and verified.get("expired"):
        await deps.persistence.append_audit(
            tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], event="APPROVAL_EXPIRED_ON_RESUME",
            payload={"approval_id": verified.get("approval_id")})
        return {"approval_id": verified.get("approval_id"), "approval_status": "EXPIRED",
                "error_code": "APPROVAL_EXPIRED"}
    await deps.persistence.append_audit(
        tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], event="APPROVAL_RESOLVED",
        payload={"approval_id": verified.get("approval_id"), "status": verified.get("status")})
    return {"approval_id": verified.get("approval_id"), "approval_status": verified.get("status")}


def route_after_hitl(state) -> str:
    if state.get("approval_status") == "APPROVED":
        return "guard"
    return "deny"


# --------------------------------------------------------------------------- #
# 9) PreExecutionGuard（所有动作都要过，逐动作前置条件）
# --------------------------------------------------------------------------- #
def _guard_common(state) -> dict:
    """公共前置：计划已冻结 + 事实版本未漂移（报价版本 + 全量 facts_version 双重 CAS）。"""
    if not state.get("plan_hash"):
        return {"error_code": "PLAN_NOT_FROZEN"}
    quote_version = state.get("quote_order_version")
    order_version = (state.get("order_snapshot") or {}).get("version")
    if quote_version and order_version and str(quote_version) != str(order_version):
        return {"error_code": "STATE_DRIFT", "drift": {"quote_order_version": quote_version,
                                                      "order_version": order_version}}
    frozen = state.get("plan_frozen_facts_version")
    current = state.get("facts_version")
    if frozen and current and frozen != current:
        return {"error_code": "STATE_DRIFT", "drift": {"frozen_facts_version": frozen,
                                                      "current_facts_version": current}}
    return {}


def _requote_or_exceed(state, deps: Deps, code: str) -> dict:
    """报价/预占过期：未达上限则回 action_preparation 重新报价；达上限则转人工（防死循环）。"""
    attempts = int(state.get("requote_count") or 0)
    limit = int(getattr(deps.settings, "max_requote_cycles", 0) or 0)
    if attempts >= limit:
        return {"error_code": "REQUOTE_EXCEEDED", "requote_count": attempts}
    return {"error_code": code, "requote_count": attempts + 1}


async def _guard_refund(state, deps: Deps) -> dict:
    if not state.get("quote_id"):
        return {"error_code": "QUOTE_MISSING"}
    # 资金预占（hold）：报价必须绑定一笔有效预占，金额一致才放行
    if not state.get("reservation_ref"):
        return {"error_code": "RESERVATION_MISSING"}
    if state.get("reservation_amount_minor") != state.get("quote_amount_minor"):
        return {"error_code": "RESERVATION_MISMATCH"}
    quote_expires = _parse_ts(state.get("quote_expires_at"))
    if quote_expires and datetime.now(timezone.utc) > quote_expires:
        return _requote_or_exceed(state, deps, "QUOTE_EXPIRED")
    hold_expires = _parse_ts(state.get("reservation_expires_at"))
    if hold_expires and datetime.now(timezone.utc) > hold_expires:
        return _requote_or_exceed(state, deps, "RESERVATION_EXPIRED")
    order_id = (state.get("slots") or {}).get("order_id") or (state.get("order_snapshot") or {}).get("order_id")
    bal = await deps.finance.get_refundable_balance(tenant_id=state["tenant_id"], order_id=order_id)
    amount = state.get("quote_amount_minor") or 0
    if amount > bal.refundable_balance_minor:
        return {"error_code": "BALANCE_INSUFFICIENT"}
    if state.get("risk_decision") == RiskDecision.REVIEW.value and state.get("approval_status") != "APPROVED":
        return {"error_code": "APPROVAL_MISSING"}
    return {"error_code": None}


async def _guard_intercept(state, deps: Deps) -> dict:
    expected_waybill = (state.get("logistics_snapshot") or {}).get("waybill_no") \
        or (state.get("slots") or {}).get("waybill_no")
    if not expected_waybill:
        return {"error_code": "WAYBILL_MISSING"}
    tracking = await deps.logistics.get_tracking_info(
        tenant_id=state["tenant_id"], order_id=(state.get("slots") or {}).get("order_id"),
        waybill_no=expected_waybill)
    if tracking.waybill_no and tracking.waybill_no != expected_waybill:
        return {"error_code": "WAYBILL_MISMATCH"}
    if tracking.parcel_status in DELIVERED_PARCEL_STATES:
        return {"error_code": "PARCEL_ALREADY_DELIVERED"}
    if tracking.parcel_status in (None, "UNKNOWN"):
        return {"error_code": "LOGISTICS_STATUS_UNKNOWN"}
    return {"error_code": None}


async def _guard_expedite(state, deps: Deps) -> dict:
    waybill = (state.get("logistics_snapshot") or {}).get("waybill_no") or (state.get("slots") or {}).get("waybill_no")
    if not waybill:
        return {"error_code": "WAYBILL_MISSING"}
    tracking = await deps.logistics.get_tracking_info(
        tenant_id=state["tenant_id"], order_id=(state.get("slots") or {}).get("order_id"), waybill_no=waybill)
    if tracking.parcel_status in DELIVERED_PARCEL_STATES:
        return {"error_code": "PARCEL_ALREADY_DELIVERED"}
    if tracking.parcel_status in (None, "UNKNOWN"):
        return {"error_code": "LOGISTICS_STATUS_UNKNOWN"}
    return {"error_code": None}


async def _guard_reshipment(state, deps: Deps) -> dict:
    skus = [s for a in (state.get("action_plan") or {}).get("actions", []) for s in (a.get("sku_ids") or [])]
    if not skus:
        return {"error_code": "SKU_MISSING"}
    order = await deps.orders.get_order_detail(
        tenant_id=state["tenant_id"], order_id=(state.get("slots") or {}).get("order_id"))
    if order.status in ("CANCELLED", "CLOSED", "REFUNDED"):
        return {"error_code": "ORDER_STATE_NOT_RESHIPPABLE"}
    lines = order.lines or []
    if not lines:
        return {"error_code": "ORDER_LINES_UNAVAILABLE"}
    by_sku: dict[str, int] = {}
    for ln in lines:
        by_sku[ln.get("sku_id")] = by_sku.get(ln.get("sku_id"), 0) + int(ln.get("quantity") or 0)
    quantity = int((state.get("slots") or {}).get("quantity") or 1)
    for sku in skus:
        if sku not in by_sku:
            return {"error_code": "SKU_NOT_IN_ORDER"}
        if quantity > by_sku[sku]:
            return {"error_code": "QUANTITY_EXCEEDS_ORDER"}
    return {"error_code": None}


async def _guard_action(action: str, state, deps: Deps) -> dict:
    """单动作执行前复核；无法识别的动作一律不放行。"""
    if action in REFUND_ACTIONS:
        return await _guard_refund(state, deps)
    if action == Action.INTERCEPT_PARCEL.value:
        return await _guard_intercept(state, deps)
    if action == Action.EXPEDITE_DELIVERY.value:
        return await _guard_expedite(state, deps)
    if action == Action.CREATE_RESHIPMENT.value:
        return await _guard_reshipment(state, deps)
    return {"error_code": "UNSUPPORTED_ACTION"}


async def pre_execution_guard(state, deps: Deps) -> dict:
    """审批后执行前的统一复核：计划内**每个**外部写动作都要逐一复核。

    历史实现只看第一个外部写动作，而 command_submitter 会提交全部写动作；
    计划含第二个写动作时，它会绕过执行前复核（金额/物流漂移都可能漏检）。
    """
    set_tool_context(tenant_id=state.get("tenant_id"), ticket_id=state.get("ticket_id"),
                     run_id=state.get("run_id"), trace_id=state.get("trace_id"), node="pre_execution_guard")
    actions = _write_actions(state)
    if not actions:
        # 计划里有动作但都不是可执行写动作（如 ESCALATE_MANUAL/CLOSE_TICKET）-> 转人工，不放行
        return {"error_code": "UNSUPPORTED_ACTION"} if _plan_actions(state) else {"error_code": None}
    # 执行前再核一次政策授权：防止审批期间计划/事实被替换成未授权动作
    policy_authorized = set(state.get("validated_actions")
                            or (state.get("policy_decision") or {}).get("authorized_actions") or [])
    if any(a not in policy_authorized for a in actions):
        return {"error_code": "POLICY_ACTION_NOT_AUTHORIZED"}
    common = _guard_common(state)
    if common:
        return common
    for action in actions:
        result = await _guard_action(action, state, deps)
        if result.get("error_code"):
            return result
    return {"error_code": None}


def route_after_guard(state) -> str:
    code = state.get("error_code")
    if code in ("QUOTE_EXPIRED", "RESERVATION_EXPIRED"):
        return "reprepare"
    if code == "STATE_DRIFT":
        return "rerisk"
    if code == "REQUOTE_EXCEEDED":
        return "manual"
    if code in ("UNSUPPORTED_ACTION", "RESERVATION_MISSING", "RESERVATION_MISMATCH",
                "POLICY_ACTION_NOT_AUTHORIZED"):
        return "manual"
    if code:
        return "deny"
    return "execute"

# --------------------------------------------------------------------------- #
# 10) CommandSubmitter（只写持久化命令 + Outbox；外部写由 Saga Dispatcher 发出）
# --------------------------------------------------------------------------- #
async def command_submitter(state, deps: Deps) -> dict:
    plan = state.get("action_plan") or {}
    # 纵深防御：只提交“本轮已被风控逐动作评估过”的写动作，否则一条命令都不写（fail-closed）
    actions = _write_actions(state)
    # 第一道闸：政策动作授权（计划动作必须 ⊆ policy_decision.authorized_actions）
    policy_authorized = set(state.get("validated_actions")
                            or (state.get("policy_decision") or {}).get("authorized_actions") or [])
    if any(a not in policy_authorized for a in actions):
        await deps.persistence.append_audit(
            tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], event="COMMAND_SUBMIT_BLOCKED",
            payload={"reason": "POLICY_ACTION_NOT_AUTHORIZED", "actions": actions,
                     "authorized_actions": sorted(policy_authorized), "plan_id": state.get("plan_id")})
        return {"error_code": "POLICY_ACTION_NOT_AUTHORIZED", "command_ids": []}
    # 第二道闸：每个写动作都必须被风控逐动作评估过
    authorized = set(state.get("risk_by_action") or {})
    if actions and not authorized.issuperset(actions):
        missing = [a for a in actions if a not in authorized]
        await deps.persistence.append_audit(
            tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], event="COMMAND_SUBMIT_BLOCKED",
            payload={"reason": "ACTION_NOT_RISK_AUTHORIZED", "actions": actions, "missing": missing,
                     "plan_id": state.get("plan_id")})
        return {"error_code": "ACTION_NOT_RISK_AUTHORIZED", "command_ids": []}
    command_ids: list[str] = []
    for a in _topo_external_writes(plan):
        action = a.get("action")
        if action not in EXTERNAL_WRITE_ACTIONS:
            continue
        sku_ids = a.get("sku_ids") or []
        authorization_ref = (f"approval:{state['approval_id']}" if state.get("approval_id")
                             else (f"plan:{state['plan_id']}" if state.get("plan_id") else None))
        cid = await deps.persistence.enqueue_command(
            tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], run_id=state["run_id"],
            action=action, order_id=plan.get("order_id"),
            refund_request_id=state.get("refund_request_id"),
            quote_id=state.get("quote_id"), quote_hash=state.get("quote_hash"),
            waybill_no=(state.get("logistics_snapshot") or {}).get("waybill_no")
            or (state.get("slots") or {}).get("waybill_no"),
            sku_ids=sku_ids, plan_id=state.get("plan_id"), authorization_ref=authorization_ref,
            priority=action_registry.priority_of(action),
            idempotency_key=_command_idempotency_key(action, a, state),
            payload_hash=payload_hash({"plan": plan, "plan_id": state.get("plan_id"),
                                       "quote_id": state.get("quote_id")}),
            payload={"action": action, "order_id": plan.get("order_id"),
                     "plan_id": state.get("plan_id"), "approval_id": state.get("approval_id"),
                     "risk_decision_id": state.get("risk_decision_id"),
                     "quote_id": state.get("quote_id"), "quote_hash": state.get("quote_hash"),
                     "reservation_ref": state.get("reservation_ref"),
                     "refund_request_id": state.get("refund_request_id"), "sku_ids": sku_ids})
        command_ids.append(cid)
    status = "WAITING_EXTERNAL" if command_ids else "COMPLETED"
    await deps.persistence.append_audit(
        tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], event="COMMANDS_SUBMITTED",
        payload={"command_ids": command_ids, "plan_id": state.get("plan_id"),
                 "approval_id": state.get("approval_id"), "quote_id": state.get("quote_id"),
                 "refund_request_id": state.get("refund_request_id")})
    return {"command_ids": command_ids, "response_status": status}


# --------------------------------------------------------------------------- #
# 11) ResultVerifier（按 command_id 核验；UNKNOWN/未结算幂等建对账任务）
# --------------------------------------------------------------------------- #
async def _schedule_reconciliation(state, deps: Deps, *, reason: str) -> str:
    """幂等创建对账任务（同一 ticket + reason 的未完成任务不重复建）。

    单命令场景带 `command_id`，让 Worker 能按命令级（而非仅退款单级）对账。
    """
    cids = list(state.get("command_ids") or [])
    return await deps.persistence.create_reconciliation_task(
        tenant_id=state["tenant_id"], ticket_id=state["ticket_id"],
        refund_request_id=state.get("refund_request_id"), reason=reason,
        command_id=(cids[0] if len(cids) == 1 else None))


async def _verify_business_authority(state, deps: Deps, command: dict) -> str:
    """命令确认后回查业务权威系统。返回 COMPLETED / WAITING / FAILED。"""
    action = command.get("action")
    if action in REFUND_ACTIONS:
        rrid = state.get("refund_request_id") or command.get("refund_request_id")
        if not rrid:
            return "WAITING"
        st = await deps.finance.get_refund_status(tenant_id=state["tenant_id"], refund_request_id=rrid)
        s = (st or {}).get("status", "UNKNOWN")
        if s in ("SUCCEEDED", "SETTLED"):
            return "COMPLETED"
        if s == "FAILED_CONFIRMED":
            return "FAILED"
        return "WAITING"
    if action == Action.INTERCEPT_PARCEL.value:
        waybill = (state.get("logistics_snapshot") or {}).get("waybill_no") \
            or (state.get("slots") or {}).get("waybill_no")
        if not waybill:
            return "WAITING"
        st = await deps.logistics.get_interception_status(tenant_id=state["tenant_id"], waybill_no=waybill)
        s = str((st or {}).get("intercept_command_status") or (st or {}).get("status") or "UNKNOWN")
        if s in INTERCEPT_DONE_STATES:
            return "COMPLETED"
        if s in INTERCEPT_FAILED_STATES:
            return "FAILED"
        return "WAITING"
    if action == Action.CREATE_RESHIPMENT.value:
        order_id = (command.get("order_id") or (state.get("order_snapshot") or {}).get("order_id")
                    or (state.get("slots") or {}).get("order_id"))
        if not order_id:
            return "WAITING"
        st = await deps.orders.get_reshipment_status(tenant_id=state["tenant_id"], order_id=order_id)
        s = st.reshipment_status or "UNKNOWN"
        if s in RESHIPMENT_DONE_STATES:
            return "COMPLETED"
        if s in RESHIPMENT_FAILED_STATES:
            return "FAILED"
        return "WAITING"
    if action == Action.EXPEDITE_DELIVERY.value:
        waybill = (command.get("waybill_no") or (state.get("logistics_snapshot") or {}).get("waybill_no")
                   or (state.get("slots") or {}).get("waybill_no"))
        if not waybill:
            return "WAITING"
        st = await deps.logistics.get_dispatch_status(tenant_id=state["tenant_id"], waybill_no=waybill)
        s = st.dispatch_status or "UNKNOWN"
        if s in DISPATCH_DONE_STATES:
            return "COMPLETED"
        if s in DISPATCH_FAILED_STATES:
            return "FAILED"
        return "WAITING"
    # 其它动作无权威端口：不谎报完成，交对账收敛
    return "WAITING"


async def result_verifier(state, deps: Deps) -> dict:
    set_tool_context(tenant_id=state.get("tenant_id"), ticket_id=state.get("ticket_id"),
                     run_id=state.get("run_id"), trace_id=state.get("trace_id"), node="result_verifier")
    command_ids = list(state.get("command_ids") or [])
    if not command_ids:
        return {"response_status": "COMPLETED", "command_results": []}
    statuses = await deps.persistence.list_command_statuses(tenant_id=state["tenant_id"],
                                                           command_ids=command_ids)
    by_id = {s["command_id"]: s for s in statuses}
    missing = [cid for cid in command_ids if cid not in by_id]
    if not by_id:
        task_id = await _schedule_reconciliation(state, deps, reason="COMMAND_NOT_VISIBLE")
        return {"response_status": "WAITING_EXTERNAL", "wait_reason": "COMMAND_NOT_VISIBLE",
                "reconciliation_task_id": task_id, "command_results": []}
    if any(s.get("status") == "FAILED_CONFIRMED" for s in by_id.values()):
        task_id = await _schedule_reconciliation(state, deps, reason="FAILED_COMMAND")
        await deps.persistence.append_audit(
            tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], event="COMMAND_FAILED",
            payload={"commands": list(by_id.values())})
        return {"response_status": "FAILED", "wait_reason": "FAILED_CONFIRMED",
                "reconciliation_task_id": task_id, "command_results": list(by_id.values())}
    if [s for s in by_id.values() if s.get("status") in PENDING_COMMAND_STATES] or missing:
        task_id = await _schedule_reconciliation(state, deps, reason="WAITING_COMMAND_RESULT")
        return {"response_status": "WAITING_EXTERNAL", "wait_reason": "COMMAND_PENDING",
                "reconciliation_task_id": task_id, "command_results": list(by_id.values())}

    verdicts = {cid: await _verify_business_authority(state, deps, cmd) for cid, cmd in by_id.items()}
    if "FAILED" in verdicts.values():
        task_id = await _schedule_reconciliation(state, deps, reason="BUSINESS_FAILED")
        return {"response_status": "FAILED", "wait_reason": "BUSINESS_FAILED",
                "reconciliation_task_id": task_id, "command_results": list(by_id.values())}
    if "WAITING" in verdicts.values():
        task_id = await _schedule_reconciliation(state, deps, reason="AWAITING_BUSINESS_SETTLEMENT")
        return {"response_status": "WAITING_EXTERNAL", "wait_reason": "AWAITING_BUSINESS_SETTLEMENT",
                "reconciliation_task_id": task_id, "command_results": list(by_id.values())}
    return {"response_status": "COMPLETED", "command_results": list(by_id.values())}


# --------------------------------------------------------------------------- #
# 12) ResponseComposer（合并 ClarificationWriter + AuditResponder）
# --------------------------------------------------------------------------- #
def classify_response(state) -> str:
    """回复类型判定（显式优先级，自上而下短路）：

    1. 路由层提示 `response_type_hint`（意图/只读答复已定型的最终态）
    2. 终态 `response_status`（WAITING_USER / WAITING_EXTERNAL / FAILED）
    3. 政策裁决 DENY
    4. 错误码 / 政策 MANUAL / 风控 MANUAL
    5. 政策 NEED_INFO / 计划缺信息
    6. 已提交命令 -> ACCEPTED；政策 ALLOW -> SUCCEEDED；否则 UNKNOWN
    """
    hint = state.get("response_type_hint")
    if hint:
        return hint
    # 人工审批被拒绝/过期：终态必须是 DENIED，不能因为政策 ALLOW 就报 SUCCEEDED
    if state.get("approval_status") in ("REJECTED", "EXPIRED"):
        return "DENIED"
    decision = state.get("policy_decision") or {}
    kind = decision.get("decision")
    status = state.get("response_status")
    # 资格咨询只回答"能不能"，即使政策 ALLOW 也不代表已执行
    if (state.get("intent_result") or {}).get("speech_act") == SpeechAct.ELIGIBILITY_QUERY.value:
        if kind == "DENY":
            return "DENIED"
        if kind in ("NEED_INFO",):
            return "NEED_INFO"
        if kind == "MANUAL":
            return "MANUAL"
        return "ANSWER"
    if status == "WAITING_USER":
        return "NEED_INFO"
    if status == "WAITING_EXTERNAL":
        return "PROCESSING"
    if status == "FAILED":
        return "FAILED"
    if kind == "DENY":
        return "DENIED"
    if state.get("error_code") or kind == "MANUAL" or state.get("risk_decision") == RiskDecision.MANUAL.value:
        return "MANUAL"
    if kind == "NEED_INFO":
        return "NEED_INFO"
    plan = state.get("action_plan") or {}
    if plan.get("needs_more_info") and not state.get("command_ids"):
        return "NEED_INFO"
    if state.get("command_ids"):
        return "ACCEPTED"
    if kind == "ALLOW":
        return "SUCCEEDED"
    return "UNKNOWN"


def _locale(state) -> str:
    loc = state.get("locale") or (state.get("context_meta") or {}).get("locale")
    return str(loc) if loc else DEFAULT_LOCALE


def _fallback_text(rtype: str, locale: str) -> str:
    table = I18N_FALLBACK.get(locale)
    if table and rtype in table:
        return table[rtype]
    return FALLBACK_TEXTS.get(rtype, FALLBACK_TEXTS["UNKNOWN"])


def _amount_text(state) -> str | None:
    """已核验的退款金额文案（来自报价，不臆造）。无报价返回 None。"""
    minor = state.get("quote_amount_minor")
    if minor is None:
        return None
    currency = state.get("quote_currency") or (state.get("finance_snapshot") or {}).get("currency") or "CNY"
    return f"{currency} {int(minor) / 100:.2f}"


def _with_facts(state, rtype: str, text: str) -> str:
    """在版本化政策文案基础上注入**已核验事实**（退款金额）；不引入模板外新事实。"""
    amount = _amount_text(state)
    if not amount:
        return text
    line = {
        "ACCEPTED": f"退款金额 {amount}，预计按原路退回。",
        "PROCESSING": f"退款金额 {amount}，正在处理中。",
        "SUCCEEDED": f"退款金额 {amount}，已处理完成。",
    }.get(rtype)
    eta = state.get("settlement_eta")
    if line and eta and rtype in ("ACCEPTED", "PROCESSING"):
        line = f"{line}（预计到账：{eta}）"
    return f"{text}\n{line}" if line else text


def _compose_text(state, rtype: str) -> tuple[str, dict]:
    existing = state.get("final_response")
    if existing:
        return existing, {}
    hint_text = state.get("response_text_hint")
    if hint_text:
        return hint_text, {}
    decision = state.get("policy_decision") or {}
    if rtype in ("DENIED", "ACCEPTED", "SUCCEEDED", "ANSWER"):
        text, meta = render_decision_message(decision)
        if text:
            return _with_facts(state, rtype, text), meta
    locale = _locale(state)
    if rtype == "NEED_INFO":
        missing = ((state.get("action_plan") or {}).get("missing_information")
                   or decision.get("missing_facts") or ["订单号"])
        if locale.startswith("zh"):
            return "为了帮您处理，请补充：" + "、".join(str(x) for x in list(missing)[:3]), {}
        return "To proceed, please provide: " + ", ".join(str(x) for x in list(missing)[:3]), {}
    return _with_facts(state, rtype, _fallback_text(rtype, locale)), {}


async def _remember_turn(state, deps: Deps, rtype: str, text: str) -> None:
    """本轮结果写入记忆系统：回复回写消息历史 + 终态归档情景记忆 + 画像更新（非权威）。

    记忆只用于理解与措辞，绝不进入政策/风控/资金裁决；任何失败都不得影响回复出口。
    """
    memory = getattr(deps, "memory", None)
    if memory is None:
        return
    # (1) 回复写回消息历史：多轮记忆的来源（幂等键绑定本轮 run）
    try:
        await deps.persistence.append_message(
            tenant_id=state["tenant_id"], ticket_id=state["ticket_id"],
            thread_id=str(state.get("thread_id") or state.get("run_id") or "thread"),
            role="assistant", content=text, content_hash=payload_hash(text),
            idempotency_key=f"{state.get('run_id')}:assistant", client_message_id=None)
    except Exception:  # noqa: BLE001 - 记忆写入失败不得影响回复
        pass
    # (2) 情景记忆：仅终态工单归档（类型判定在 MemoryService 内）
    try:
        await memory.remember_episode(tenant_id=state["tenant_id"], user_id=state["user_id"],
                                     ticket_id=state["ticket_id"], state=state,
                                     response_type=rtype, response_text=text)
    except Exception:  # noqa: BLE001
        pass
    # (3) 语义记忆：画像更新（非权威）
    try:
        await memory.update_profile(tenant_id=state["tenant_id"], user_id=state["user_id"],
                                   patch=memory.profile_patch_from_state(state))
    except Exception:  # noqa: BLE001
        pass


async def response_composer(state, deps: Deps) -> dict:
    """唯一回复出口：按 response_type 分发；审计在此统一落库（关键资金事件另在节点内落库）。"""
    rtype = classify_response(state)
    public_status = state.get("public_status")
    if public_status == "PROCESSING":
        rtype = "PROCESSING"
    text, meta = _compose_text(state, rtype)
    if public_status == "PROCESSING":
        text = (state.get("response_text_hint")
                or "您的申请已受理，正在处理中，可在工单内查看进度。")
        meta = {"source": "v4_public_status"}
    status = _STATUS_BY_TYPE.get(rtype, "COMPLETED")
    await deps.persistence.append_audit(
        tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], event="RESPONDED",
        payload={"response_type": rtype, "response_status": status, "text": text,
                 "policy_decision_id": ((state.get("policy_decision") or {}).get("policy_decision_id")
                                        or (state.get("validated_decision") or {}).get("decision_id")),
                 "policy_template": meta, "plan_id": state.get("plan_id"),
                 "command_ids": state.get("command_ids") or []})
    channels = state.get("channels") or ([state.get("channel")] if state.get("channel") else list(DEFAULT_CHANNELS))
    for ch in channels:
        await deps.persistence.append_audit(
            tenant_id=state["tenant_id"], ticket_id=state["ticket_id"], event="MESSAGE_DISPATCHED",
            payload={"channel": str(ch), "response_type": rtype, "locale": _locale(state),
                     "text_len": len(text)})
    await _remember_turn(state, deps, rtype, text)
    out = {"final_response": text, "response_status": status, "response_type": rtype}
    if state.get("manual_queue"):
        out["manual_queue"] = state["manual_queue"]
    return out
