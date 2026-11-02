# -*- coding: utf-8 -*-
"""Parent-Child 构建（LLD v4）：条级 Parent 为主、章/节 Parent 为辅；Child 由 HybridChunker 生成。

- Parent 单位：条（含款项/条件/内部例外）；章/节 Parent 仅作层级导航，不默认回填。
- Child：token-aware 切分（默认 HybridChunker，512 token；不可用时按句切分的确定性兜底）。
- 关联：Child.parent_id -> Parent.parent_id；parent_id 绑定具体政策版本（doc_id@version:logical_clause_id）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.rag.clause_parser import ClauseUnit

_SENT_SPLIT = re.compile(r"(?<=[。；;！？!?])")


@dataclass
class Parent:
    parent_id: str
    level: str                       # CHAPTER | CLAUSE
    logical_clause_id: str
    heading_path: list[str]
    display_text: str
    order_index: int
    is_exception: bool
    doc_id: str = ""
    version: str = ""
    token_count: int | None = None


@dataclass
class Child:
    clause_id: str
    parent_id: str
    logical_clause_id: str
    text: str
    token_count: int | None
    is_exception: bool
    order_index: int
    page: int | None = None


def make_parent_id(doc_id: str, version: str, logical_clause_id: str) -> str:
    return f"{doc_id}@{version}:{logical_clause_id}"


# --------------------------------------------------------------------------- tokenizer
def estimate_tokens(text: str) -> int:
    return max(1, round(len(text) / 1.6))


def get_token_counter(name: str | None = "BAAI/bge-m3"):
    """返回 (计数函数, 标签)。BGE-M3 tokenizer 不可用时退化为字符估算。"""
    if not name or name in ("none", "estimate"):
        return estimate_tokens, "estimate"
    try:
        from transformers import AutoTokenizer
        tok = AutoTokenizer.from_pretrained(name, trust_remote_code=False)

        def f(t: str) -> int:
            return len(tok.encode(t, add_special_tokens=False))

        return f, name
    except Exception:
        return estimate_tokens, "estimate"


# --------------------------------------------------------------------------- split
def split_text_by_tokens(text: str, max_tokens: int, count_fn, overlap_tokens: int = 0) -> list[str]:
    """按句子边界切分到 <= max_tokens；overlap 仅用于超长叙述段（默认 0）。"""
    sents = [s for s in _SENT_SPLIT.split(text) if s and s.strip()]
    if not sents:
        return [text] if text.strip() else []
    chunks: list[str] = []
    cur = ""
    for s in sents:
        cand = (cur + s) if cur else s
        if not cur or count_fn(cand) <= max_tokens:
            cur = cand
        else:
            chunks.append(cur)
            tail = ""
            if overlap_tokens > 0 and count_fn(cur) > overlap_tokens:
                # 近似：按 token 比例取尾部（overlap 默认 0，通常不触发）
                keep = int(len(cur) * overlap_tokens / max(1, count_fn(cur)))
                tail = cur[-keep:] if keep else ""
            cur = (tail + s) if tail else s
    if cur.strip():
        chunks.append(cur)
    return chunks


def build_parents(units: list[ClauseUnit], doc_id: str, version: str) -> list[Parent]:
    return [Parent(parent_id=make_parent_id(doc_id, version, u.logical_clause_id),
                   level=u.level if u.level in ("CHAPTER", "CLAUSE") else "CLAUSE",
                   logical_clause_id=u.logical_clause_id, heading_path=list(u.heading_path),
                   display_text=u.text, order_index=u.order_index,
                   is_exception=u.is_exception, doc_id=doc_id, version=version)
            for u in units]


def build_children_split(parents: list[Parent], max_tokens: int, count_fn,
                         overlap_tokens: int = 0) -> list[Child]:
    """确定性兜底：对每个条级/TABLE Parent 的原文按 token 切分。章级 Parent 不产 Child。"""
    children: list[Child] = []
    for p in parents:
        if p.level == "CHAPTER":
            continue
        parts = split_text_by_tokens(p.display_text, max_tokens, count_fn, overlap_tokens) or [p.display_text]
        for k, txt in enumerate(parts):
            cid = p.logical_clause_id if k == 0 else f"{p.logical_clause_id}#{k + 1}"
            children.append(Child(clause_id=cid, parent_id=p.parent_id,
                                  logical_clause_id=p.logical_clause_id, text=txt,
                                  token_count=count_fn(txt), is_exception=p.is_exception,
                                  order_index=p.order_index))
    return children


# --------------------------------------------------------------------------- HybridChunker
def build_children_hybrid(json_path: str | Path, parents: list[Parent],
                          max_tokens: int, count_fn, tokenizer_name: str = "BAAI/bge-m3",
                          merge_peers: bool = True, repeat_table_header: bool = True):
    """用 Docling HybridChunker 切分；返回 (children, unmatched_count)。不可用时抛异常由上层兜底。"""
    from docling_core.transforms.chunker import HybridChunker
    from docling_core.types.doc import DoclingDocument
    from transformers import AutoTokenizer

    doc = DoclingDocument.load_from_json(Path(json_path))
    tok = AutoTokenizer.from_pretrained(tokenizer_name, trust_remote_code=False)
    chunker = HybridChunker(tokenizer=tok, max_tokens=max_tokens,
                            merge_peers=merge_peers, repeat_table_header=repeat_table_header)

    by_key = {tuple(p.heading_path): p for p in parents}
    by_leaf = {}
    for p in parents:
        if p.heading_path:
            by_leaf.setdefault(p.heading_path[-1], p)
    ordered = sorted(parents, key=lambda p: p.order_index)

    def match(headings):
        h = tuple(headings or [])
        if h in by_key:
            return by_key[h]
        if h and h[-1] in by_leaf:
            return by_leaf[h[-1]]
        for p in ordered:
            if p.heading_path and h and p.heading_path[-1] == h[-1]:
                return p
        return None

    children: list[Child] = []
    unmatched = 0
    last_parent = ordered[0] if ordered else None
    per_parent_seq: dict[str, int] = {}
    for ch in chunker.chunk(dl_doc=doc):
        text = ch.text
        headings = list(getattr(getattr(ch, "meta", None), "headings", None) or [])
        p = match(headings)
        if p is None:
            unmatched += 1
            p = last_parent
        last_parent = p
        if p is None:
            continue
        k = per_parent_seq.get(p.logical_clause_id, 0)
        per_parent_seq[p.logical_clause_id] = k + 1
        cid = p.logical_clause_id if k == 0 else f"{p.logical_clause_id}#{k + 1}"
        page = None
        try:
            items = getattr(getattr(ch, "meta", None), "doc_items", None) or []
            if items and getattr(items[0], "prov", None):
                page = items[0].prov[0].page_no
        except Exception:
            page = None
        children.append(Child(clause_id=cid, parent_id=p.parent_id,
                              logical_clause_id=p.logical_clause_id, text=text,
                              token_count=count_fn(text), is_exception=p.is_exception,
                              order_index=p.order_index, page=page))
    return children, unmatched