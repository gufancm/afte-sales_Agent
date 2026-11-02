# -*- coding: utf-8 -*-
"""HTTP MCP 客户端的连接级重试：只重试「请求未发出」的失败，读超时不重试。"""
from __future__ import annotations

import httpx
import pytest

from app.mcp import client as client_mod


def _patch_post(monkeypatch, outcomes: list[object]) -> list[str]:
    calls: list[str] = []

    async def fake_post(self, url, json=None, **kwargs):  # noqa: ANN001, ARG001
        calls.append(url)
        outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(client_mod, "_CONNECT_BACKOFF_S", 0.0)
    return calls


async def test_connect_error_is_retried_then_succeeds(monkeypatch) -> None:
    ok = httpx.Response(200, json={"ok": True})
    calls = _patch_post(monkeypatch, [httpx.ConnectError("dns"), ok])
    assert await client_mod._post("http://mcp", "/x", {}, 1.0) == {"ok": True}
    assert len(calls) == 2


async def test_connect_error_exhausts_attempts(monkeypatch) -> None:
    calls = _patch_post(monkeypatch, [httpx.ConnectError("dns")])
    with pytest.raises(httpx.ConnectError):
        await client_mod._post("http://mcp", "/x", {}, 1.0)
    assert len(calls) == client_mod._CONNECT_ATTEMPTS


async def test_read_timeout_is_not_retried(monkeypatch) -> None:
    """读超时可能已在上游执行，不能盲目重试（写操作尤其危险）。"""
    calls = _patch_post(monkeypatch, [httpx.ReadTimeout("slow")])
    with pytest.raises(httpx.ReadTimeout):
        await client_mod._post("http://mcp", "/x", {}, 1.0)
    assert len(calls) == 1
