"""依赖装配：按配置选择内存/真实实现（LLD §19.2）。

外部工具装配分三层：
  1) 原始（可写）端口 `WriteTools`：仅 SagaDispatcher/Worker/联调脚本持有；
  2) ToolGateway：只读调用的超时/重试/熔断/限流/校验/审计；
  3) 只读门面 `ReadToolkit`：注入给图节点，只暴露只读方法。
"""
from __future__ import annotations

import pathlib
from typing import Any

from app.config import Settings
from app.graph.deps import Deps
from app.llm.adapter import build_llm, build_llm_small
from app.mcp.gateway import ToolGateway, WriteTools, build_policies, build_read_toolkit
from app.policy.engine import PolicyEngine
from app.policy.library import load_clauses, load_rules
from app.policy.repository import InMemoryRuleRepository
from app.rag.memory import InMemoryClauseStore, InMemoryDense, InMemoryLexical, NoopReranker
from app.rag.retrieval import Retriever

PROMPT_DIR = pathlib.Path(__file__).parent / "llm" / "prompts"


def load_prompts() -> dict[str, str]:
    out: dict[str, str] = {}
    for p in PROMPT_DIR.glob("*.txt"):
        out[p.stem] = p.read_text(encoding="utf-8")
    return out


def _build_retriever(settings: Settings, session_factory: Any, *, llm: Any = None,
                     prompts: dict[str, str] | None = None) -> Retriever:
    """检索链路：查询变换（多查询/HyDE）-> BM25 词法 + Dense 向量 -> RRF -> BGE Reranker。
    rag_backend=pg 时从 policy_chunks 读取（需 DB + embedding）；否则用内存条款（无 DB 也能跑通）。"""
    transformer = None
    if llm is not None and settings.query_transform_enabled:
        from app.rag.query_transform import LLMQueryTransformer
        transformer = LLMQueryTransformer(
            llm, prompts, multi_n=settings.rag_multi_query_n,
            use_rewrite=settings.rag_query_rewrite_enabled,
            use_hyde=settings.rag_hyde_enabled,
            cache_ttl_s=settings.rag_transform_cache_ttl_s,
            cache_max=settings.rag_transform_cache_max,
            max_variant_chars=settings.rag_transform_max_variant_chars,
            max_calls_per_day=settings.rag_transform_daily_call_budget)

    if session_factory is not None and settings.rag_backend == "pg":
        from app.rag.pg import HttpReranker, NoopReranker as PgNoop
        from app.rag.pg import OpenAICompatEmbedder, PgBM25, PgClauseStore, PgDense
        if not settings.embeddings_enabled and settings.rag_require_embeddings and not settings.rag_allow_lexical_only:
            raise RuntimeError(
                "RAG_BACKEND=pg 但未配置 EMBEDDING_MODEL：Dense 检索缺失（拒绝静默退化）。"
                "请配置 EMBEDDING_* 或显式设置 RAG_ALLOW_LEXICAL_ONLY=true（仅 BM25，会告警）。")
        embedder = None
        if settings.embeddings_enabled:
            embedder = OpenAICompatEmbedder(settings.embedding_base_url or settings.llm_base_url,
                                            settings.embedding_api_key or settings.llm_api_key,
                                            settings.embedding_model)
        clauses: Any = PgClauseStore(session_factory)
        lexical: Any = PgBM25(session_factory, k1=settings.rag_bm25_k1, b=settings.rag_bm25_b)
        dense: Any = PgDense(session_factory, embedder=embedder)
        if settings.rerank_enabled:
            reranker: Any = HttpReranker(settings.rerank_base_url, settings.rerank_model,
                                         api_key=(settings.rerank_api_key or None),
                                         timeout_s=settings.rerank_timeout_ms / 1000.0)
        else:
            reranker = PgNoop()
    else:
        store = InMemoryClauseStore(load_clauses())
        clauses = store
        lexical = InMemoryLexical(store, k1=settings.rag_bm25_k1, b=settings.rag_bm25_b)
        dense, reranker = InMemoryDense(), NoopReranker()

    return Retriever(
        lexical=lexical, dense=dense, reranker=reranker, clauses=clauses,
        bm25_top_k=settings.rag_bm25_top_k, dense_top_k=settings.rag_dense_top_k,
        rrf_k=settings.rag_rrf_k, rrf_top_n=settings.rag_rerank_candidates,
        rerank_top_n=settings.rag_final_top_k, transformer=transformer,
        transform_policy=settings.rag_transform_policy,
        adaptive_min_dense_sim=settings.rag_adaptive_min_dense_sim,
        adaptive_min_margin=settings.rag_adaptive_min_margin,
        adaptive_short_query_chars=settings.rag_adaptive_short_query_chars,
        adaptive_min_candidates=settings.rag_adaptive_min_candidates,
        adaptive_soft_signals_required=settings.rag_adaptive_soft_signals_required,
        adaptive_context_chars=settings.rag_adaptive_context_chars,
        adaptive_context_messages=settings.rag_adaptive_context_messages,
        adaptive_use_required_coverage=settings.rag_adaptive_use_required_coverage,
        adaptive_min_required_coverage=settings.rag_adaptive_min_required_coverage,
        transform_timeout_s=settings.rag_transform_timeout_ms / 1000.0,
    )


def build_write_tools(settings: Settings, *, use_memory: bool) -> WriteTools:
    """构造原始（可写）端口。内存模式使用共享内存实现，便于测试与 Worker 共享状态。"""
    if use_memory:
        from app.mcp.mocks.inmemory import InMemoryFinance, InMemoryOMS, InMemoryTMS
        from app.mcp.ports import LogisticsSnapshot, OrderSnapshot
        orders, logistics, finance = InMemoryOMS(), InMemoryTMS(), InMemoryFinance()
        # 演示数据与 app/mcp/mocks/server.py（HTTP mock）保持一致，保证内存后端与容器后端行为一致
        orders.seed(OrderSnapshot(order_id="o_1001", owner_user_id="u_demo", status="PAID_UNSHIPPED",
                                  version="v1", category="FRESH", currency="CNY",
                                  lines=[{"order_line_id": "l1", "sku_id": "sku_1", "quantity": 1}]))
        orders.seed(OrderSnapshot(order_id="o_1002", owner_user_id="u_demo", status="DELIVERED",
                                  version="v1", category="APPAREL", currency="CNY",
                                  lines=[{"order_line_id": "l1", "sku_id": "sku_a1", "quantity": 1}]))
        orders.seed(OrderSnapshot(order_id="o_1003", owner_user_id="u_demo", status="DELIVERED",
                                  version="v1", category="DIGITAL", currency="CNY",
                                  lines=[{"order_line_id": "l1", "sku_id": "sku_d1", "quantity": 1}]))
        orders.seed(OrderSnapshot(order_id="o_1004", owner_user_id="u_demo", status="DELIVERED",
                                  version="v1", category="BOOK_MEDIA", currency="CNY",
                                  lines=[{"order_line_id": "l1", "sku_id": "sku_m1", "quantity": 1}]))
        orders.seed(OrderSnapshot(order_id="o_1005", owner_user_id="u_demo", status="PAID_UNSHIPPED",
                                  version="v1", category="PRESALE", currency="CNY",
                                  lines=[{"order_line_id": "l1", "sku_id": "sku_p1", "quantity": 1}]))
        logistics.seed(LogisticsSnapshot(waybill_no="wb_1", parcel_status="IN_TRANSIT", version="t1"))
        logistics.seed(LogisticsSnapshot(waybill_no="wb_2", parcel_status="DELIVERED", version="t2"))
        logistics.seed(LogisticsSnapshot(waybill_no="wb_3", parcel_status="DELIVERED", version="t3"))
        finance.seed("o_1001", 20000)
        finance.seed("o_1002", 50000)
        finance.seed("o_1003", 300000)
        finance.seed("o_1004", 8800)
        finance.seed("o_1005", 10000)
        return WriteTools(orders=orders, logistics=logistics, finance=finance)
    from app.mcp.client import HttpFinance, HttpOMS, HttpTMS
    # HTTP 客户端超时略大于网关超时，确保先由网关判超时并记录（网关内部为权威超时）
    timeout_s = settings.mcp_tool_timeout_ms / 1000 + 5.0
    return WriteTools(orders=HttpOMS(settings.oms_mcp_url, timeout_s=timeout_s),
                      logistics=HttpTMS(settings.tms_mcp_url, timeout_s=timeout_s),
                      finance=HttpFinance(settings.finance_mcp_url, timeout_s=timeout_s))


def _build_embedder(settings: Settings):
    """OpenAI 兼容 embedding 客户端（与检索链路同源）；未配置则 None。"""
    if not settings.embeddings_enabled:
        return None
    from app.rag.pg import OpenAICompatEmbedder
    return OpenAICompatEmbedder(settings.embedding_base_url or settings.llm_base_url,
                                settings.embedding_api_key or settings.llm_api_key,
                                settings.embedding_model)


def build_deps(settings: Settings) -> Deps:
    use_memory = settings.app_env in ("test",) or settings.database_url.startswith("memory")

    # 持久化
    session_factory = None
    if use_memory:
        from app.db.memory import InMemoryPersistence
        persistence: Any = InMemoryPersistence()
    else:
        from app.db.engine import make_engine, make_sessionmaker
        from app.db.persistence import SqlPersistence
        engine = make_engine(settings)
        session_factory = make_sessionmaker(engine)
        persistence = SqlPersistence(session_factory)

    # 外部端口：原始（可写）+ 网关 + 只读门面
    write_tools = build_write_tools(settings, use_memory=use_memory)

    async def _on_tool_call(record: dict) -> None:
        await persistence.record_tool_call(**record)

    gateway = ToolGateway(policies=build_policies(settings), on_call=_on_tool_call,
                          max_concurrency=settings.mcp_tool_max_concurrency)
    read_toolkit = build_read_toolkit(write_tools, gateway)

    llm = build_llm(settings)
    llm_small = build_llm_small(settings)
    prompts = load_prompts()
    retriever = _build_retriever(settings, session_factory, llm=llm, prompts=prompts)
    # 规则来自 policy/rules.json（与入库内容一致）；缺失时回退内置示例。
    policy = PolicyEngine(repo=InMemoryRuleRepository(load_rules()))
    # 记忆系统：短期跨轮 / 情景 / 语义。非权威，仅注入理解与措辞，失败即降级。
    from app.memory.service import MemoryService
    memory = MemoryService(settings=settings, persistence=persistence, llm=llm,
                           embedder=_build_embedder(settings), prompts=prompts)

    return Deps(settings=settings, llm=llm, llm_small=llm_small, policy=policy, retriever=retriever,
                orders=read_toolkit.orders, logistics=read_toolkit.logistics, finance=read_toolkit.finance,
                persistence=persistence, prompts=prompts, tools=write_tools, memory=memory)