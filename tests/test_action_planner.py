# -*- coding: utf-8 -*-
"""ActionPlanner 落地回归：确定性快路径 / 计划审查 / 有界重规划 / 依赖 DAG / plan_id 前移。

覆盖本轮"除评测集外全部落地"的 P0/P1/P2：
  - 快路径：意图明确 + 单动作 + 政策 ALLOW + 证据 OK + 无歧义 SKU 时跳过 LLM
  - 计划审查：确定性 critic 分级（FATAL -> manual，REPLAN -> 有界重规划，CLARIFY -> 澄清）
  - 有界重规划：planner 自行修正（补回事实里已有的槽位）后通过，不无限循环
  - 依赖 DAG：command_submitter 按 depends_on 拓扑序提交
  - 幂等键：按动作注册表模板生成
  - plan_id 前移：planner 落 DRAFT，action_preparation 原地冻结为同一 plan_id
"""
from __future__ import annotations

import pytest

pytest.importorskip("langgraph")

from app.bootstrap import build_deps  # noqa: E402
from app.config import Settings  # noqa: E402
from app.domain.action_registry import external_write_actions  # noqa: E402
from app.domain.enums import Action, EvidenceStatus, Intent  # noqa: E402
from app.domain.schemas import ActionPlan, ActionProposal  # noqa: E402
from app.graph import nodes  # noqa: E402
from app.graph.plan_critic import deterministic_critique  # noqa: E402

ORDER_SNAPSHOT = {"order_id": "o_1001", "status": "PAID_UNSHIPPED", "version": "v1", "category": "FRESH",
                  "currency": "CNY", "lines": [{"order_line_id": "l1", "sku_id": "sku_1", "quantity": 1}]}
FINANCE_SNAPSHOT = {"order_id": "o_1001", "refundable_balance_minor": 20000,
                    "reserved_amount_minor": 0, "currency": "CNY"}


def _deps():
    return build_deps(Settings(_env_file=None, app_env="test", database_url="memory://", llm_model=""))


def _state(**over):
    state = {"tenant_id": "t1", "user_id": "u_demo", "ticket_id": "tk1", "thread_id": "th1",
             "run_id": "r1", "trace_id": "tr1", "messages": [{"role": "user", "content": "我要退货退款"}],
             "slots": {"order_id": "o_1001", "quantity": 1},
             "order_snapshot": dict(ORDER_SNAPSHOT), "finance_snapshot": dict(FINANCE_SNAPSHOT),
             "policy_decision": {"decision": "ALLOW",
                                 "authorized_actions": ["REQUEST_REFUND_ONLY", "REQUEST_RETURN_REFUND",
                                                      "CREATE_RESHIPMENT", "INTERCEPT_PARCEL"]},
             "evidence_status": EvidenceStatus.OK.value}
    state.update(over)
    return state


class _ReplanStub:
    """首轮漏填 reason_code（事实里有）触发 REPLAN，带反馈时补回。

    注：写动作的 order_id 由 schema 强制，故用 reason_code 作为可重规划槽位。
    """

    def __init__(self):
        self.calls = 0

    async def structured(self, *, messages, schema_model, prompt_version, temperature=0.0):
        self.calls += 1
        with_feedback = any("驳回" in (m.get("content") or "")
                            for m in messages if m.get("role") == "user")
        plan = ActionPlan(order_id="o_1001", actions=[ActionProposal(
            action=Action.REQUEST_RETURN_REFUND, order_id="o_1001", sku_ids=["sku_1"],
            reason_code="DAMAGED" if with_feedback else "")], rationale="stub")
        return plan, {"request_hash": "x", "response_hash": "y", "usage": {}}


# --------------------------------------------------------------------------- #
# 1) 快路径：命中即直出，不调用 LLM
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_fastpath_builds_plan_without_llm():
    deps = _deps()  # llm_model="" -> deps.llm is None，若走 LLM 会得到 LLM_DISABLED
    state = _state(intent_result={"intent": Intent.REFUND_ONLY.value})
    out = await nodes.action_planner(state, deps)

    assert out["plan_source"] == "fastpath"
    assert out["action_plan"]["source"] == "fastpath"
    acts = out["action_plan"]["actions"]
    assert [a["action"] for a in acts] == ["REQUEST_REFUND_ONLY"]
    assert acts[0]["sku_ids"] == ["sku_1"]  # 退款必须绑定无歧义 SKU
    assert out["plan_critic"]["ok"] is True
    assert out["plan_id"] and out["plan_id"] in deps.persistence.plans
    assert deps.persistence.plans[out["plan_id"]]["status"] == "DRAFT"
    events = [a["event"] for a in deps.persistence.audits]
    assert "PLAN_FASTPATH" in events and "PLAN_PROPOSED" in events
    assert nodes.route_after_plan(out) == "prepare"


@pytest.mark.asyncio
async def test_fastpath_skips_when_sku_ambiguous():
    # 订单两行且用户未指明 SKU -> 不可无歧义定位 -> 快路径放弃
    order = dict(ORDER_SNAPSHOT)
    order["lines"] = [{"order_line_id": "l1", "sku_id": "sku_1", "quantity": 1},
                      {"order_line_id": "l2", "sku_id": "sku_2", "quantity": 1}]
    deps = _deps()
    state = _state(intent_result={"intent": Intent.REFUND_ONLY.value}, order_snapshot=order)
    out = await nodes.action_planner(state, deps)
    assert out["plan_source"] == "none"  # 无 LLM -> 未规划


@pytest.mark.asyncio
async def test_fastpath_off_switch():
    deps = _deps()
    deps.settings.planner_fastpath_enabled = False
    out = await nodes.action_planner(_state(intent_result={"intent": Intent.REFUND_ONLY.value}), deps)
    assert out["plan_source"] == "none"


# --------------------------------------------------------------------------- #
# 2) 确定性计划审查
# --------------------------------------------------------------------------- #
def test_critique_flags_policy_not_allow_and_bad_sku():
    plan = ActionPlan(order_id="o_1001", actions=[
        ActionProposal(action=Action.REQUEST_REFUND_ONLY, order_id="o_1001", sku_ids=["sku_zzz"])])
    c = deterministic_critique(_state(policy_decision={"decision": "DENY"}), plan, Intent.REFUND_ONLY)
    codes = {f.code for f in c.findings}
    assert {"POLICY_NOT_ALLOW", "SKU_NOT_IN_ORDER"} <= codes
    assert c.fatal is True


def test_schema_rejects_two_refund_actions():
    # 退款类动作上限由 schema 强制（critic 的冲突检查为纵深防御）
    with pytest.raises(Exception):
        ActionPlan(order_id="o_1001", actions=[
            ActionProposal(action=Action.REQUEST_REFUND_ONLY, order_id="o_1001", sku_ids=["sku_1"]),
            ActionProposal(action=Action.REQUEST_RETURN_REFUND, order_id="o_1001", sku_ids=["sku_1"])])


def test_critique_flags_duplicate_action():
    plan = ActionPlan(order_id="o_1001", actions=[
        ActionProposal(action=Action.CREATE_RESHIPMENT, order_id="o_1001", sku_ids=["sku_1"]),
        ActionProposal(action=Action.CREATE_RESHIPMENT, order_id="o_1001", sku_ids=["sku_1"])])
    c = deterministic_critique(_state(), plan, Intent.RESHIPMENT)
    assert "DUPLICATE_ACTION" in {f.code for f in c.findings} and c.fatal is True


def test_critique_omitted_slot_with_facts_is_replan():
    # 事实里已有 SKU（单行订单），但计划没填 -> planner 可自行补回（REPLAN，而非澄清）
    plan = ActionPlan(order_id="o_1001", actions=[
        ActionProposal(action=Action.CREATE_RESHIPMENT, order_id="o_1001", sku_ids=[])])
    c = deterministic_critique(_state(), plan, Intent.RESHIPMENT)
    assert c.needs_replan is True and c.fatal is False and c.needs_clarify is False


def test_critique_missing_slot_without_facts_is_clarify():
    # 多行订单且用户未指明 SKU -> 事实里没有 -> 必须澄清
    order = dict(ORDER_SNAPSHOT)
    order["lines"] = [{"order_line_id": "l1", "sku_id": "sku_1", "quantity": 1},
                      {"order_line_id": "l2", "sku_id": "sku_2", "quantity": 1}]
    plan = ActionPlan(order_id="o_1001", actions=[
        ActionProposal(action=Action.CREATE_RESHIPMENT, order_id="o_1001", sku_ids=[])])
    c = deterministic_critique(_state(order_snapshot=order), plan, Intent.RESHIPMENT)
    assert c.needs_clarify is True and c.fatal is False


# --------------------------------------------------------------------------- #
# 3) 有界重规划：planner 自行补回事实里已有的槽位
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_replan_recovers_slot_that_facts_already_provide():
    deps = _deps()
    deps.settings.planner_fastpath_enabled = False  # 强制走 LLM，验证重规划闭环
    stub = _ReplanStub()
    deps.llm = stub  # type: ignore[assignment]
    # 事实里已有 order_id / reason_code，但首轮 LLM 漏填 order_id -> REPLAN
    state = _state(intent_result={"intent": Intent.RETURN_REFUND.value},
                   slots={"order_id": "o_1001", "reason_code": "DAMAGED", "quantity": 1})
    first = await nodes.action_planner(state, deps)
    assert first["plan_critic"]["needs_replan"] is True
    assert first["replan_feedback"]  # 需要重规划
    assert nodes.route_after_plan(first) == "replan"

    second = await nodes.action_planner({**state, **first}, deps)
    assert second["action_plan"]["actions"][0]["reason_code"] == "DAMAGED"
    assert second["replan_feedback"] == []
    assert stub.calls == 2
    assert nodes.route_after_plan(second) == "prepare"


@pytest.mark.asyncio
async def test_replan_is_bounded():
    """达到上限后不再无限回 planner。"""
    deps = _deps()
    deps.settings.planner_fastpath_enabled = False
    deps.settings.planner_max_replans = 1
    deps.llm = _ReplanStub()  # type: ignore[assignment]

    class _AlwaysBad:
        async def structured(self, *, messages, schema_model, prompt_version, temperature=0.0):
            return (ActionPlan(order_id="o_1001", actions=[ActionProposal(
                action=Action.REQUEST_RETURN_REFUND, order_id="o_1001", sku_ids=["sku_1"],
                reason_code="")]), {"usage": {}})

    deps.llm = _AlwaysBad()  # type: ignore[assignment]
    state = _state(intent_result={"intent": Intent.RETURN_REFUND.value},
                   slots={"order_id": "o_1001", "reason_code": "DAMAGED", "quantity": 1})
    r1 = await nodes.action_planner(state, deps)
    assert nodes.route_after_plan(r1) == "replan"
    r2 = await nodes.action_planner({**state, **r1}, deps)
    assert nodes.route_after_plan(r2) != "replan"  # 上限后不再回 planner


# --------------------------------------------------------------------------- #
# 4) command_submitter：依赖 DAG 拓扑序 + 注册表幂等键
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_command_submitter_topological_order_and_idempotency_template():
    deps = _deps()
    plan = ActionPlan(order_id="o_1001", actions=[
        ActionProposal(action=Action.INTERCEPT_PARCEL, order_id="o_1001", waybill_no="wb_1"),
        ActionProposal(action=Action.REQUEST_REFUND_ONLY, order_id="o_1001", sku_ids=["sku_1"],
                       depends_on=[0])])
    state = _state(action_plan=plan.model_dump(mode="json"), plan_id="p1",
                   logistics_snapshot={"waybill_no": "wb_1", "parcel_status": "IN_TRANSIT"},
                   risk_by_action={"INTERCEPT_PARCEL": "AUTO_APPROVE",
                                   "REQUEST_REFUND_ONLY": "AUTO_APPROVE"})
    out = await nodes.command_submitter(state, deps)
    assert len(out["command_ids"]) == 2
    keys = [c["idempotency_key"] for c in deps.persistence.commands]
    # 幂等键 = 租户 : 注册表模板 : 操作类型 : 计划指纹
    assert keys[0].startswith("t1:intercept:tk1:wb_1:INTERCEPT_PARCEL:")
    assert keys[1].startswith("t1:refund:tk1:o_1001:REQUEST_REFUND_ONLY:")
    assert keys[0] != keys[1]


def test_topo_external_writes_respects_depends_on():
    plan = {"actions": [{"action": "INTERCEPT_PARCEL", "depends_on": []},
                        {"action": "REQUEST_REFUND_ONLY", "depends_on": [0]}]}
    ordered = nodes._topo_external_writes(plan)
    assert [a["action"] for a in ordered] == ["INTERCEPT_PARCEL", "REQUEST_REFUND_ONLY"]


# --------------------------------------------------------------------------- #
# 5) plan_id 前移：planner 落 DRAFT，preparation 原地冻结为同一 id
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_action_preparation_freezes_same_plan_id():
    deps = _deps()
    plan = ActionPlan(order_id="o_1001", actions=[
        ActionProposal(action=Action.REQUEST_REFUND_ONLY, order_id="o_1001", sku_ids=["sku_1"])])
    state = _state(action_plan=plan.model_dump(mode="json"), plan_id="p_draft",
                   policy_decision={"policy_decision_id": "pd1", "decision": "ALLOW",
                                    "amount_cap": {"cap_minor": 20000},
                                    "authorized_actions": ["REQUEST_REFUND_ONLY"]})
    deps.persistence.plans["p_draft"] = {"plan_id": "p_draft", "plan_hash": "ph-draft",
                                         "plan_json": {"plan": plan.model_dump(mode="json")},
                                         "status": "DRAFT"}
    update = await nodes.action_preparation(state, deps)
    assert update["plan_id"] == "p_draft"
    assert deps.persistence.plans["p_draft"]["status"] == "VALIDATED"


# --------------------------------------------------------------------------- #
# 6) 单一元数据源：外部写动作集合来自动作注册表
# --------------------------------------------------------------------------- #
def test_external_write_set_comes_from_registry():
    assert nodes.EXTERNAL_WRITE_ACTIONS == external_write_actions()
    assert "REQUEST_REFUND_ONLY" in nodes.EXTERNAL_WRITE_ACTIONS
