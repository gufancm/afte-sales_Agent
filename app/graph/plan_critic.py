# -*- coding: utf-8 -*-
"""计划审查（Plan Critic）：确定性审查（始终执行）+ 可选 LLM 复核。

产出分级结论：
  - FATAL   : 计划与事实/意图矛盾、不可执行 -> 转人工（manual）
  - REPLAN  : 事实里已有该槽位但计划漏填，planner 可自行补回 -> 有界重规划
  - CLARIFY : 缺少必须由用户提供的信息 -> 澄清
  - WARN    : 仅记录（如金额超余额，交由风控/报价判定）

LLM 复核默认关闭（避免每次规划 +1 次 LLM 调用与不确定性）；开启后仅作"降级闸门"：
仅在 HIGH 严重且确定性审查通过时才转人工，失败不影响主流程。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.domain import action_registry
from app.domain.enums import EvidenceStatus, Intent
from app.domain.schemas import ActionPlan, StrictOutput
from app.graph.planner_fastpath import _slot_values

PROMPT_VERSION_PLAN_CRITIC = "plan-critic-v1"

FATAL = "FATAL"
REPLAN = "REPLAN"
CLARIFY = "CLARIFY"
WARN = "WARN"


class PlanCritiqueOut(StrictOutput):
    ok: bool = True
    severity: Literal["NONE", "LOW", "HIGH"] = "NONE"
    issues: list[str] = []


@dataclass
class Finding:
    code: str
    severity: str
    detail: str = ""


@dataclass
class Critique:
    findings: list[Finding] = field(default_factory=list)

    def add(self, code: str, severity: str, detail: str = "") -> None:
        self.findings.append(Finding(code, severity, detail))

    @property
    def fatal(self) -> bool:
        return any(f.severity == FATAL for f in self.findings)

    @property
    def needs_replan(self) -> bool:
        return not self.fatal and any(f.severity == REPLAN for f in self.findings)

    @property
    def needs_clarify(self) -> bool:
        return not self.fatal and any(f.severity == CLARIFY for f in self.findings)

    @property
    def ok(self) -> bool:
        return not any(f.severity in (FATAL, REPLAN, CLARIFY) for f in self.findings)

    def feedback(self) -> list[str]:
        return [f"{f.code}: {f.detail}" for f in self.findings if f.severity == REPLAN]

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "fatal": self.fatal, "needs_replan": self.needs_replan,
                "needs_clarify": self.needs_clarify,
                "findings": [{"code": f.code, "severity": f.severity, "detail": f.detail}
                             for f in self.findings]}


def _proposal_slot(a, slot: str):
    """读取提案自身携带的槽位值（判断 planner 是否真的把事实填进了计划）。"""
    if slot == "order_id":
        return a.order_id
    if slot == "sku_id":
        return a.sku_ids[0] if a.sku_ids else None
    if slot == "reason_code":
        return a.reason_code
    if slot == "waybill_no":
        return a.waybill_no
    return None


def deterministic_critique(state: dict, plan: ActionPlan, intent: Intent) -> Critique:
    c = Critique()
    values = _slot_values(state)
    order = state.get("order_snapshot") or {}
    lines = order.get("lines") or []
    by_sku: dict[str, int] = {}
    for ln in lines:
        by_sku[ln.get("sku_id")] = by_sku.get(ln.get("sku_id"), 0) + int(ln.get("quantity") or 0)

    if (state.get("policy_decision") or {}).get("decision") != "ALLOW":
        c.add("POLICY_NOT_ALLOW", FATAL, "政策裁决非 ALLOW")
    if state.get("evidence_status") not in (None, EvidenceStatus.OK.value):
        c.add("EVIDENCE_NOT_OK", FATAL, f"evidence_status={state.get('evidence_status')}")

    names = [a.action.value for a in plan.actions]
    if len(names) != len(set(names)):
        c.add("DUPLICATE_ACTION", FATAL, "计划包含重复动作")
    # 动作级政策授权（确定性，不依赖 LLM 复核）：写动作必须落在授权白名单内
    policy_authorized = set((state.get("policy_decision") or {}).get("authorized_actions") or [])
    for a in plan.actions:
        spec = action_registry.spec(a.action)
        if spec and spec.is_external_write and a.action.value not in policy_authorized:
            c.add("ACTION_NOT_POLICY_AUTHORIZED", FATAL,
                  f"{a.action.value} 未被政策授权（authorized={sorted(policy_authorized)}）")
    if sum(1 for n in names if action_registry.is_refund_family(n)) > 1:
        c.add("CONFLICTING_REFUND_ACTIONS", FATAL, "计划包含多个退款类动作")

    quantity = int((state.get("slots") or {}).get("quantity") or 1)
    for i, a in enumerate(plan.actions):
        for dep in a.depends_on:
            if dep < 0 or dep >= len(plan.actions) or dep == i:
                c.add("BAD_DEPENDENCY", FATAL, f"{a.action.value} depends_on={dep}")
        mspec = action_registry.spec(a.action)
        if mspec and mspec.is_external_write:
            for slot in mspec.required_slots:
                # 事实里有、但计划没填 -> planner 可自行补回（有界重规划）。
                if values.get(slot):
                    if not _proposal_slot(a, slot):
                        c.add(f"MISSING_SLOT_{slot.upper()}", REPLAN,
                              f"{a.action.value} 未填 {slot}（事实已提供）")
                    continue
                # 事实里也没有 -> planner 不能凭空捏造，必须由用户补充（澄清）。
                c.add(f"MISSING_SLOT_{slot.upper()}", CLARIFY, f"{a.action.value} 缺少 {slot}")
        for sku in (a.sku_ids or []):
            if sku not in by_sku:
                c.add("SKU_NOT_IN_ORDER", FATAL, f"sku {sku} 不在订单行")
            elif quantity > by_sku[sku]:
                c.add("QUANTITY_EXCEEDS_ORDER", FATAL, f"数量 {quantity} 超过订单 {by_sku[sku]}")

    if plan.order_id and order.get("order_id") and plan.order_id != order.get("order_id"):
        c.add("ORDER_ID_MISMATCH", FATAL, f"计划 {plan.order_id} != 订单 {order.get('order_id')}")

    balance = (state.get("finance_snapshot") or {}).get("refundable_balance_minor")
    for a in plan.actions:
        if a.requested_amount_minor is not None and balance is not None and a.requested_amount_minor > balance:
            c.add("REQUESTED_AMOUNT_OVER_BALANCE", WARN, f"主张 {a.requested_amount_minor} > 可退余额 {balance}")
    return c


async def llm_critique(deps, state: dict, plan: ActionPlan) -> tuple[PlanCritiqueOut | None, dict]:
    """可选 LLM 复核（默认关闭）。失败返回 (None, {})，不影响主流程。"""
    if deps.llm is None:
        return None, {}
    prompt = (deps.prompts or {}).get("plan_critic", "")
    user = (f"ActionPlan={plan.model_dump(mode='json')}\n"
            f"PolicyDecision={state.get('policy_decision')}\nIntent={state.get('intent_result')}\n"
            f"Order={state.get('order_snapshot')}\nSlots={state.get('slots')}")
    try:
        out, audit = await deps.llm.structured(
            messages=[{"role": "system", "content": prompt}, {"role": "user", "content": user}],
            schema_model=PlanCritiqueOut, prompt_version=PROMPT_VERSION_PLAN_CRITIC, temperature=0.0)
    except Exception:  # noqa: BLE001 - 复核失败不阻断
        return None, {}
    return out, audit