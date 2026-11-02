# -*- coding: utf-8 -*-
"""确定性政策引擎（LLD §4）。输入可信事实与意图，输出 PolicyDecision；不调用 LLM。

v2 修正（Policy Engine 120 条测评前置条件）：
1. 时间：只使用 ``claim.claim_at`` 与 ``order.delivered_at`` 两个原始事实派生的
   ``day_index_signed`` / ``day_index_next``，绝不在裁决时现取系统时间；
2. 冲突：按 **裁决阶段 stage** 决策，DENY 永远先于 ALLOW；阶段内再比 priority/specificity；
   仍有同阶段冲突且结论不同 -> MANUAL(POLICY_CONFLICT)，绝不默认放行；
3. 缺事实：某条规则除“事实缺失”外全部条件成立时，判定为 *blocked*；
   blocked 规则按同一阶段顺序参与竞争，赢了就返回 NEED_INFO / MANUAL，而不是让通用
   ALLOW 规则兜底；
4. 金额：``cap_type`` 区分政策业务上限（超限 DENY）与自动执行阈值（超限 MANUAL）；
   金额缺失时不阻断“业务资格”，但打 ``execution_blocked``，执行前必须由 Finance 报价补齐；
5. 指纹：``ruleset_hash`` 由**规范化规则内容**计算，并同时记录 ruleset_version /
   facts_snapshot_id / evaluated_at，保证“同一订单为什么在不同日期裁决不同”可复现。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from app.domain.enums import INTENT_ALLOWED_ACTIONS, Intent
from app.domain.schemas import AmountCap, PolicyDecision
from app.policy.dsl import Rule
from app.policy.facts import enrich_facts

ENGINE_VERSION = "policy-engine/2.0"
RULESET_SCHEMA_VERSION = "pe-ruleset/2"

# 派生事实 -> 原始输入事实（缺哪个就报哪个，避免把派生字段当成用户可填项）
FACT_INPUTS: dict[str, list[str]] = {
    "claim.day_index_signed": ["order.delivered_at", "claim.claim_at"],
    "claim.day_index_next": ["order.delivered_at", "claim.claim_at"],
    "claim.days_since_delivery": ["order.delivered_at", "claim.claim_at"],
}
# 用户可以补充的事实（走 NEED_INFO）；其余视为权威事实（缺失只能转人工）
# 用户可以补充的事实（走 NEED_INFO）；其余视为权威事实（缺失只能转人工）。
# 注意：claim_at 是**系统记录的申请时点**，不是用户可随意补填的字段；
# 它缺失说明受理链路没有留下权威时间，必须 MANUAL(AUTHORITATIVE_FACT_UNAVAILABLE)，
# 否则用户补一个有利日期就能改变时效裁决。
USER_SUPPLIABLE_FACTS = {
    "claim.reason_code", "claim.quantity",
    "finance.requested_amount_minor", "order.order_id",
}


class RuleRepository(Protocol):
    def load_applicable(self, *, tenant_id: str, as_of: str, scope: dict) -> list[Rule]: ...


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class PolicyEngine:
    repo: RuleRepository

    # ------------------------------------------------------------------ #
    def decide(
        self,
        *,
        tenant_id: str,
        intent: Intent,
        facts: dict,
        as_of: str | None = None,
        scope: dict | None = None,
    ) -> PolicyDecision:
        as_of = as_of or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        scope = scope or {}
        # 事实装配统一在引擎入口完成：品类归一化 + 时间派生（节点/评测/回放走同一口径）
        facts = enrich_facts(facts)
        # 意图是系统输入的裁决维度（不可由调用方伪造/遗漏）：缺失会让“含 intent 条件”的规则
        # 被误判为 blocked，从而以更高 stage 抢占允许规则。
        facts["intent"] = str(intent)
        rules = self.repo.load_applicable(tenant_id=tenant_id, as_of=as_of, scope=scope)

        ruleset_version = "+".join(sorted({r.version for r in rules})) or "EMPTY"
        ruleset_hash = _sha256(_canonical_json(
            [r.canonical() for r in sorted(rules, key=lambda x: x.rule_id)]))
        facts_snapshot_id = _sha256(_canonical_json(facts))
        claim_at = (facts.get("claim") or {}).get("claim_at")
        time_basis = "claim_at" if claim_at else "as_of"

        trace: dict = {
            "engine": ENGINE_VERSION,
            "ruleset_schema": RULESET_SCHEMA_VERSION,
            "ruleset_version": ruleset_version,
            "ruleset_hash": ruleset_hash,
            "facts_snapshot_id": facts_snapshot_id,
            "as_of": as_of,
            "evaluated_at": as_of,
            "time_basis": time_basis,
            "scope": scope,
            "intent": str(intent),
            "loaded_rule_count": len(rules),
            "loaded_rule_ids": [r.rule_id for r in rules],
            "stage_order": {
                "0": "FACT_GUARD", "10": "MANDATORY_LIMIT", "20": "EXPLICIT_DENY",
                "30": "GENERAL_ALLOW", "40": "FALLBACK_MANUAL",
            },
            "evaluated": [],
            "matched_rule_ids": [],
            "blocked_rules": [],
            "candidates_ranked": [],
            "top_priority": None,
            "conflict": None,
            "missing_facts": [],
            "winner": None,
            "outcome": None,
        }

        def finish(decision: PolicyDecision) -> PolicyDecision:
            decision.trace = trace
            decision.ruleset_version = ruleset_version
            decision.ruleset_hash = ruleset_hash
            decision.facts_snapshot_id = facts_snapshot_id
            decision.evaluated_at = as_of
            trace["outcome"] = decision.reason_code
            return decision

        if not rules:
            return finish(self._no_policy(as_of, time_basis))

        matched: list[Rule] = []
        blocked: list[tuple[Rule, list[str], list[str]]] = []  # (rule, 缺失条件字段, 缺失原始输入)
        for r in rules:
            ok, conds = r.evaluate(facts)
            missing_conds = [c["fact"] for c in conds if c["missing"]
                             and c["op"] not in ("not_exists", "exists")]
            is_blocked = (not ok) and bool(missing_conds) and all(
                c["ok"] for c in conds if not (c["missing"] and c["op"] not in ("not_exists", "exists")))
            trace["evaluated"].append({
                "rule_id": r.rule_id, "version": r.version, "priority": r.priority,
                "stage": r.resolved_stage, "specificity": r.specificity,
                "decision": r.decision, "reason_code": r.reason_code,
                "matched": ok, "blocked": is_blocked,
                "conditions": conds,
                "failed_conditions": [c["fact"] for c in conds if not c["ok"]],
                "missing_conditions": missing_conds,
            })
            if ok:
                matched.append(r)
            elif is_blocked:
                inputs = self._missing_inputs(missing_conds, facts)
                blocked.append((r, missing_conds, inputs))
                trace["blocked_rules"].append(
                    {"rule_id": r.rule_id, "stage": r.resolved_stage, "priority": r.priority,
                     "decision": r.decision, "missing_conditions": missing_conds,
                     "missing_inputs": inputs})

        trace["matched_rule_ids"] = [r.rule_id for r in matched]

        # 动作作用域过滤：只授权**其他动作**的 ALLOW 规则，即便补齐事实后命中，
        # 结论也只能是"请求动作未被授权"，不能据此向用户追问与该动作无关的事实。
        # 例：用户要"补发"，只授权"仅退款"的迟到补偿规则缺 reason_code 时，
        # 不能拿它去问用户退款原因（NEED_INFO），应落到 NO_APPLICABLE_POLICY。
        # DENY / MANUAL 是限制性规则，与请求动作无关也必须保留参与竞争。
        # 但 MANUAL 分支若因缺用户可补事实而未命中，补齐后结论仍只是"转人工"，
        # 追问不能解锁任何自动执行动作，只会把一次转人工拖成多轮澄清 —— 直接按无适用政策处理。
        intent_actions = {a.value for a in INTENT_ALLOWED_ACTIONS.get(intent, frozenset())}
        scoped_blocked: list[tuple[Rule, list[str], list[str]]] = []
        for item in blocked:
            rule = item[0]
            if rule.decision == "ALLOW" and rule.allowed_actions and not (
                    set(rule.allowed_actions) & intent_actions):
                trace.setdefault("action_irrelevant_blocked", []).append(
                    {"rule_id": rule.rule_id, "missing_conditions": item[1],
                     "allowed_actions": list(rule.allowed_actions),
                     "intent_actions": sorted(intent_actions)})
                continue
            if rule.decision == "MANUAL":
                trace.setdefault("manual_branch_blocked", []).append(
                    {"rule_id": rule.rule_id, "missing_conditions": item[1],
                     "note": "补齐事实后仍是 MANUAL，不构成 NEED_INFO 理由"})
                continue
            scoped_blocked.append(item)
        blocked = scoped_blocked

        # 候选 = 已命中规则 + 仅因缺事实而未命中的规则，统一按 (stage, -priority, -specificity, id)
        candidates: list[tuple[Rule, bool]] = [(r, False) for r in matched] + [(r, True) for r, _, _ in blocked]
        if not candidates:
            # 走到这里说明**每条规则都有一个非缺失条件不成立**，此时"缺什么事实"与本次裁决无关：
            # 即便把该事实补齐，那些规则依然不会命中（blocked 判定已覆盖"只差事实"的情形）。
            # 因此只把无关缺失写入 trace 供排查，裁决必须是 NO_APPLICABLE_POLICY，
            # 不能对外声称"缺签收时间"之类并不影响结论的原因。
            trace["irrelevant_missing_facts"] = self._legacy_missing_facts(rules, facts)
            return finish(self._no_policy(as_of, time_basis))

        ranked = sorted(candidates, key=lambda item: (item[0].resolved_stage, -item[0].priority,
                                                     -item[0].specificity, item[0].rule_id))
        trace["candidates_ranked"] = [
            {"rule_id": r.rule_id, "stage": r.resolved_stage, "priority": r.priority,
             "specificity": r.specificity, "decision": r.decision, "blocked_by_missing_fact": b}
            for r, b in ranked[:8]]

        best_stage = ranked[0][0].resolved_stage
        top = [item for item in ranked if item[0].resolved_stage == best_stage]
        best_priority = max(item[0].priority for item in top)
        trace["top_priority"] = best_priority
        top = [item for item in top if item[0].priority == best_priority]
        if len(top) > 1:
            best_spec = max(item[0].specificity for item in top)
            narrowed = [item for item in top if item[0].specificity == best_spec]
            if len(narrowed) > 1 and len({item[0].decision for item in narrowed}) > 1:
                trace["conflict"] = {"type": "stage_priority_specificity_tie",
                                     "stage": best_stage, "priority": best_priority,
                                     "specificity": best_spec,
                                     "rule_ids": sorted(item[0].rule_id for item in narrowed)}
                return finish(PolicyDecision(decision="MANUAL", reason_code="POLICY_CONFLICT",
                                             reason_codes=["POLICY_CONFLICT"],
                                             rule_ids=sorted(item[0].rule_id for item in narrowed),
                                             as_of=as_of, time_basis=time_basis, scope=scope))
            top = narrowed

        winner, winner_blocked = sorted(top, key=lambda item: item[0].rule_id)[0]

        # 缺事实的胜出规则：区分“用户可补充”与“系统权威事实缺失”
        if winner_blocked:
            _, missing_conds, missing_inputs = next(b for b in blocked if b[0].rule_id == winner.rule_id)
            user_fixable = all(f in USER_SUPPLIABLE_FACTS for f in missing_inputs)
            trace["missing_facts"] = missing_inputs
            trace["winner"] = {"rule_id": winner.rule_id, "stage": winner.resolved_stage,
                               "decision": "NEED_INFO" if user_fixable else "MANUAL",
                               "blocked_by_missing_fact": True}
            if user_fixable:
                # NEED_INFO 是"要求澄清"，不是资格裁决：下游 route=clarify，不取证据，
                # 因此不回填条款（避免向用户引用一条尚未确定适用的政策）。
                # 但仍绑定触发澄清的规则，审计可经 rule_id 反查条款。
                return finish(PolicyDecision(decision="NEED_INFO", reason_code="MISSING_FACT",
                                             reason_codes=["MISSING_FACT", winner.reason_code],
                                             rule_ids=[winner.rule_id],
                                             missing_facts=missing_inputs, as_of=as_of,
                                             policy_version=winner.version,
                                             time_basis=time_basis, scope=scope))
            return finish(PolicyDecision(decision="MANUAL", reason_code="AUTHORITATIVE_FACT_UNAVAILABLE",
                                         reason_codes=["AUTHORITATIVE_FACT_UNAVAILABLE"],
                                         rule_ids=[winner.rule_id], missing_facts=missing_inputs,
                                         as_of=as_of, policy_version=winner.version,
                                         time_basis=time_basis, scope=scope))

        trace["winner"] = {"rule_id": winner.rule_id, "version": winner.version,
                           "stage": winner.resolved_stage, "priority": winner.priority,
                           "specificity": winner.specificity, "decision": winner.decision,
                           "reason_code": winner.reason_code}

        # 兜底不变量：任何 DENY 命中而胜出为 ALLOW 的组合都不允许（理论上不可达）
        if winner.decision == "ALLOW":
            denies = [r for r in matched if r.decision == "DENY"]
            if denies:
                deny = sorted(denies, key=lambda r: (r.resolved_stage, -r.priority, r.rule_id))[0]
                trace["conflict"] = {"type": "deny_overrides_allow",
                                     "deny_rule_ids": [r.rule_id for r in denies]}
                winner = deny
                trace["winner"] = {"rule_id": deny.rule_id, "version": deny.version,
                                   "stage": deny.resolved_stage, "decision": deny.decision,
                                   "reason_code": deny.reason_code, "forced_by": "deny_overrides_allow"}

        authorized, auth_trace = self._action_authorization(winner, intent)
        trace["action_authorization"] = auth_trace
        cap, amount_decision = self._apply_amount_cap(winner, facts, trace)
        base = dict(reason_codes=[winner.reason_code], rule_ids=[winner.rule_id],
                    required_clause_ids=list(winner.required_clause_ids), amount_cap=cap,
                    authorized_actions=authorized, action_authorization=auth_trace,
                    policy_version=winner.version, as_of=as_of, scope=scope, time_basis=time_basis)
        if amount_decision is not None:
            return finish(amount_decision)
        return finish(PolicyDecision(decision=winner.decision, reason_code=winner.reason_code, **base))

    # ------------------------------------------------------------------ #
    @staticmethod
    def _action_authorization(winner: Rule, intent: Intent) -> tuple[list[str], dict]:
        """规则动作授权 ∩ 意图动作白名单。

        - 规则 ALLOW 但 allowed_actions 为空 -> 政策成立但无可自动化动作；
        - 请求动作不在交集 -> 只能澄清/转人工，绝不能因为“政策 ALLOW”就直接执行。
        非 ALLOW 的胜出规则一律不授权任何写动作。
        """
        from app.domain.enums import INTENT_ALLOWED_ACTIONS

        policy_actions = [str(a) for a in winner.allowed_actions] if winner.decision == "ALLOW" else []
        intent_actions = sorted(a.value for a in INTENT_ALLOWED_ACTIONS.get(intent, frozenset()))
        authorized = [a for a in policy_actions if a in set(intent_actions)]
        if winner.decision != "ALLOW":
            reason = "DECISION_NOT_ALLOW"
        elif not policy_actions:
            reason = "NO_AUTOMATABLE_ACTION_FOR_POLICY"
        elif not authorized:
            reason = "REQUESTED_ACTION_NOT_AUTHORIZED"
        else:
            reason = "AUTHORIZED"
        return authorized, {"rule_id": winner.rule_id, "decision": winner.decision,
                            "policy_actions": policy_actions, "intent": intent.value,
                            "intent_actions": intent_actions, "authorized_actions": authorized,
                            "reason": reason}

    # ------------------------------------------------------------------ #
    def _apply_amount_cap(self, winner: Rule, facts: dict,
                          trace: dict) -> tuple[AmountCap | None, PolicyDecision | None]:
        """金额语义：政策业务上限 -> DENY；自动执行阈值 -> MANUAL；金额缺失 -> 阻断执行。"""
        if winner.amount_cap_minor is None:
            return None, None
        finance = facts.get("finance") or {}
        amount = finance.get("requested_amount_minor")
        amount_source = "finance.requested_amount_minor"
        if amount is None:
            amount = finance.get("amount_minor")
            amount_source = "finance.amount_minor"
        currency = (facts.get("order") or {}).get("currency") or "CNY"
        cap = AmountCap(cap_minor=int(winner.amount_cap_minor), currency=currency,
                        basis=f"rule:{winner.rule_id}", cap_type=winner.cap_type)
        if amount is None:
            trace["amount_check"] = {"status": "UNKNOWN", "cap_minor": cap.cap_minor,
                                     "cap_type": winner.cap_type,
                                     "note": "金额缺失：保留业务资格，但阻断执行，需 Finance 报价补齐"}
            return cap, PolicyDecision(
                decision=winner.decision, reason_code=winner.reason_code,
                reason_codes=[winner.reason_code], rule_ids=[winner.rule_id],
                required_clause_ids=list(winner.required_clause_ids), amount_cap=cap,
                policy_version=winner.version, time_basis=trace.get("time_basis"),
                as_of=trace.get("as_of"), scope=trace.get("scope") or {},
                amount_check={"status": "UNKNOWN", "cap_minor": cap.cap_minor},
                execution_blocked=True, requires_manual_approval=True)
        amount_int = int(amount)
        gained = amount_int > cap.cap_minor
        trace["amount_check"] = {"status": "EXCEEDED" if gained else "WITHIN",
                                 "amount_minor": amount_int, "amount_source": amount_source,
                                 "cap_minor": cap.cap_minor, "cap_type": winner.cap_type,
                                 "currency": currency}
        if not gained:
            return cap, None
        if cap.cap_type == "POLICY_LIMIT":
            return cap, PolicyDecision(
                decision="DENY", reason_code="AMOUNT_EXCEEDS_POLICY_LIMIT",
                reason_codes=["AMOUNT_EXCEEDS_POLICY_LIMIT", winner.reason_code],
                rule_ids=[winner.rule_id], required_clause_ids=list(winner.required_clause_ids),
                amount_cap=cap, policy_version=winner.version, time_basis=trace.get("time_basis"),
                as_of=trace.get("as_of"), scope=trace.get("scope") or {},
                amount_check={"status": "EXCEEDED", "amount_minor": amount_int,
                              "cap_minor": cap.cap_minor, "cap_type": cap.cap_type})
        # AUTO_EXEC_THRESHOLD：有资格但必须人工审批，不能据此推断“没有退款资格”
        return cap, PolicyDecision(
            decision="MANUAL", reason_code="AMOUNT_EXCEEDS_AUTO_THRESHOLD",
            reason_codes=["AMOUNT_EXCEEDS_AUTO_THRESHOLD", winner.reason_code],
            rule_ids=[winner.rule_id], required_clause_ids=list(winner.required_clause_ids),
            amount_cap=cap, policy_version=winner.version, time_basis=trace.get("time_basis"),
            as_of=trace.get("as_of"), scope=trace.get("scope") or {},
            amount_check={"status": "EXCEEDED", "amount_minor": amount_int,
                          "cap_minor": cap.cap_minor, "cap_type": cap.cap_type},
            requires_manual_approval=True)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _missing_inputs(missing_conds: list[str], facts: dict) -> list[str]:
        out: set[str] = set()
        from app.policy.dsl import _resolve
        for fact in missing_conds:
            for src in FACT_INPUTS.get(fact, [fact]):
                if _resolve(facts, src) is None:
                    out.add(src)
        if not out:
            # 原始输入都在、但派生值为空：说明数据不可解析/异常，按权威事实处理（转人工）
            return sorted(missing_conds)
        return sorted(out)

    @staticmethod
    def _legacy_missing_facts(rules: list[Rule], facts: dict) -> list[str]:
        """诊断用：列出“所有规则需要但事实里没有”的字段。

        注意：这些字段**不参与裁决**。无候选说明每条规则都有非缺失条件已失败，
        补齐这些事实也不会改变结论，所以不能据此返回 NEED_INFO/MANUAL。"""
        needed: set[str] = set()
        for r in rules:
            for c in r.when:
                if c.op in ("exists", "not_exists"):
                    continue
                needed.add(c.fact)
        from app.policy.dsl import _resolve
        raw = sorted({src for f in needed
                      for src in FACT_INPUTS.get(f, [f])
                      if _resolve(facts, src) is None})
        return raw

    @staticmethod
    def _no_policy(as_of: str, time_basis: str = "as_of") -> PolicyDecision:
        return PolicyDecision(decision="MANUAL", reason_code="NO_APPLICABLE_POLICY",
                              reason_codes=["NO_APPLICABLE_POLICY"], as_of=as_of,
                              time_basis=time_basis)
