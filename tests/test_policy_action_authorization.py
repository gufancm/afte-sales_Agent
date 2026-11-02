# -*- coding: utf-8 -*-
"""政策动作授权回归：ALLOW ≠ 可执行；计划/命令必须落在 authorized_actions 内。"""
from __future__ import annotations

import pytest

pytest.importorskip("langgraph")

from app.bootstrap import build_deps  # noqa: E402
from app.config import Settings  # noqa: E402
from app.domain.enums import Intent  # noqa: E402
from app.domain.schemas import ActionPlan, ActionProposal  # noqa: E402
from app.domain.enums import Action  # noqa: E402
from app.graph import nodes  # noqa: E402
from app.policy.engine import PolicyEngine  # noqa: E402
from app.policy.library import load_rules  # noqa: E402
from app.policy.repository import InMemoryRuleRepository  # noqa: E402

AS_OF = "2026-10-20T10:00:00+08:00"


def _facts(category="APPAREL", reason="NO_LONGER_NEEDED", status="DELIVERED", *, day=1):
    return {"order": {"order_id": "o1", "category": category, "status": status, "currency": "CNY",
                      "version": "v1", "delivered_at": "2026-10-20T10:00:00+08:00",
                      "lines": [{"order_line_id": "l1", "sku_id": "sku_1", "quantity": 1}]},
            "logistics": {"parcel_status": "SIGNED"},
            "finance": {"requested_amount_minor": 1000},
            "claim": {"reason_code": reason, "claim_at": "2026-10-20T10:00:00+08:00"}}


def _engine():
    return PolicyEngine(repo=InMemoryRuleRepository(load_rules()))


def test_seven_day_rule_does_not_authorize_refund_only():
    d = _engine().decide(tenant_id="tenant_demo", intent=Intent.REFUND_ONLY,
                         facts=_facts(), as_of=AS_OF)
    assert d.decision == "ALLOW"                       # 业务资格成立
    assert d.action_authorization["policy_actions"] == ["REQUEST_RETURN_REFUND"]
    assert d.authorized_actions == []                  # 但“仅退款”未被授权


def test_bakery_exchange_is_not_automatable_reshipment():
    facts = _facts(category="BAKERY", reason="DATE_DISSATISFIED")
    # S4 原文只规定“换货”：退货退款没有政策依据，按人工复核处理（不得用换货窗口去拒绝退货退款）
    ret = _engine().decide(tenant_id="tenant_demo", intent=Intent.RETURN_REFUND, facts=facts, as_of=AS_OF)
    assert ret.decision == "MANUAL" and ret.reason_code == "FRESH_BAKERY_EXCHANGE"
    assert ret.authorized_actions == []
    # 换货资格成立，但“换货申请”≠“补发履约”：系统无换货动作，不得自动建补发单
    ship = _engine().decide(tenant_id="tenant_demo", intent=Intent.RESHIPMENT, facts=facts, as_of=AS_OF)
    assert ship.decision == "ALLOW"
    assert ship.authorized_actions == []
    assert ship.action_authorization["reason"] == "NO_AUTOMATABLE_ACTION_FOR_POLICY"


def test_repair_rule_has_no_automatable_action():
    # 已发货未签收：无法验货，质量争议转人工（既不能判“可维修”，也不能直接退款）
    facts = _facts(reason="DAMAGED", status="PAID_SHIPPED")
    d = _engine().decide(tenant_id="tenant_demo", intent=Intent.RETURN_REFUND, facts=facts, as_of=AS_OF)
    assert d.decision == "MANUAL" and d.reason_code == "IN_TRANSIT_QUALITY_REVIEW"
    assert d.authorized_actions == []
    # 已签收的同类问题仍走质量问题退换/维修规则
    delivered = _engine().decide(tenant_id="tenant_demo", intent=Intent.RETURN_REFUND,
                                 facts=_facts(reason="DAMAGED", status="DELIVERED"), as_of=AS_OF)
    assert delivered.decision == "ALLOW"


@pytest.mark.asyncio
async def test_policy_node_downgrades_action_mismatch_to_need_info():
    deps = build_deps(Settings(_env_file=None, app_env="test", database_url="memory://", llm_model=""))
    state = {"tenant_id": "tenant_demo", "user_id": "u_demo", "ticket_id": "tk1", "thread_id": "th1",
             "run_id": "r1", "trace_id": "tr1", "claim_at": AS_OF,
             "intent_result": {"intent": "REFUND_ONLY"},
             "slots": {"order_id": "o1", "reason_code": "NO_LONGER_NEEDED"},
             "order_snapshot": _facts()["order"],
             "logistics_snapshot": _facts()["logistics"],
             "finance_snapshot": _facts()["finance"]}
    out = await nodes.policy_engine(state, deps)
    d = out["policy_decision"]
    assert d["eligibility_decision"] == "ALLOW"
    assert d["decision"] == "NEED_INFO"
    assert d["reason_code"] == "ACTION_NOT_AUTHORIZED_BY_POLICY"
    assert "退货退款" in out["response_text_hint"]


@pytest.mark.asyncio
async def test_command_submitter_blocks_plan_action_not_authorized():
    deps = build_deps(Settings(_env_file=None, app_env="test", database_url="memory://", llm_model=""))
    plan = ActionPlan(order_id="o1", actions=[ActionProposal(
        action=Action.REQUEST_REFUND_ONLY, order_id="o1", sku_ids=["sku_1"])])
    state = {"tenant_id": "tenant_demo", "user_id": "u_demo", "ticket_id": "tk1", "thread_id": "th1",
             "run_id": "r1", "trace_id": "tr1", "plan_id": "p1",
             "policy_decision": {"decision": "ALLOW", "authorized_actions": []},
             "risk_by_action": {"REQUEST_REFUND_ONLY": "AUTO_APPROVE"},
             "action_plan": plan.model_dump(mode="json")}
    out = await nodes.command_submitter(state, deps)
    assert out["error_code"] == "POLICY_ACTION_NOT_AUTHORIZED"
    assert deps.persistence.commands == []
