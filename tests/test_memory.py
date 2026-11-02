# -*- coding: utf-8 -*-
"""记忆系统回归测试：短期跨轮 / 情景 / 语义 + 非权威边界。

覆盖：
  1) 短期跨轮：同一工单多轮历史被完整回填；滚动摘要按预算生成并落库；
  2) 情景记忆：终态工单归档为摘要 + 向量；按用户/租户作用域召回；
  3) 语义记忆：用户画像读写（非权威）；
  4) 边界：确定性节点（政策/风控/执行前复核等）绝不读取记忆上下文。
"""
from __future__ import annotations

import pathlib
from types import SimpleNamespace

import pytest

pytest.importorskip("langgraph")

from app.bootstrap import build_deps  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db.memory import InMemoryPersistence  # noqa: E402
from app.domain.canonical import sha256_hex  # noqa: E402
from app.graph import nodes  # noqa: E402
from app.memory.service import MemoryService, render_memory_note  # noqa: E402

NODES_SRC = pathlib.Path(__file__).resolve().parents[1] / "app" / "graph" / "nodes.py"


class FakeTextLLM:
    """只实现 .text()；用于摘要/归档措辞。"""

    def __init__(self, reply: str = "摘要：用户咨询物流，订单 o_1001 已签收。") -> None:
        self.reply = reply
        self.calls: list[dict] = []

    async def text(self, *, messages, prompt_version):
        self.calls.append({"prompt_version": prompt_version, "messages": messages})
        return self.reply, {"model": "fake", "usage": {}, "request_hash": "x", "response_hash": "y"}


class FakeEmbedder:
    """确定性字符袋向量：相同文本余弦=1，便于断言召回闭环。"""

    model = "fake-embedder"

    async def embed(self, texts):
        out = []
        for t in texts:
            vec = [0.0] * 8
            for ch in t:
                vec[ord(ch) % 8] += 1.0
            out.append(vec)
        return out


async def _seed_ticket(p: InMemoryPersistence, *, tenant_id="t1", user_id="u1") -> str:
    t = await p.create_ticket(tenant_id=tenant_id, user_id=user_id, order_id="o_1001", intent="UNKNOWN")
    return t["ticket_id"]


async def _append(p: InMemoryPersistence, tid: str, role: str, text: str, key: str) -> None:
    await p.append_message(tenant_id="t1", ticket_id=tid, thread_id="th", role=role, content=text,
                           content_hash=sha256_hex(text), idempotency_key=key)


def _settings(**kw) -> Settings:
    base = dict(_env_file=None, app_env="test", database_url="memory://")
    base.update(kw)
    return Settings(**base)


# --------------------------------------------------------------------------- #
# 1) 短期跨轮记忆
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_short_term_history_replayed_across_turns():
    p = InMemoryPersistence()
    svc = MemoryService(settings=_settings(), persistence=p)
    tid = await _seed_ticket(p)
    for i, (u, a) in enumerate([("我想查物流", "已受理0"), ("订单 o_1001 到哪了", "已受理1")]):
        await _append(p, tid, "user", u, f"u{i}")
        await _append(p, tid, "assistant", a, f"a{i}")

    conv = await svc.load_conversation(tenant_id="t1", ticket_id=tid)
    assert [m["content"] for m in conv.messages] == ["我想查物流", "已受理0",
                                                    "订单 o_1001 到哪了", "已受理1"]
    assert [m["role"] for m in conv.messages] == ["user", "assistant", "user", "assistant"]
    assert conv.summary is None
    assert conv.stats["history_messages"] == 4


@pytest.mark.asyncio
async def test_intent_prompt_sees_full_history_and_summary():
    deps = build_deps(_settings())
    state = {"messages": [{"role": "user", "content": "第一轮：查物流"},
                          {"role": "assistant", "content": "已回复"},
                          {"role": "user", "content": "第二轮：还是没到"}],
             "conversation_summary": {"text": "用户此前咨询物流。"},
             "memory_context": {"profile": {"last_intent": "STATUS_QUERY"},
                                "episodes": [{"outcome_type": "ACCEPTED", "intent": "REFUND_ONLY",
                                              "summary": "上次退款已受理"}]}}
    msgs = nodes._intent_messages(state, deps)
    joined = "\n".join(m["content"] for m in msgs)
    assert "第二轮：还是没到" in joined          # 多轮历史进入意图提示
    assert "用户此前咨询物流。" in joined        # 滚动摘要注入
    assert "上次退款已受理" in joined            # 情景记忆注入
    assert "last_intent=STATUS_QUERY" in joined  # 画像注入
    assert msgs[0]["content"] == deps.prompts.get("intent_guardrail", "")


@pytest.mark.asyncio
async def test_rolling_summary_created_persisted_and_budgeted():
    settings = _settings(memory_recent_turns=1, memory_max_turns=2, memory_summary_tokens=5)
    p = InMemoryPersistence()
    llm = FakeTextLLM("用户咨询物流；订单 o_1001 已签收。")
    svc = MemoryService(settings=settings, persistence=p, llm=llm)
    tid = await _seed_ticket(p)
    for i, (u, a) in enumerate([("我要查物流，麻烦帮我看看", "已查询"),
                                ("订单 o_1001 现在到哪了", "运输中")]):
        await _append(p, tid, "user", u, f"u{i}")
        await _append(p, tid, "assistant", a, f"a{i}")

    conv = await svc.load_conversation(tenant_id="t1", ticket_id=tid)
    # 只保留最近 1 轮（2 条）逐字文本，更早的内容折叠为摘要
    assert [m["content"] for m in conv.messages] == ["订单 o_1001 现在到哪了", "运输中"]
    assert conv.summary == "用户咨询物流；订单 o_1001 已签收。"
    assert conv.summarized_count == 2 and conv.stats["summarized"] is True
    assert llm.calls and llm.calls[0]["prompt_version"] == "memory-summary-v1"

    stored = await p.get_conversation_summary(tenant_id="t1", ticket_id=tid)
    assert stored == {"summary": "用户咨询物流；订单 o_1001 已签收。", "up_to_message_count": 2}

    # 第二轮：已有摘要且覆盖更早内容 -> 不重复调用 LLM
    again = await svc.load_conversation(tenant_id="t1", ticket_id=tid)
    assert again.summary == conv.summary and len(llm.calls) == 1


@pytest.mark.asyncio
async def test_summary_skipped_when_llm_disabled():
    settings = _settings(memory_recent_turns=1, memory_summary_tokens=1)
    p = InMemoryPersistence()
    svc = MemoryService(settings=settings, persistence=p, llm=None)
    tid = await _seed_ticket(p)
    for i in range(4):
        await _append(p, tid, "user", f"很长的历史消息 {i}" * 5, f"u{i}")
    conv = await svc.load_conversation(tenant_id="t1", ticket_id=tid)
    assert conv.summary is None            # 无 LLM 时不生成摘要
    assert len(conv.messages) <= 2         # 仅按窗口截断
    assert conv.stats["older_messages"] > 0


# --------------------------------------------------------------------------- #
# 2) 情景记忆
# --------------------------------------------------------------------------- #
def _episode_state() -> dict:
    return {"tenant_id": "t1", "user_id": "u1", "ticket_id": "tk1", "run_id": "r1",
            "intent_result": {"intent": "REFUND_ONLY"},
            "slots": {"order_id": "o_1001", "reason_code": "DAMAGED"},
            "order_snapshot": {"category": "FRESH", "order_id": "o_1001"},
            "policy_decision": {"decision": "ALLOW", "reason_code": "FRESH_DAMAGE",
                                "policy_decision_id": "pd1"},
            "action_plan": {"actions": [{"action": "REQUEST_REFUND_ONLY"}]},
            "quote_amount_minor": 20000}


@pytest.mark.asyncio
async def test_episodic_remember_and_vector_recall():
    settings = _settings(memory_episodic_min_score=0.9)
    p = InMemoryPersistence()
    svc = MemoryService(settings=settings, persistence=p, embedder=FakeEmbedder())
    state = _episode_state()
    mid = await svc.remember_episode(tenant_id="t1", user_id="u1", ticket_id="tk1", state=state,
                                     response_type="ACCEPTED", response_text="已受理退款 200.00")
    assert mid and len(p.episodic_memories) == 1
    facts = MemoryService._episode_facts(state=state, response_type="ACCEPTED",
                                        response_text="已受理退款 200.00")

    hits = await svc.recall_episodes(tenant_id="t1", user_id="u1", query=facts)
    assert len(hits) == 1
    assert hits[0]["outcome_type"] == "ACCEPTED" and hits[0]["intent"] == "REFUND_ONLY"
    assert hits[0]["score"] >= 0.9

    # 租户隔离：其他租户召不到
    assert await svc.recall_episodes(tenant_id="t2", user_id="u1", query=facts) == []
    # 用户隔离：其他用户召不到
    assert await svc.recall_episodes(tenant_id="t1", user_id="u2", query=facts) == []


@pytest.mark.asyncio
async def test_episodic_respects_min_score_and_disabled():
    p = InMemoryPersistence()
    svc = MemoryService(settings=_settings(memory_episodic_min_score=1.5), persistence=p,
                        embedder=FakeEmbedder())
    state = _episode_state()
    await svc.remember_episode(tenant_id="t1", user_id="u1", ticket_id="tk1", state=state,
                               response_type="ACCEPTED", response_text="已受理")
    facts = MemoryService._episode_facts(state=state, response_type="ACCEPTED", response_text="已受理")
    assert await svc.recall_episodes(tenant_id="t1", user_id="u1", query=facts) == []  # 阈值过高

    off = MemoryService(settings=_settings(memory_episodic_enabled=False), persistence=p,
                        embedder=FakeEmbedder())
    assert await off.recall_episodes(tenant_id="t1", user_id="u1", query=facts) == []
    assert await off.remember_episode(tenant_id="t1", user_id="u1", ticket_id="tk2", state=state,
                                      response_type="ACCEPTED", response_text="x") is None


@pytest.mark.asyncio
async def test_episodic_only_archived_for_terminal_types():
    p = InMemoryPersistence()
    svc = MemoryService(settings=_settings(), persistence=p, embedder=FakeEmbedder())
    for rtype in ("NEED_INFO", "UNKNOWN"):
        assert await svc.remember_episode(tenant_id="t1", user_id="u1", ticket_id="tk1",
                                          state=_episode_state(), response_type=rtype,
                                          response_text="请补充订单号") is None
    assert p.episodic_memories == []


# --------------------------------------------------------------------------- #
# 3) 语义记忆
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_semantic_profile_roundtrip_and_patch():
    p = InMemoryPersistence()
    svc = MemoryService(settings=_settings(), persistence=p)
    assert await svc.get_profile(tenant_id="t1", user_id="u1") == {}
    await svc.update_profile(tenant_id="t1", user_id="u1", patch={"last_intent": "REFUND_ONLY"})
    await svc.update_profile(tenant_id="t1", user_id="u1",
                             patch={"locale": "en-US", "last_category": None})  # None 不入库
    assert await svc.get_profile(tenant_id="t1", user_id="u1") == {"last_intent": "REFUND_ONLY",
                                                                   "locale": "en-US"}
    # 租户隔离
    assert await svc.get_profile(tenant_id="t2", user_id="u1") == {}

    patch = MemoryService.profile_patch_from_state(
        {"intent_result": {"intent": "RETURN_REFUND"}, "order_snapshot": {"category": "APPAREL"},
         "locale": "zh-CN"})
    assert patch == {"last_intent": "RETURN_REFUND", "last_category": "APPAREL", "locale": "zh-CN"}
    assert MemoryService.profile_patch_from_state({"intent_result": {"intent": "UNKNOWN"}}) == {}


@pytest.mark.asyncio
async def test_semantic_disabled_returns_empty():
    p = InMemoryPersistence()
    svc = MemoryService(settings=_settings(memory_semantic_enabled=False), persistence=p)
    await svc.update_profile(tenant_id="t1", user_id="u1", patch={"last_intent": "REFUND_ONLY"})
    assert p.user_profiles == {} and await svc.get_profile(tenant_id="t1", user_id="u1") == {}


@pytest.mark.asyncio
async def test_build_context_aggregates_profile_and_episodes():
    p = InMemoryPersistence()
    svc = MemoryService(settings=_settings(memory_episodic_min_score=0.9), persistence=p,
                        embedder=FakeEmbedder())
    state = _episode_state()
    await svc.remember_episode(tenant_id="t1", user_id="u1", ticket_id="tk1", state=state,
                               response_type="ACCEPTED", response_text="已受理")
    await svc.update_profile(tenant_id="t1", user_id="u1", patch={"last_intent": "REFUND_ONLY"})
    facts = MemoryService._episode_facts(state=state, response_type="ACCEPTED", response_text="已受理")
    ctx = await svc.build_context(tenant_id="t1", user_id="u1", ticket_id="tk2", query=facts)
    assert ctx.is_empty() is False
    assert ctx.profile["last_intent"] == "REFUND_ONLY"
    assert len(ctx.episodes) == 1 and ctx.meta["episodic_count"] == 1


# --------------------------------------------------------------------------- #
# 4) response_composer 收口：写回消息 + 归档 + 画像
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_response_composer_writes_history_episode_and_profile():
    deps = build_deps(_settings())
    p = deps.persistence
    tid = await _seed_ticket(p)
    state = {"tenant_id": "t1", "user_id": "u1", "ticket_id": tid, "run_id": "r1", "thread_id": "th",
             "trace_id": "tr", "intent_result": {"intent": "REFUND_ONLY"},
             "order_snapshot": {"category": "FRESH", "order_id": "o_1001"},
             "policy_decision": {"decision": "ALLOW", "reason_code": "FRESH_DAMAGE"},
             "action_plan": {"actions": [{"action": "REQUEST_REFUND_ONLY"}]}, "command_ids": ["c1"]}
    out = await nodes.response_composer(state, deps)
    assert out["response_type"] == "ACCEPTED"
    roles = [(m["role"], m["message_seq"]) for m in p.messages[tid]]
    assert roles == [("assistant", 1)]                       # 回复写回消息历史
    assert len(p.episodic_memories) == 1                     # 终态归档情景记忆
    assert p.user_profiles[("t1", "u1")]["last_intent"] == "REFUND_ONLY"  # 画像更新

    # 中间态（NEED_INFO）不归档情景记忆
    mid_state = {"tenant_id": "t1", "user_id": "u1", "ticket_id": tid, "run_id": "r2",
                 "thread_id": "th", "trace_id": "tr",
                 "policy_decision": {"decision": "NEED_INFO", "missing_facts": ["订单号"]}}
    out2 = await nodes.response_composer(mid_state, deps)
    assert out2["response_type"] == "NEED_INFO"
    assert len(p.episodic_memories) == 1


@pytest.mark.asyncio
async def test_load_memory_helper_populates_state_from_routes():
    from app.api.routes import _load_memory

    deps = build_deps(_settings())
    p = deps.persistence
    tid = await _seed_ticket(p)
    await _append(p, tid, "user", "第一轮：查物流", "u0")
    await _append(p, tid, "assistant", "已回复0", "a0")

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(deps=deps)))
    state = {"tenant_id": "t1", "user_id": "u1", "ticket_id": tid, "messages": []}
    await _load_memory(request, state, query="第二轮：还是没到")
    assert [m["content"] for m in state["messages"]] == ["第一轮：查物流", "已回复0"]
    assert state["memory_meta"]["history_messages"] == 2


# --------------------------------------------------------------------------- #
# 5) 非权威边界：确定性节点绝不读取记忆
# --------------------------------------------------------------------------- #
def test_deterministic_nodes_do_not_read_memory_context():
    src = NODES_SRC.read_text(encoding="utf-8")
    segments = src.split("\nasync def ")
    guarded = ["policy_engine", "risk_authorization", "action_preparation", "pre_execution_guard",
               "command_submitter", "context_aggregator", "hitl_review",
               "_guard_refund", "_guard_intercept", "_guard_expedite", "_guard_reshipment"]
    for name in guarded:
        seg = next(s for s in segments if s.startswith(name + "("))
        assert "memory_context" not in seg, f"{name} 读取了记忆上下文"
        assert "render_memory_note" not in seg, f"{name} 读取了记忆提示"
        assert "deps.memory" not in seg, f"{name} 引用了记忆服务"


@pytest.mark.asyncio
async def test_memory_metrics_exposed():
    from app.observability import metrics as M

    p = InMemoryPersistence()
    svc = MemoryService(settings=_settings(memory_episodic_min_score=0.9), persistence=p,
                        embedder=FakeEmbedder())
    state = _episode_state()
    await svc.remember_episode(tenant_id="t1", user_id="u1", ticket_id="tk1", state=state,
                               response_type="ACCEPTED", response_text="已受理")
    facts = MemoryService._episode_facts(state=state, response_type="ACCEPTED", response_text="已受理")
    await svc.recall_episodes(tenant_id="t1", user_id="u1", query=facts)
    text = M.render().decode("utf-8")
    assert "aftersale_memory_episodic_total" in text
    assert "aftersale_memory_recall_total" in text


def test_memory_note_marks_non_authoritative():
    note = render_memory_note({"conversation_summary": {"text": "S"},
                               "memory_context": {"profile": {"locale": "en-US"},
                                                  "episodes": [{"outcome_type": "DENIED",
                                                                "intent": "RETURN_REFUND",
                                                                "summary": "E"}]}})
    assert "非权威" in note and "历史会话摘要" in note
    assert "用户偏好画像" in note and "历史工单记忆" in note
    assert render_memory_note({}) == ""
