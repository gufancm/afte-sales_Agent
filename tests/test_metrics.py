# -*- coding: utf-8 -*-
"""可观测性：Prometheus 指标渲染 + 持久化埋点。"""
from __future__ import annotations

import pytest

from app.db.memory import InMemoryPersistence
from app.observability import metrics as M


def _text() -> str:
    return M.render().decode("utf-8")


def test_render_contains_metric_families():
    text = _text()
    for name in ("aftersale_http_requests_total", "aftersale_llm_calls_total",
                 "aftersale_tool_calls_total", "aftersale_commands_total",
                 "aftersale_tickets_total"):
        assert name in text


def test_record_llm_and_tool_labels():
    M.record_llm(node="intent_guardrail", status="SUCCESS", latency_ms=42, input_tokens=100, output_tokens=20)
    M.record_tool(tool="oms.get_order_detail", status="SUCCESS", latency_ms=7)
    text = _text()
    assert 'aftersale_llm_calls_total{node="intent_guardrail",status="SUCCESS"}' in text
    assert "aftersale_llm_latency_ms_count" in text
    assert 'aftersale_llm_tokens_total{direction="input"}' in text
    assert "aftersale_tool_calls_total{" in text and 'tool="oms.get_order_detail"' in text


@pytest.mark.asyncio
async def test_persistence_write_paths_emit_metrics():
    p = InMemoryPersistence()
    await p.create_ticket(tenant_id="t1", user_id="u1", order_id="o_1001", intent="REFUND_ONLY")
    await p.enqueue_command(tenant_id="t1", ticket_id="tk1", run_id="r1", action="INTERCEPT_PARCEL",
                            order_id="o_1001", refund_request_id=None, quote_id=None, quote_hash=None,
                            waybill_no="wb_1", sku_ids=[], plan_id=None, authorization_ref=None,
                            payload_hash="ph", payload={})
    await p.record_tool_call(tenant_id="t1", tool="tms.get_tracking_info", status="ERROR", latency_ms=15)
    text = _text()
    assert "aftersale_tickets_total" in text
    assert 'aftersale_commands_total{action="INTERCEPT_PARCEL"}' in text
    assert "aftersale_tool_calls_total{" in text and 'tool="tms.get_tracking_info"' in text
