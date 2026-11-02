"""内存检索后端（本地/测试）。生产用 app.rag.pg（Postgres/pgvector）。
词法用与 PgBM25 同一套分词与 BM25 公式，保证内存/DB 行为一致。"""
from __future__ import annotations

import math

from app.rag.retrieval import PolicyChunk
from app.rag.tokenize import token_counts, tokenize


class InMemoryClauseStore:
    """条款库（内存版）。支持按 clause_id 或 logical_clause_id 精确取条款，
    与 DB 版（app.rag.pg）保持一致：规则既可引用 child chunk_id，也可引用逻辑条款 ID。"""

    def __init__(self, chunks: list[PolicyChunk] | None = None) -> None:
        self._by_id: dict[str, PolicyChunk] = {}
        self._by_logical: dict[str, str] = {}
        for c in (chunks or []):
            self.add(c)

    def _index_logical(self, chunk: PolicyChunk) -> None:
        logical = chunk.logical_clause_id or chunk.clause_id
        self._by_logical.setdefault(logical, chunk.clause_id)

    def add(self, chunk: PolicyChunk) -> None:
        self._by_id[chunk.clause_id] = chunk
        self._index_logical(chunk)

    def resolve(self, clause_id: str) -> PolicyChunk | None:
        hit = self._by_id.get(clause_id)
        if hit is not None:
            return hit
        mapped = self._by_logical.get(clause_id)
        return self._by_id.get(mapped) if mapped else None

    async def fetch_many(self, tenant_id: str, clause_ids: list[str], as_of: str) -> list[PolicyChunk]:
        out: list[PolicyChunk] = []
        for cid in clause_ids:
            chunk = self.resolve(cid)
            if chunk is not None:
                out.append(chunk)
        return out

    async def fetch_group(self, tenant_id: str, clause_group_id: str, as_of: str) -> list[PolicyChunk]:
        return [c for c in self._by_id.values() if c.clause_group_id == clause_group_id and c.is_exception]

    def all(self) -> list[PolicyChunk]:
        return list(self._by_id.values())


class InMemoryLexical:
    """内存版真实 BM25（与 PgBM25 同一公式：idf/tf 饱和/长度归一化）。"""

    enabled = True
    name = "memory-bm25"

    def __init__(self, store: InMemoryClauseStore, *, k1: float = 1.2, b: float = 0.75) -> None:
        self.store = store
        self.k1 = float(k1)
        self.b = float(b)
        self._docs = [(c, token_counts(c.text)) for c in store.all()]
        lens = [max(1, sum(tc.values())) for _, tc in self._docs]
        self._avgdl = (sum(lens) / len(lens)) if lens else 1.0

    async def search(self, tenant_id: str, query: str, top_k: int, filters: dict) -> list[PolicyChunk]:
        terms = tokenize(query)
        if not terms or not self._docs:
            return []
        n = len(self._docs)
        df = {t: sum(1 for _, tc in self._docs if t in tc) for t in terms}
        scored: list[tuple[float, PolicyChunk]] = []
        for c, tc in self._docs:
            dl = max(1, sum(tc.values()))
            score = 0.0
            for t in terms:
                tf = tc.get(t, 0)
                if not tf:
                    continue
                idf = math.log(1 + max(n - df[t] + 0.5, 0) / (df[t] + 0.5))
                score += idf * (tf * (self.k1 + 1)) / (tf + self.k1 * (1 - self.b + self.b * dl / self._avgdl))
            if score > 0:
                scored.append((score, c))
        scored.sort(key=lambda kv: kv[0], reverse=True)
        return [PolicyChunk(**{**c.__dict__, "score": float(s)}) for s, c in scored[:top_k]]


class InMemoryDense:
    """内存后端不提供向量检索（无 embedding 服务）；诊断据此标记 dense 关闭。"""

    enabled = False
    name = "memory-none"
    score_is_distance = True

    async def search(self, tenant_id: str, query: str, top_k: int, filters: dict) -> list[PolicyChunk]:
        return []


class NoopReranker:
    name = "noop"

    async def rerank(self, query: str, candidates: list[PolicyChunk], top_n: int) -> list[PolicyChunk]:
        return candidates[:top_n]