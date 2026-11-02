# -*- coding: utf-8 -*-
"""MCP Server（JSON-RPC / stdio）：工具注册、只读标注、真实 stdio 握手。

前几条在进程内用 ASGI 传输验证工具语义；最后一条真的起子进程走 stdio JSON-RPC，
确保不是「自称 MCP」而是协议层可被任意 Host 接入。
"""
from __future__ import annotations

import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.mcp.stdio_server import TOOL_NAMES, AsgiTransport, build_server


@pytest.fixture
async def server():
    t = AsgiTransport()
    s = build_server(t)
    try:
        yield s
    finally:
        await t.aclose()


async def test_tool_catalog_is_complete(server):
    tools = await server.list_tools()
    names = [t.name for t in tools]
    assert len(names) == 15
    assert names == list(TOOL_NAMES)
    assert sum(1 for t in tools if t.annotations and t.annotations.read_only_hint) == 10
    assert sum(1 for t in tools if t.annotations and t.annotations.destructive_hint) == 5


async def test_readonly_tool_returns_snapshot(server):
    res = await server.call_tool("oms.get_order_detail", {"order_id": "o_1002"})
    assert res.is_error is False
    assert "APPAREL" in res.content[0].text


async def test_unknown_order_maps_to_error_code(server):
    with pytest.raises(Exception) as ei:
        await server.call_tool("oms.get_order_detail", {"order_id": "nope"})
    assert "ORDER_NOT_FOUND" in str(ei.value)


async def test_write_tool_requires_idempotency_key(server):
    with pytest.raises(Exception) as ei:
        await server.call_tool("finance.execute_refund",
                               {"refund_request_id": "rr_1", "quote_id": "q",
                                "quote_hash": "h", "authorization_ref": "a",
                                "payload_hash": "p"})
    assert "idempotency_key" in str(ei.value)


async def test_tenant_is_required_by_downstream(server):
    with pytest.raises(Exception) as ei:
        await server.call_tool("oms.get_order_detail",
                               {"order_id": "o_1001", "tenant_id": ""})
    assert "TENANT_FORBIDDEN" in str(ei.value)


async def test_stdio_jsonrpc_handshake():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp.stdio_server"],
        env={"MCP_STDIO_BACKEND": "asgi", "PYTHONIOENCODING": "utf-8"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            assert init.server_info.name == "aftersale-oms"
            tools = await session.list_tools()
            assert len(tools.tools) == 15
            res = await session.call_tool("oms.get_order_detail", {"order_id": "o_1001"})
            assert res.is_error is False
            assert "PAID_UNSHIPPED" in res.content[0].text
