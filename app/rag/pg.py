"""Postgres 检索后端：条款库 + 真实 BM25 词法检索 + pgvector Dense（可选）。
- BM25：jieba 分词（app.rag.tokenize）写入 tokens{term:tf}/doc_len，SQL 侧按 idf/tf 饱和/长度归一化打分。
- Dense：pgvector 近邻；未配置 embedding 时**不静默为空**——由 config.rag_require_embeddings 在启动期拦截。
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.engine import tenant_transaction
from app.rag.retrieval import PolicyChunk
from app.rag.tokenize import tokenize


class Embedder(Protocol):
    model: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class PgClauseStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.sf = session_factory

    async def fetch_many(self, tenant_id: str, clause_ids: list[str], as_of: str) -> list[PolicyChunk]:
        if not clause_ids:
            return []
        async with tenant_transaction(self.sf, tenant_id) as s:
            rows = (await s.execute(text(
                "SELECT clause_id,doc_id,version,text,clause_group_id,is_exception,logical_clause_id FROM policy_chunks "
                "WHERE tenant_id=:ten AND (clause_id = ANY(:ids) OR logical_clause_id = ANY(:ids))"),
                {"ten": tenant_id, "ids": clause_ids})).mappings().all()
        return [PolicyChunk(clause_id=r["clause_id"], doc_id=r["doc_id"], version=r["version"], text=r["text"],
                            clause_group_id=r["clause_group_id"], is_exception=r["is_exception"],
                            logical_clause_id=r["logical_clause_id"]) for r in rows]

    async def fetch_group(self, tenant_id: str, clause_group_id: str, as_of: str) -> list[PolicyChunk]:
        async with tenant_transaction(self.sf, tenant_id) as s:
            rows = (await s.execute(text(
                "SELECT clause_id,doc_id,version,text,clause_group_id,is_exception,logical_clause_id FROM policy_chunks "
                "WHERE tenant_id=:ten AND clause_group_id=:gid AND is_exception IS TRUE"),
                {"ten": tenant_id, "gid": clause_group_id})).mappings().all()
        return [PolicyChunk(clause_id=r["clause_id"], doc_id=r["doc_id"], version=r["version"], text=r["text"],
                            clause_group_id=r["clause_group_id"], is_exception=r["is_exception"],
                            logical_clause_id=r["logical_clause_id"]) for r in rows]


_BM25_SQL = (
    "WITH stats AS ("
    "  SELECT count(*)::float8 AS n, COALESCE(avg(doc_len), 1)::float8 AS avgdl "
    "  FROM policy_chunks WHERE tenant_id = :ten AND doc_len IS NOT NULL),"
    " idf AS ("
    "  SELECT t.term, ln(1 + GREATEST(stats.n - dfv + 0.5, 0) / (dfv + 0.5)) AS val "
    "  FROM (SELECT DISTINCT unnest(CAST(:terms AS text[])) AS term) t "
    "  CROSS JOIN LATERAL ("
    "    SELECT count(*)::float8 AS dfv FROM policy_chunks p "
    "    WHERE p.tenant_id = :ten AND jsonb_exists(p.tokens, t.term)) d "
    "  CROSS JOIN stats)"
    " SELECT p.clause_id, p.doc_id, p.version, p.text, p.clause_group_id, p.is_exception, p.logical_clause_id,"
    "   SUM(idf.val * (tf.tfn * (:k1 + 1)) /"
    "       (tf.tfn + :k1 * (1 - :b + :b * (COALESCE(p.doc_len, 1)::float8 / GREATEST(stats.avgdl, 1)))))::float8 AS score"
    " FROM policy_chunks p CROSS JOIN stats JOIN idf ON true"
    " CROSS JOIN LATERAL (SELECT COALESCE(NULLIF(p.tokens ->> idf.term, '')::float8, 0) AS tfn) tf"
    " WHERE p.tenant_id = :ten AND jsonb_exists_any(p.tokens, CAST(:terms AS text[])) AND tf.tfn > 0"
    " GROUP BY p.clause_id, p.doc_id, p.version, p.text, p.clause_group_id, p.is_exception,"
    "          p.logical_clause_id, stats.avgdl"
    " ORDER BY score DESC LIMIT :k"
)


class PgBM25:
    """真实 BM25（k1=1.2 / b=0.75，jieba 分词）。GIN(jsonb_path_ops) 加速 tokens 命中。"""

    enabled = True
    name = "pg-bm25"

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, k1: float = 1.2,
                 b: float = 0.75) -> None:
        self.sf = session_factory
        self.k1 = float(k1)
        self.b = float(b)

    async def search(self, tenant_id: str, query: str, top_k: int, filters: dict) -> list[PolicyChunk]:
        terms = tokenize(query)
        if not terms:
            return []
        async with tenant_transaction(self.sf, tenant_id) as s:
            rows = (await s.execute(text(_BM25_SQL), {
                "ten": tenant_id, "terms": terms, "k": top_k, "k1": self.k1, "b": self.b})).mappings().all()
        return [PolicyChunk(clause_id=r["clause_id"], doc_id=r["doc_id"], version=r["version"], text=r["text"],
                            clause_group_id=r["clause_group_id"], is_exception=r["is_exception"],
                            logical_clause_id=r["logical_clause_id"], score=float(r["score"] or 0.0))
                for r in rows]


class PgDense:
    """pgvector 近邻检索。enabled=False 表示未配置 embedding（启动期已按配置拦截，避免静默为空）。

    score 为 cosine distance（越小越相似）；Retriever 依 score_is_distance 统一成相似度。
    """

    name = "pg-dense"
    score_is_distance = True

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], embedder: Embedder | None = None) -> None:
        self.sf = session_factory
        self.embedder = embedder

    @property
    def enabled(self) -> bool:
        return self.embedder is not None

    async def search(self, tenant_id: str, query: str, top_k: int, filters: dict) -> list[PolicyChunk]:
        if self.embedder is None:
            return []
        vecs = await self.embedder.embed([query])
        if not vecs:
            return []
        vec = vecs[0]
        async with tenant_transaction(self.sf, tenant_id) as s:
            rows = (await s.execute(text(
                "SELECT clause_id,doc_id,version,text,clause_group_id,is_exception, "
                "       (embedding <=> CAST(:vec AS vector)) AS dist "
                "FROM policy_chunks WHERE tenant_id=:ten AND embedding IS NOT NULL "
                "  AND embedding_model = :model ORDER BY dist ASC LIMIT :k"),
                {"ten": tenant_id, "vec": "[" + ",".join(str(x) for x in vec) + "]",
                 "model": self.embedder.model, "k": top_k})).mappings().all()
        return [PolicyChunk(clause_id=r["clause_id"], doc_id=r["doc_id"], version=r["version"], text=r["text"],
                            clause_group_id=r["clause_group_id"], is_exception=r["is_exception"],
                            embedding_model=self.embedder.model, score=float(r["dist"] or 0.0)) for r in rows]


class NoopReranker:
    name = "noop"

    async def rerank(self, query: str, candidates: list[PolicyChunk], top_n: int) -> list[PolicyChunk]:
        return candidates[:top_n]


def apply_rerank_scores(candidates: list[PolicyChunk], results: list[dict], top_n: int) -> list[PolicyChunk]:
    """把 cross-encoder 的 relevance_score 写回 PolicyChunk.score。

    旧实现只按 index 重排、保留 RRF 分（≈0.03），导致「分数摊平」：下游无法用
    重排分数做低置信门控，报告也看不到真实区分度。
    """
    out: list[PolicyChunk] = []
    for d in results or []:
        try:
            i = int(d.get("index"))
        except (TypeError, ValueError):
            continue
        if not (0 <= i < len(candidates)):
            continue
        chunk = candidates[i]
        raw = d.get("relevance_score", chunk.score)
        try:
            score = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            score = chunk.score
        out.append(PolicyChunk(**{**chunk.__dict__, "score": score}))
    return out[:top_n]


class HttpReranker:
    """BGE Reranker（cross-encoder）HTTP 客户端。

    可用性优先：调用失败时降级为原序（不阻断检索）；但**降级必须可观测**——
    记录 warning 并累计 degraded_count，避免「重排静默失效 = 增益假 0」。
    """

    name = "http"

    def __init__(self, base_url: str, model: str, *, api_key: str | None = None, timeout_s: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.degraded_count = 0
        self.last_error: str | None = None

    async def rerank(self, query: str, candidates: list[PolicyChunk], top_n: int) -> list[PolicyChunk]:
        if not candidates:
            return []
        import httpx
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as c:
                r = await c.post(f"{self.base_url}/rerank", headers=headers, json={
                    "model": self.model, "query": query,
                    "documents": [ch.text for ch in candidates], "top_n": top_n})
                r.raise_for_status()
                data = r.json()
            return apply_rerank_scores(candidates, data.get("results", []), top_n)
        except Exception as exc:  # noqa: BLE001 - 重排失败降级为原序（可观测）
            self.degraded_count += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            logging.getLogger("app.rag.rerank").warning(
                "reranker degraded to RRF order (count=%d): %s", self.degraded_count, self.last_error)
            return candidates[:top_n]


class OpenAICompatEmbedder:
    """OpenAI 兼容 embeddings（可选）。model 用于索引/查询一致性。"""

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{self.base_url}/embeddings", headers={"Authorization": f"Bearer {self.api_key}"},
                             json={"model": self.model, "input": texts})
            r.raise_for_status()
            data = r.json()
        return [d["embedding"] for d in data["data"]]