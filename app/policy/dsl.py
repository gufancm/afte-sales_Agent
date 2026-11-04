"""政策规则 DSL（LLD §4.3/§4.8）。纯确定性求值，不调用 LLM。

v2 变更（Policy Engine 120 条测评前置修正）：
- 修复 ``not_exists`` 的未定义变量；
- 规则新增 ``stage``（裁决阶段），把“谁优先”从单一 priority 数值改为显式阶段语义；
- 规则新增 ``cap_type``，区分“政策业务上限（拒绝）”与“自动执行阈值（转人工）”；
- 新增 ``canonical()``，让规则集指纹由**规范化规则内容**计算，而不是只对版本号计算。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

Op = Literal["eq", "neq", "in", "nin", "gte", "lte", "gt", "lt", "between", "regex",
             "exists", "not_exists"]

# --------------------------------------------------------------------------- #
# 裁决阶段（stage）：数值小的先裁决，同一阶段内再比 priority / specificity。
#
# 顺序对应业务评审结论：
#   事实缺失/无效 -> 强制限制与特殊例外 -> 明确拒绝 -> 一般允许 -> 兜底人工
#
# 关键不变量：**DENY 永远先于 ALLOW**，不存在“允许与拒绝同时命中时默认放行”的路径。
# 需要人工签核才能放行的场景（如未知品类、特殊品类取消）放在 STAGE_MANDATORY_LIMIT，
# 因此 MANUAL 也能压过一般允许；纯兜底人工则放在 STAGE_FALLBACK_MANUAL，允许明确规则先行。
# --------------------------------------------------------------------------- #
STAGE_FACT_GUARD = 0          # 事实缺失/无效守卫（NEED_INFO / 权威事实缺失 MANUAL）
STAGE_MANDATORY_LIMIT = 10    # 强制限制、特殊例外、必须人工签核
STAGE_EXPLICIT_DENY = 20      # 明确拒绝（时效/状态不满足）
STAGE_GENERAL_ALLOW = 30      # 一般允许
STAGE_FALLBACK_MANUAL = 40    # 兜底人工（有明确允许规则时让位）

DEFAULT_STAGE_BY_DECISION: dict[str, int] = {
    "NEED_INFO": STAGE_FACT_GUARD,
    "DENY": STAGE_EXPLICIT_DENY,
    "MANUAL": STAGE_FALLBACK_MANUAL,
    "ALLOW": STAGE_GENERAL_ALLOW,
}

CapType = Literal["POLICY_LIMIT", "AUTO_EXEC_THRESHOLD"]


@dataclass(frozen=True)
class Condition:
    fact: str            # 例如 "claim.reason_code" / "order.category"
    op: Op
    value: Any = None

    def evaluate(self, facts: dict[str, Any]) -> bool:
        cur = _resolve(facts, self.fact)
        op, val = self.op, self.value
        if op == "not_exists":
            return cur is None
        if op == "exists":
            return cur is not None
        if cur is None:
            return False
        if op == "eq":
            return cur == val
        if op == "neq":
            return cur != val
        if op == "in":
            return cur in val
        if op == "nin":
            return cur not in val
        if op == "gte":
            return cur >= val
        if op == "lte":
            return cur <= val
        if op == "gt":
            return cur > val
        if op == "lt":
            return cur < val
        if op == "between":
            lo, hi = val
            return lo <= cur <= hi
        if op == "regex":
            return bool(re.search(val, str(cur)))
        return False

    def describe(self, facts: dict[str, Any]) -> dict[str, Any]:
        """条件级判定留痕（用于决策 trace）：fact / op / expected / actual / ok / missing。"""
        actual = _resolve(facts, self.fact)
        return {"fact": self.fact, "op": self.op, "expected": self.value,
                "actual": actual, "ok": self.evaluate(facts), "missing": actual is None}


@dataclass(frozen=True)
class Rule:
    rule_id: str
    version: str
    priority: int
    when: list[Condition]
    decision: Literal["ALLOW", "DENY", "MANUAL", "NEED_INFO"]
    reason_code: str
    required_clause_ids: list[str] = field(default_factory=list)
    amount_cap_minor: int | None = None
    scope: dict[str, Any] = field(default_factory=dict)
    effective_from: str | None = None   # ISO8601
    effective_to: str | None = None
    status: Literal["PUBLISHED", "SUPERSEDED", "RETIRED"] = "PUBLISHED"
    tenant_scope: str = "tenant_demo"
    stage: int | None = None            # None -> 按 decision 取默认阶段
    cap_type: CapType = "AUTO_EXEC_THRESHOLD"
    # 该规则明确授权的**具体业务动作**（动作级授权，不等于“政策允许”）。
    # 例：7 天无理由只授权 REQUEST_RETURN_REFUND，不得据此直接执行 REQUEST_REFUND_ONLY。
    # 空元组表示“政策成立，但没有任何动作被授权自动化执行”（只能转人工）。
    allowed_actions: tuple[str, ...] = ()

    @property
    def specificity(self) -> int:
        return len(self.when)

    @property
    def resolved_stage(self) -> int:
        if self.stage is not None:
            return int(self.stage)
        return DEFAULT_STAGE_BY_DECISION.get(self.decision, STAGE_FALLBACK_MANUAL)

    def evaluate(self, facts: dict[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
        """返回 (是否命中, 每个条件的留痕)。用于生成可解释的决策 trace。"""
        conds = [c.describe(facts) for c in self.when]
        return all(c["ok"] for c in conds), conds

    def matches(self, facts: dict[str, Any]) -> bool:
        return all(c.evaluate(facts) for c in self.when)

    def canonical(self) -> dict[str, Any]:
        """用于规则集指纹的规范化表示（只含影响裁决的字段）。"""
        return {
            "rule_id": self.rule_id,
            "version": self.version,
            "priority": self.priority,
            "stage": self.resolved_stage,
            "when": [[c.fact, c.op, c.value] for c in self.when],
            "decision": self.decision,
            "reason_code": self.reason_code,
            "required_clause_ids": list(self.required_clause_ids),
            "amount_cap_minor": self.amount_cap_minor,
            "cap_type": self.cap_type,
            "allowed_actions": list(self.allowed_actions),
            "scope": self.scope,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "status": self.status,
        }


def _resolve(facts: dict[str, Any], path: str) -> Any:
    cur: Any = facts
    for seg in path.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            return None
    return cur
