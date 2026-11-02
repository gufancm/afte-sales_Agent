"""RAG：检索 + RRF + 重排（§5）；证据闭包与校验见 app/rag/evidence.py。

检索链路（工业界标准）：首轮 BM25 + 向量 Dense -> 置信度判定 -> 仅低置信时查询变换
（多查询改写 + HyDE）-> 多路 RRF 融合 -> cross-encoder 重排。
Top-K 仅用于**发现**；授权证据由 required_clause_ids 精确补齐（§5.10）。
全异步接口。
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

from app.domain.enums import EvidenceStatus
from app.domain.schemas import EvidenceBundle, EvidenceItem

logger = logging.getLogger("app.rag.retrieval")

# 明确标识符/精确约束：这类 query 不需要 LLM 改写，改写反而可能引入噪声。
_EXACT_ID_RE = re.compile(
    r"(?i)(?:cl[-_][a-z0-9]{4,}|(?:order|ord|o|wb|waybill|sku)[-_]?\d{3,}|"
    r"(?:订单|运单|工单|退款单)\s*[:：]?\s*[a-z]?\d{3,}|\b\d{10,}\b)"
)
# 指代/省略/追问：需要结合上下文或补全语义。
_DEICTIC_RE = re.compile(
    r"(这个|那个|它|刚才|刚刚|上面|前面|继续|还有|这种|该笔|这笔|那笔|这单|那单|"
    r"该订单|该商品|这件|那件)"
)
# 已含明确业务规则词的 query：词法/向量通常已足够，不因“短”而触发。
_RULE_TERM_RE = re.compile(
    r"(七天无理由|7天无理由|无理由|运费险|闪电退款|价保|预售|生鲜|图书|服饰|"
    r"退货退款|退货|退款|换货|补发|拦截|赔付|发货|签收|质量问题|仅退款)"
)


@dataclass(frozen=True)
class PolicyChunk:
    clause_id: str
    doc_id: str
    version: str
    text: str
    clause_group_id: str | None = None
    is_exception: bool = False
    embedding_model: str | None = None
    score: float | None = None
    logical_clause_id: str | None = None


class LexicalBackend(Protocol):
    async def search(self, tenant_id: str, query: str, top_k: int, filters: dict) -> list[PolicyChunk]: ...


class DenseBackend(Protocol):
    async def search(self, tenant_id: str, query: str, top_k: int, filters: dict) -> list[PolicyChunk]: ...


class Reranker(Protocol):
    async def rerank(self, query: str, candidates: list[PolicyChunk], top_n: int) -> list[PolicyChunk]: ...


class ClauseStore(Protocol):
    async def fetch_many(self, tenant_id: str, clause_ids: list[str], as_of: str) -> list[PolicyChunk]: ...
    async def fetch_group(self, tenant_id: str, clause_group_id: str, as_of: str) -> list[PolicyChunk]: ...


def rrf_fuse(rankings: list[list[PolicyChunk]], k: int = 60) -> list[PolicyChunk]:
    """Reciprocal Rank Fusion（§5.8）。k 为平滑常数，不是候选数量。"""
    scores: dict[str, float] = {}
    by_id: dict[str, PolicyChunk] = {}
    for ranking in rankings:
        for rank, chunk in enumerate(ranking, start=1):
            scores[chunk.clause_id] = scores.get(chunk.clause_id, 0.0) + 1.0 / (k + rank)
            by_id.setdefault(chunk.clause_id, chunk)
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [PolicyChunk(**{**by_id[cid].__dict__, "score": s}) for cid, s in ordered]


def _dense_similarity(chunk: PolicyChunk, dense: Any) -> float:
    """把 backend 的 score 统一成“越大越相似”。PgDense 返回 cosine distance。"""
    score = float(chunk.score or 0.0)
    if getattr(dense, "score_is_distance", True):
        return 1.0 - score
    return score


def _required_coverage(required_clause_ids: list[str] | None, chunks: list[PolicyChunk]) -> float:
    """必备条款在“发现候选”中的覆盖率。仅用于诊断/触发，不是授权依据。"""
    required = list(required_clause_ids or [])
    if not required:
        return 1.0
    seen: set[str] = set()
    for c in chunks:
        seen.add(c.clause_id)
        if c.logical_clause_id:
            seen.add(c.logical_clause_id)
    return sum(1 for cid in required if cid in seen) / len(required)


def _history_hint(history: list[str] | None, max_messages: int, max_chars: int) -> str:
    """从最近几轮用户话术提取上下文锚点（去标识符、去空白），用于零成本指代消解。"""
    if not history or max_messages <= 0 or max_chars <= 0:
        return ""
    picked: list[str] = []
    for item in list(history)[-max_messages:]:
        text = " ".join(str(item or "").split())
        if text:
            picked.append(text)
    if not picked:
        return ""
    hint = _EXACT_ID_RE.sub(" ", " ".join(picked))
    hint = " ".join(hint.split())
    return hint[:max_chars].strip()


def _expand_with_context(query: str, hint: str, max_chars: int = 240) -> str | None:
    """把上下文锚点拼进检索 query：解决“这个/那个/刚才”的指代，不花一次 LLM。"""
    q = (query or "").strip()
    h = (hint or "").strip()
    if not q or not h or h in q:
        return None
    return (q + " " + h)[: len(q) + 1 + max_chars]


@dataclass
class Retriever:
    lexical: LexicalBackend
    dense: DenseBackend
    reranker: Reranker
    clauses: ClauseStore
    bm25_top_k: int = 20
    dense_top_k: int = 20
    rrf_k: int = 60
    rrf_top_n: int = 15
    rerank_top_n: int = 5
    transformer: Any = None   # 可选：app.rag.query_transform.LLMQueryTransformer
    transform_policy: str = "adaptive"  # off | always | adaptive
    adaptive_min_dense_sim: float = 0.60
    adaptive_min_margin: float = 0.015
    adaptive_short_query_chars: int = 12
    adaptive_min_candidates: int = 5
    adaptive_soft_signals_required: int = 2
    adaptive_use_required_coverage: bool = True
    adaptive_min_required_coverage: float = 0.5
    adaptive_context_chars: int = 240
    adaptive_context_messages: int = 4
    transform_timeout_s: float = 2.5

    async def discover(self, *, tenant_id: str, query: str, filters: dict) -> list[PolicyChunk]:
        chunks, _ = await self.discover_ex(tenant_id=tenant_id, query=query, filters=filters)
        return chunks

    async def _variants(self, query: str, *, context: str | None = None) -> tuple[list[str], dict]:
        """调用 transformer，带总超时；任何异常都降级为原始 query。"""
        if self.transformer is None:
            return [query], self._empty_transform_diag()

        async def _call():
            if context:
                try:
                    return await self.transformer.variants(query, context=context)
                except TypeError:  # 兼容不支持上下文参数的实现
                    return await self.transformer.variants(query)
            return await self.transformer.variants(query)

        try:
            if self.transform_timeout_s and self.transform_timeout_s > 0:
                variants, diag = await asyncio.wait_for(_call(), timeout=self.transform_timeout_s)
            else:
                variants, diag = await _call()
            if not variants:
                variants = [query]
            return variants, diag
        except asyncio.TimeoutError:
            return [query], {**self._empty_transform_diag(), "errors": ["timeout"]}
        except Exception as exc:  # noqa: BLE001 - 变换失败不阻断
            return [query], {**self._empty_transform_diag(), "errors": [str(exc)[:120]]}
    def _empty_transform_diag(self) -> dict:
        return {"multi_query": [], "hyde": None, "errors": [], "rejected": [],
                "enabled": self.transformer is not None, "llm_calls": [], "cache_hit": False}

    def _decide_transform(self, *, query: str, dense_hits: list[PolicyChunk],
                          fused: list[PolicyChunk], required_clause_ids: list[str] | None,
                          history: list[str] | None = None
                          ) -> tuple[bool, list[str], dict, bool]:
        """工业界 adaptive 门控：先花一次便宜的检索，再决定是否花 LLM 钱。

        硬信号（单条成立即触发）：无候选 / 候选不足 / Dense Top1 过低 / 必备条款覆盖不足。
        软信号（需同时满足 soft_signals_required 条）：短查询 / Top1-Top2 间隔小 / 含指代。
        指代若无可用会话上下文，改写无从解析指代对象，宁可跳过也不白花一次调用。

        返回 (triggered, reasons, info, llm_needed)。
        """
        q = (query or "").strip()
        hint = _history_hint(history, self.adaptive_context_messages, self.adaptive_context_chars)
        info: dict[str, Any] = {
            "bm25_hits": len(fused), "candidate_count": len(fused),
            "required_coverage": round(_required_coverage(required_clause_ids, fused), 4),
            "has_context": bool(hint),
        }
        if not q:
            return False, ["empty_query"], info, False
        if self.transform_policy == "off":
            return False, ["policy_off"], info, False
        if self.transformer is None:
            return False, ["transformer_unavailable"], info, False
        if self.transform_policy == "always":
            return True, ["policy_always"], info, True
        if _EXACT_ID_RE.search(q):
            info["skip_reason"] = "exact_identifier"
            return False, ["exact_identifier"], info, False

        hard: list[str] = []
        soft: list[str] = []
        if not fused:
            hard.append("no_candidates")
        if len(fused) < self.adaptive_min_candidates:
            hard.append(f"few_candidates:{len(fused)}")

        cov = _required_coverage(required_clause_ids, fused)
        if (required_clause_ids and self.adaptive_use_required_coverage
                and cov < self.adaptive_min_required_coverage):
            hard.append(f"required_coverage:{cov:.2f}")

        dense_enabled = bool(getattr(self.dense, "enabled", True))
        if dense_enabled and dense_hits:
            sims = sorted((_dense_similarity(c, self.dense) for c in dense_hits), reverse=True)
            top1 = sims[0]
            top2 = sims[1] if len(sims) > 1 else top1
            margin = top1 - top2
            info.update({"dense_top1_sim": round(top1, 4), "dense_margin": round(margin, 4)})
            if top1 < self.adaptive_min_dense_sim:
                hard.append(f"low_dense_sim:{top1:.3f}")
            if margin < self.adaptive_min_margin and not _RULE_TERM_RE.search(q):
                soft.append(f"low_margin:{margin:.3f}")

        if _DEICTIC_RE.search(q):
            if hint:
                soft.append("deictic_reference")
            else:
                info["deictic_without_context"] = True
        if len(q) <= self.adaptive_short_query_chars and not _RULE_TERM_RE.search(q):
            soft.append(f"short_query:{len(q)}")

        triggered = bool(hard) or len(soft) >= self.adaptive_soft_signals_required
        info.update({"hard_signals": hard, "soft_signals": soft,
                     "soft_signals_required": self.adaptive_soft_signals_required})
        if not triggered:
            return False, ["high_confidence"], info, False
        # 只有硬信号才值得花钱；纯指代类软信号且有上下文锚点时，零成本扩展已足够。
        llm_needed = bool(hard) or not hint or "deictic_reference" not in soft
        return True, hard + soft, info, llm_needed

    async def discover_ex(self, *, tenant_id: str, query: str, filters: dict,
                          required_clause_ids: list[str] | None = None,
                          history: list[str] | None = None) -> tuple[list[PolicyChunk], dict]:
        """带诊断的发现流程：首轮检索 -> 自适应判定 -> 上下文扩展/LLM 改写 -> RRF -> 重排。"""
        variants: list[str] = [query]
        transform_diag = self._empty_transform_diag()
        triggered = False
        reasons: list[str] = []
        llm_used = False
        first_pass: dict[str, Any] = {}

        bm0 = await self.lexical.search(tenant_id, query, self.bm25_top_k, filters)
        dn0 = await self.dense.search(tenant_id, query, self.dense_top_k, filters)
        rankings: list[list[PolicyChunk]] = [x for x in (bm0, dn0) if x]
        bm25_hits = len(bm0)
        dense_hits = len(dn0)

        first_fused = rrf_fuse(rankings, self.rrf_k)[: self.rrf_top_n] if rankings else []
        triggered, reasons, first_pass, llm_needed = self._decide_transform(
            query=query, dense_hits=dn0, fused=first_fused,
            required_clause_ids=required_clause_ids, history=history)

        if triggered:
            hint = _history_hint(history, self.adaptive_context_messages, self.adaptive_context_chars)
            free = _expand_with_context(query, hint, self.adaptive_context_chars) if hint else None
            if free and free not in variants:
                variants.append(free)
            if llm_needed:
                llm_variants, transform_diag = await self._variants(query, context=hint or None)
                llm_used = len(llm_variants) > 1
                for v in llm_variants[1:]:
                    if v not in variants:
                        variants.append(v)
            for v in variants[1:]:
                bm = await self.lexical.search(tenant_id, v, self.bm25_top_k, filters)
                dn = await self.dense.search(tenant_id, v, self.dense_top_k, filters)
                bm25_hits += len(bm)
                dense_hits += len(dn)
                if bm:
                    rankings.append(bm)
                if dn:
                    rankings.append(dn)

        fused = rrf_fuse(rankings, self.rrf_k)[: self.rrf_top_n] if rankings else []
        reranked = await self.reranker.rerank(query, fused, self.rerank_top_n)
        rerank_scored = getattr(self.reranker, "name", "noop") != "noop"
        rerank_top1 = reranked[0].score if reranked else None
        rerank_margin = None
        if len(reranked) > 1 and reranked[0].score is not None and reranked[1].score is not None:
            rerank_margin = round(float(reranked[0].score) - float(reranked[1].score), 4)

        transform_diag = dict(transform_diag)
        transform_diag.update({"policy": self.transform_policy, "triggered": triggered,
                               "reasons": reasons, "effective": len(variants) > 1,
                               "llm_used": llm_used, "llm_calls": transform_diag.get("llm_calls") or []})
        diagnostics = {
            "variants": variants,
            "variant_count": len(variants),
            "bm25_hits": bm25_hits,
            "dense_hits": dense_hits,
            "fused_count": len(fused),
            "lexical": getattr(self.lexical, "name", type(self.lexical).__name__),
            "dense_enabled": bool(getattr(self.dense, "enabled", True)),
            "reranker": getattr(self.reranker, "name", type(self.reranker).__name__),
            "rerank_scored": rerank_scored,
            "rerank_top1_score": rerank_top1,
            "rerank_score_margin": rerank_margin,
            "transform_policy": self.transform_policy,
            "transform_triggered": triggered,
            "transform_reasons": reasons,
            "query_transform": transform_diag,
            "first_pass": {**first_pass, "fused_count": len(first_fused),
                           "bm25_hits": len(bm0), "dense_hits": len(dn0)},
            "post_transform": {"fused_count": len(fused),
                               "required_coverage": round(_required_coverage(required_clause_ids, fused), 4)},
            # 检索期 LLM 调用审计（供 policy_evidence 节点落 llm_calls）
            "llm_calls": list(transform_diag.get("llm_calls") or []),
        }
        logger.info("rag.discover policy=%s triggered=%s reasons=%s variants=%d fused=%d",
                    self.transform_policy, triggered, ",".join(reasons), len(variants), len(fused))
        return reranked, diagnostics

    async def build_evidence(
        self, *, tenant_id: str, query: str, filters: dict,
        required_clause_ids: list[str], as_of: str, policy_version: str | None,
    ) -> EvidenceBundle:
        """兼容入口：实现已收敛到 EvidenceService（一次检索 + 确定性校验）。"""
        from app.rag.evidence import EvidenceService
        return await EvidenceService(self).build_and_validate(
            tenant_id=tenant_id,
            policy_decision={"required_clause_ids": list(required_clause_ids), "as_of": as_of,
                             "policy_version": policy_version},
            query=query, filters=filters,
        )


__all__ = ["PolicyChunk", "Retriever", "rrf_fuse", "EvidenceStatus", "EvidenceBundle", "EvidenceItem"]