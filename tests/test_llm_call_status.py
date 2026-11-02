"""llm_calls.status 必须落在 LLD §10.7 词表内（SUCCESS/SCHEMA_ERROR/TIMEOUT/ERROR）。

回归：上游失败曾写入 "UPSTREAM_ERROR"，违反 DB CHECK 约束导致接口 500。
"""
from __future__ import annotations

import pytest

from app.bootstrap import build_deps
from app.config import Settings
from app.graph.nodes import _llm_failure_status, intent_guardrail

ALLOWED = {"SUCCESS", "SCHEMA_ERROR", "TIMEOUT", "ERROR"}


class TimeoutLLM:
    async def structured(self, **kw):
        raise TimeoutError("Request timed out.")


class BrokenLLM:
    async def structured(self, **kw):
        raise RuntimeError("boom")


def _deps(llm):
    deps = build_deps(Settings(_env_file=None, app_env="test", database_url="memory://", llm_model="stub"))
    deps.llm = llm
    return deps


def _state():
    return {"tenant_id": "tenant_demo", "user_id": "u_demo", "ticket_id": "t1", "run_id": "r1",
            "trace_id": "tr1", "messages": [{"role": "user", "content": "要退款"}]}


def test_failure_status_vocabulary():
    assert _llm_failure_status(TimeoutError("Request timed out.")) == "TIMEOUT"
    assert _llm_failure_status(RuntimeError("boom")) == "ERROR"
    assert {"TIMEOUT", "ERROR"} <= ALLOWED


@pytest.mark.asyncio
@pytest.mark.parametrize("llm,expected", [(TimeoutLLM(), "TIMEOUT"), (BrokenLLM(), "ERROR")])
async def test_upstream_failure_records_allowed_status(llm, expected):
    deps = _deps(llm)
    await intent_guardrail(_state(), deps)
    calls = deps.persistence.llm_calls
    assert calls, "应记录一次 llm_call"
    assert calls[-1]["status"] == expected, calls
    assert calls[-1]["status"] in ALLOWED
