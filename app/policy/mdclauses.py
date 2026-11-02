"""Markdown 政策条款切分（供入库脚本与内存政策库共用，避免逻辑重复）。

约定：标题形如 `## [CL-FRESH-A] 生鲜签收后24小时` 时，用方括号内 ID 作为 clause_id，
便于与 policy_rules.required_clause_ids 精确对齐；否则按 <prefix>-NNN 自动编号。
标题含「例外/exception」的节标记 is_exception=true；clause_group_id = 所在 H1 根标题 slug，
因此同一文档内的例外条款会与其基础条款同组，证据闭包时自动一并补齐。
"""
from __future__ import annotations

import re
import unicodedata

_EXPLICIT_ID = re.compile(r"^\[([A-Za-z0-9_\-]+)\]\s*(.*)$")


def slug(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).strip().lower()
    text = re.sub(r"[\s/]+", "-", text)
    text = re.sub(r"[^\w\u4e00-\u9fff-]", "", text)
    return text.strip("-")[:80] or "root"


def split_sections(text: str) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    stack: dict[int, str] = {}
    cur_title, cur_body = "", []
    for ln in text.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            if cur_title or any(s.strip() for s in cur_body):
                sections.append((cur_title, cur_body))
            level, title = len(m.group(1)), m.group(2).strip()
            stack[level] = title
            for k in [k for k in stack if k > level]:
                del stack[k]
            cur_title = " > ".join(stack[k] for k in sorted(stack))
            cur_body = []
        else:
            cur_body.append(ln)
    if cur_title or any(s.strip() for s in cur_body):
        sections.append((cur_title, cur_body))
    return sections


def pack(paras: list[str], chunk_size: int, overlap: int) -> list[str]:
    chunks, cur = [], ""
    for raw in paras:
        p = raw.strip()
        if not p:
            continue
        if not cur or len(cur) + len(p) + 2 <= chunk_size:
            cur = f"{cur}\n\n{p}" if cur else p
        else:
            chunks.append(cur)
            tail = cur[-overlap:] if overlap > 0 else ""
            cur = f"{tail}\n\n{p}" if tail else p
    if cur:
        chunks.append(cur)
    out: list[str] = []
    for c in chunks:
        if len(c) <= chunk_size * 2:
            out.append(c)
        else:
            step = max(1, chunk_size - overlap)
            out.extend(c[i:i + chunk_size] for i in range(0, len(c), step))
    return out


def build_chunks(text: str, doc_id: str, prefix: str, chunk_size: int, overlap: int) -> list[dict]:
    chunks: list[dict] = []
    n = 0
    for title, body in split_sections(text):
        leaf = title.split(" > ")[-1] if title else ""
        m = _EXPLICIT_ID.match(leaf)
        explicit = m.group(1) if m else None
        root = title.split(" > ")[0] if title else ""
        group = slug(root) if root else None
        is_exception = bool(re.search(r"例外|exception", title, re.I))
        packed = pack(body, chunk_size, overlap)
        for k, c in enumerate(packed):
            n += 1
            cid = (explicit if k == 0 else f"{explicit}#{k + 1}") if explicit else f"{prefix}-{n:03d}"
            chunks.append({"clause_id": cid, "text": c, "clause_group_id": group,
                           "is_exception": is_exception, "heading": title})
    if not chunks and text.strip():
        n += 1
        chunks.append({"clause_id": f"{prefix}-{n:03d}", "text": text.strip(),
                       "clause_group_id": None, "is_exception": False, "heading": ""})
    return chunks