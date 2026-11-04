"""政策库加载（真实规则与条款的单一事实源）。

事实源（按优先级）：
  - policy/rules.json           -> 规则集 + 文档清单（documents）+ 非裁决清单（catalog）
  - policy/clauses_jd.json      -> 真实语料条款（child chunk，含 logical_clause_id/parent_id）
  - docs/policy/*.md            -> 旧版按 [CLAUSE-ID] 现切的条款（回退路径）

用途：让内存后端（RAG_BACKEND=memory / 无 DB）与 DB 后端读到**同一批**规则与条款；
文件缺失时回退到 app.policy.seed 内置示例，保证服务仍可运行。
入库（DB/pg 后端）使用同一份 rules.json / clauses_jd.json，由 scripts/ingest_policy.py 完成。
"""
from __future__ import annotations

import json
import pathlib
from functools import lru_cache
from typing import Any

from app.policy.dsl import Condition, Rule
from app.policy.mdclauses import build_chunks
from app.rag.retrieval import PolicyChunk

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
LIBRARY_PATH = REPO_ROOT / "policy" / "rules.json"
CLAUSES_PATH = REPO_ROOT / "policy" / "clauses_jd.json"


@lru_cache
def _raw() -> dict[str, Any]:
    if not LIBRARY_PATH.is_file():
        return {}
    return json.loads(LIBRARY_PATH.read_text(encoding="utf-8"))


@lru_cache
def _corpus() -> dict[str, Any]:
    if not CLAUSES_PATH.is_file():
        return {}
    return json.loads(CLAUSES_PATH.read_text(encoding="utf-8"))


def library_version() -> str:
    return _raw().get("version", "2026.10")


def library_tenant() -> str:
    return str(_raw().get("tenant") or "tenant_demo")


def documents() -> list[dict[str, Any]]:
    docs = list(_raw().get("documents", []))
    if docs:
        return docs
    return list(_corpus().get("documents", []))


def load_rules() -> list[Rule]:
    """从 rules.json 载入规则；无文件时回退到内置示例规则。"""
    items = _raw().get("rules", [])
    if not items:
        from app.policy.seed import default_rules
        return default_rules()
    out: list[Rule] = []
    for r in items:
        conds = [Condition(c["fact"], c["op"], c.get("value")) for c in r.get("when", [])]
        out.append(Rule(
            rule_id=r["rule_id"], version=r.get("version", library_version()),
            priority=int(r.get("priority", 0)), when=conds,
            decision=r["decision"], reason_code=r["reason_code"],
            required_clause_ids=list(r.get("required_clause_ids", [])),
            amount_cap_minor=r.get("amount_cap_minor"),
            cap_type=r.get("amount_cap_type", "AUTO_EXEC_THRESHOLD"),
            allowed_actions=tuple(r.get("allowed_actions") or ()),
            stage=(int(r["stage"]) if r.get("stage") is not None else None),
            scope=dict(r.get("scope", {})), status=r.get("status", "PUBLISHED"),
            tenant_scope=str(_raw().get("tenant") or "tenant_demo"),
            effective_from=r.get("effective_from"), effective_to=r.get("effective_to"),
        ))
    return out


def catalog() -> list[dict[str, Any]]:
    """非消费者裁决清单（服务资格类 / 商家合规类）。不参与 PolicyEngine 求值，
    仅用于覆盖度审计与文档，避免把需要额外事实或不属于消费者裁决的条目塞进裁决链。"""
    return list(_raw().get("catalog", []))


def load_clauses(chunk_size: int = 900, overlap: int = 150) -> list[PolicyChunk]:
    """载入条款：优先真实语料 clauses_jd.json；否则按文档清单现切 md；最后回退内置示例。"""
    corpus = _corpus().get("clauses", [])
    if corpus:
        out: list[PolicyChunk] = []
        seen: set[str] = set()
        for c in corpus:
            cid = c["clause_id"]
            if cid in seen:
                continue
            seen.add(cid)
            out.append(PolicyChunk(
                clause_id=cid, doc_id=c["doc_id"], version=c.get("version", library_version()),
                text=c["text"], clause_group_id=c.get("clause_group_id"),
                is_exception=bool(c.get("is_exception")), logical_clause_id=c.get("logical_clause_id"),
            ))
        return out

    docs = documents()
    if not docs:
        from app.policy.seed import default_clauses
        return default_clauses()
    chunks: list[PolicyChunk] = []
    seen_ids: set[str] = set()
    for d in docs:
        path = REPO_ROOT / str(d.get("file", ""))
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for c in build_chunks(text, d["doc_id"], d["doc_id"], chunk_size, overlap):
            cid = c["clause_id"]
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            chunks.append(PolicyChunk(clause_id=cid, doc_id=d["doc_id"],
                                      version=d.get("version", library_version()), text=c["text"],
                                      clause_group_id=c["clause_group_id"], is_exception=c["is_exception"]))
    if not chunks:
        from app.policy.seed import default_clauses
        return default_clauses()
    return chunks
