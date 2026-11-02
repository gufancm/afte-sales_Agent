# -*- coding: utf-8 -*-
"""只读自助答复（混合式）回归测试：模板事实 + LLM 仅润色 + 后置校验 + 回退。

覆盖：
  - 事实模板渲染（订单/物流状态中文化）；
  - 后置校验：新增数字/承诺词/丢失关键实体 -> 拒绝，回退模板；
  - 图级：STATUS_QUERY 回真实订单/物流状态；COMPLAINT 受理；缺订单号 -> 澄清；
  - 物流不可用时降级但仍有回复。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("langgraph")

from app.bootstrap import build_deps  # noqa: E402
from app.config import Settings  # noqa: E402
from app.domain.enums import Intent, SlotSource  # noqa: E402
from app.domain.schemas import ExtractedSlots, IntentResult, SlotSources  # noqa: E402
from app.graph import nodes  # noqa: E402
from app.graph.build import build_graph  # noqa: E402
from app.graph.readonly_answer import render_fact_template, verify_grounded  # noqa: E402


def _slot_sources(order_id=None):
    return SlotSources(order_id=SlotSource.USER_STATED if order_id else None, sku_id=None,
                       order_line_id=None, waybill_no=None, reason_code=None,
                       requested_amount_minor=None, quantity=None, product_category=None)


def _intent(intent, *, order_id=None, secondary=()):
    return IntentResult(intent=intent, secondary_intents=list(secondary), confidence=0.95,
                        slots=ExtractedSlots(order_id=order_id),
                        slot_sources=_slot_sources(order_id), safety_flags=[])


class _IntentOnlyStub:
    """只实现 structured（意图识别）；无 text() -> 只读润色会回退模板。"""
    def __init__(self, result):
        self._result = result
        self.s = SimpleNamespace(model="stub")

    async def structured(self, *, messages, schema_model, prompt_version, temperature=0.0):
        return self._result, {"request_hash": "h", "response_hash": "h", "usage": {}, "model": "stub"}


class _IntentAndTextStub(_IntentOnlyStub):
    def __init__(self, result, text):
        super().__init__(result)
        self._text = text

    async def text(self, *, messages, prompt_version):
        return self._text, {"model": "stub", "request_hash": "h", "response_hash": "h", "usage": {}}


def _deps(llm):
    settings = Settings(_env_file=None, app_env="test", database_url="memory://", llm_model="stub",
                        llm_model_small="")
    deps = build_deps(settings)
    deps.llm = llm  # type: ignore[assignment]
    return deps


def _state(msg):
    return {"tenant_id": "tenant_demo", "user_id": "u_demo", "ticket_id": "tk1", "thread_id": "th1",
            "run_id": "r1", "trace_id": "tr1", "messages": [{"role": "user", "content": msg}]}


class _Order:
    order_id = "o_1002"
    status = "DELIVERED"


class _Logi:
    waybill_no = "wb_2"
    parcel_status = "DELIVERED"
    version = "t2"


class _FailLogi:
    async def get_tracking_info(self, **kw):
        raise RuntimeError("tms down")


# --------------------------------------------------------------------------- #
# 1) 模板渲染
# --------------------------------------------------------------------------- #
def test_render_fact_template_status_query():
    t = render_fact_template(Intent.STATUS_QUERY, order=_Order(), logistics=_Logi())
    assert "o_1002" in t and "已送达" in t and "wb_2" in t


def test_render_fact_template_complaint_without_order():
    t = render_fact_template(Intent.COMPLAINT)
    assert "已收到您的反馈" in t


# --------------------------------------------------------------------------- #
# 2) 后置校验
# --------------------------------------------------------------------------- #
def test_verify_grounded_rules():
    tpl = render_fact_template(Intent.STATUS_QUERY, order=_Order(), logistics=_Logi())
    allowed = {"1002", "2"}
    assert verify_grounded("您的订单 o_1002 目前：已送达，运单 wb_2 也已送达。", tpl, allowed)[0]
    assert verify_grounded("您的订单 o_1002 已送达，将赔付 50 元。", tpl, allowed)[0] is False
    assert verify_grounded("我们保证今天一定送达。", tpl, allowed)[0] is False
    assert verify_grounded("订单已经送达啦。", tpl, allowed)[0] is False  # 丢失关键实体
    assert verify_grounded("", tpl, allowed)[0] is False


# --------------------------------------------------------------------------- #
# 3) 图级：STATUS_QUERY 正常回事实（LLM 润色通过）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_status_query_answers_with_llm_polish():
    polished = "您好，您的订单 o_1002 目前状态为已送达；物流信息暂时还没有更新，请稍后再试试。"
    deps = _deps(_IntentAndTextStub(_intent(Intent.STATUS_QUERY, order_id="o_1002"), polished))
    result = await build_graph(deps).ainvoke(_state("订单 o_1002 我的快递到哪了"))
    assert result["response_type"] == "ANSWER"
    assert result["final_response"] == polished
    assert any(a["event"] == "READONLY_ANSWER" and a["payload"]["mode"] == "llm"
               for a in deps.persistence.audits)
    # 只读调用被审计
    assert {c["tool"] for c in deps.persistence.tool_calls} == \
        {"oms.get_order_detail", "tms.get_tracking_info"}
    assert deps.persistence.commands == []


@pytest.mark.asyncio
async def test_status_query_falls_back_when_polish_hallucinates():
    bad = "您的订单 o_1002 已送达，预计 3 天内到账。"
    deps = _deps(_IntentAndTextStub(_intent(Intent.STATUS_QUERY, order_id="o_1002"), bad))
    result = await build_graph(deps).ainvoke(_state("订单 o_1002 快递到哪了"))
    assert result["response_type"] == "ANSWER"
    assert "预计 3 天" not in result["final_response"]  # 新增数字被拒
    assert "o_1002" in result["final_response"] and "已送达" in result["final_response"]
    assert any(a["event"] == "READONLY_ANSWER" and a["payload"]["mode"] == "template"
               for a in deps.persistence.audits)


@pytest.mark.asyncio
async def test_status_query_without_order_id_clarifies():
    deps = _deps(_IntentOnlyStub(_intent(Intent.STATUS_QUERY)))
    result = await build_graph(deps).ainvoke(_state("我的快递到哪了"))
    assert result["response_type"] == "NEED_INFO"
    assert result["response_status"] == "WAITING_USER"


@pytest.mark.asyncio
async def test_complaint_acks_without_order_or_facts():
    deps = _deps(_IntentOnlyStub(_intent(Intent.COMPLAINT)))
    result = await build_graph(deps).ainvoke(_state("我要投诉你们客服"))
    # 投诉不是“只读答复”：必须进入人工待办（工单状态 MANUAL），否则没人接手
    assert result["response_type"] == "MANUAL"
    assert "已受理您的投诉" in result["final_response"]
    assert deps.persistence.tool_calls == []  # 投诉不取业务事实
    assert deps.persistence.commands == []
    assert any(a["event"] == "COMPLAINT_ESCALATED" for a in deps.persistence.audits)
    assert result["manual_queue"] == "after_sales_complaint"


@pytest.mark.asyncio
async def test_status_query_degrades_when_logistics_down():
    deps = _deps(_IntentOnlyStub(_intent(Intent.STATUS_QUERY, order_id="o_1002")))
    deps.logistics = _FailLogi()  # type: ignore[assignment]
    result = await build_graph(deps).ainvoke(_state("订单 o_1002 快递到哪了"))
    assert result["response_type"] == "ANSWER"
    assert "暂时无法获取物流信息" in result["final_response"]
    assert "o_1002" in result["final_response"]