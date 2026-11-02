"""图冒烟：LLM 关闭时应走只读/人工，绝不产生写命令（§2.2/§0.4）。"""
import pytest

pytest.importorskip("langgraph")

from app.bootstrap import build_deps  # noqa: E402
from app.config import Settings  # noqa: E402
from app.graph.build import build_graph  # noqa: E402


@pytest.mark.asyncio
async def test_graph_llm_disabled_routes_to_clarify_or_manual():
    settings = Settings(_env_file=None, app_env="test", database_url="memory://", llm_model="", finance_real_writes_enabled=False)
    deps = build_deps(settings)
    graph = build_graph(deps)
    state = {"tenant_id": "t1", "user_id": "u_demo", "ticket_id": "tk1", "thread_id": "th1",
             "run_id": "r1", "trace_id": "tr1", "messages": [{"role": "user", "content": "生鲜坏了想退款"}]}
    result = await graph.ainvoke(state)
    assert result.get("response_status") in ("WAITING_USER", "COMPLETED")
    assert (deps.persistence.commands == [])  # 无写命令