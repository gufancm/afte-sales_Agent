# -*- coding: utf-8 -*-
"""政策证据服务（LLD §5.10）：一次检索 + 确定性校验。

合并背景：原 PolicyRetrieval 与 EvidenceGate 是两次独立检索（discover 被调用两次），
既重复耗时，也无法保证两次用的是同一个版本快照。现在由本服务一次性完成
「发现 -> 精确条款补齐 -> 例外闭包 -> 校验」，并输出唯一 evidence_bundle。

授权事实永远来自 required_clause_ids 的精确补齐，Top-K 只用于发现与诊断。
以下校验是确定性函数，**不得由 LLM 替代**：
  - 覆盖：必备条款必须全部取到，缺一即 INSUFFICIENT；
  - 版本：条款版本与政策版本不一致即 STALE；
  - 冲突：同一条款出现两个版本即 CONFLICT；
  - 例外：例外条款必须能归属到某个条款组，否则无法保证例外闭包。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.domain.enums import EvidenceStatus
from app.domain.schemas import EvidenceBundle, EvidenceItem
from app.rag.retrieval import PolicyChunk, Retriever


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class EvidenceValidation:
    status: EvidenceStatus
    missing_clause_ids: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def validate_coverage(*, required_clause_ids: list[str], fetched: dict[str, PolicyChunk],
                      policy_version: str | None) -> EvidenceValidation:
    problems: list[str] = []
    missing = [cid for cid in required_clause_ids if cid not in fetched]
    if missing:
        problems.append("MISSING_REQUIRED_CLAUSES:" + ",".join(missing))
    if policy_version:
        drifted = sorted({c.version for c in fetched.values() if c.version and c.version != policy_version})
        if drifted:
            problems.append("CLAUSE_VERSION_DRIFT:" + ",".join(drifted))
    orphans = sorted(c.clause_id for c in fetched.values() if c.is_exception and not c.clause_group_id)
    if orphans:
        problems.append("EXCEPTION_WITHOUT_GROUP:" + ",".join(orphans))

    if missing:
        status = EvidenceStatus.INSUFFICIENT
    elif any(p.startswith("CLAUSE_VERSION_DRIFT") for p in problems):
        status = EvidenceStatus.STALE
    elif orphans:
        status = EvidenceStatus.CONFLICT
    else:
        status = EvidenceStatus.OK
    return EvidenceValidation(status=status, missing_clause_ids=missing, problems=problems)


@dataclass
class EvidenceService:
    retriever: Retriever

    async def build_and_validate(self, *, tenant_id: str, policy_decision: dict,
                                 query: str, filters: dict | None = None,
                                 history: list[str] | None = None) -> EvidenceBundle:
        filters = filters or {}
        required = list(policy_decision.get("required_clause_ids") or [])
        as_of = policy_decision.get("as_of") or _now()
        policy_version = policy_decision.get("policy_version")

        # 1) 发现（唯一一次 Top-K 检索，带诊断：查询变换/多路召回/重排）
        discover_ex = getattr(self.retriever, "discover_ex", None)
        if discover_ex is not None:
            discovered, retrieval_diag = await discover_ex(
                tenant_id=tenant_id, query=query or "售后", filters=filters,
                required_clause_ids=required, history=history)
        else:
            discovered = await self.retriever.discover(tenant_id=tenant_id, query=query or "售后", filters=filters)
            retrieval_diag = {}

        # 2) 精确补齐必备条款 + 例外闭包（版本以 as_of 为准）
        fetched: dict[str, PolicyChunk] = {}
        duplicates: list[str] = []
        for c in await self.retriever.clauses.fetch_many(tenant_id, required, as_of):
            if c.clause_id in fetched and fetched[c.clause_id].version != c.version:
                duplicates.append(c.clause_id)
            fetched[c.clause_id] = c
        groups = {c.clause_group_id for c in fetched.values() if c.clause_group_id}
        for gid in groups:
            for c in await self.retriever.clauses.fetch_group(tenant_id, gid, as_of):
                fetched.setdefault(c.clause_id, c)

        # 3) 校验
        validation = validate_coverage(required_clause_ids=required, fetched=fetched,
                                      policy_version=policy_version)
        status = validation.status
        problems = list(validation.problems)
        if duplicates:
            problems.append("DUPLICATE_CLAUSE_VERSIONS:" + ",".join(sorted(set(duplicates))))
            if status == EvidenceStatus.OK:
                status = EvidenceStatus.CONFLICT

        items = [EvidenceItem(clause_id=c.clause_id, clause_group_id=c.clause_group_id, doc_id=c.doc_id,
                              version=c.version, text=c.text, score=c.score, is_exception=c.is_exception)
                 for c in fetched.values()]
        return EvidenceBundle(
            status=status, items=items, required_clause_ids=required,
            missing_clause_ids=validation.missing_clause_ids, policy_version=policy_version, as_of=as_of,
            diagnostics={"discovered": [c.clause_id for c in discovered], "discovered_count": len(discovered),
                         "required_count": len(required), "problems": problems,
                         "retrieval": retrieval_diag},
        )