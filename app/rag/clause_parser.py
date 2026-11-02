# -*- coding: utf-8 -*-
"""条款解析器：把 Docling 产出的 Markdown 恢复成 章/条 结构，并赋 logical_clause_id。

背景（LLD v3.5 §5.4.1）：Docling / HybridChunker 只做版面与 token 切分，不会产生符合
售后业务的 clause_id / clause_group_id —— 必须由领域解析器生成并人工复核（见 clause_overrides.json）。

输出「条款单元」：
  - CHAPTER  章/节（第X章）  -> 上层 Parent，同时作为 clause_group_id
  - CLAUSE   条（第X条）      -> 条级 Parent（主要证据单位）
  - TABLE    表（表 N：…）    -> 归属其所在条/章
兼容三种编号体系：第X条 / 1.【…】+1.1 / 一、二、；识别「行首」条号（无论是否被 Docling 标为标题），
清洗 HTML 实体与注释，去重（重复条款号 + 目录式重复标题）；均可由 overrides 人工覆盖。
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from pathlib import Path

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_CHAPTER = re.compile(r"^第([一二三四五六七八九十百零〇]+)章")
_ARTICLE = re.compile(r"^第([一二三四五六七八九十百零〇]+)条")
_TABLE = re.compile(r"^表\s*\d+\s*[:：]")
_NUM_ITEM = re.compile(r"^\d+\.\s*[【\[]")
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_EXC = re.compile(r"例外|除外|不适用|不支持|不包含")

_CN = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4,
       "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn_to_int(s: str):
    s = (s or "").strip()
    if not s:
        return None
    if "百" in s:
        left, _, right = s.partition("百")
        return (cn_to_int(left) or 1) * 100 + (cn_to_int(right) or 0)
    if "十" in s:
        left, _, right = s.partition("十")
        return (cn_to_int(left) if left else 1) * 10 + (cn_to_int(right) or 0)
    n = 0
    for ch in s:
        if ch not in _CN:
            return None
        n = n * 10 + _CN[ch]
    return n


def clean_text(s: str) -> str:
    return html.unescape(_COMMENT.sub(" ", s or "")).strip()


@dataclass
class ClauseUnit:
    logical_clause_id: str
    level: str
    heading_path: list[str]
    text: str
    order_index: int
    is_exception: bool = False
    title: str = ""


def _is_exc(*parts: str) -> bool:
    return any(_EXC.search(p or "") for p in parts)


def _trunc(t: str, n: int = 40) -> str:
    return t if len(t) <= n else t[:n] + "…"


def _drop_toc_duplicates(units: list["ClauseUnit"]) -> list["ClauseUnit"]:
    """删除「正文仅为标题本身」的重复单元（目录式重复标题），前提是存在同名带正文单元。"""
    groups: dict[tuple, list[ClauseUnit]] = {}
    for u in units:
        groups.setdefault((u.level, u.title), []).append(u)
    drop: set[int] = set()
    for grp in groups.values():
        if len(grp) > 1 and any(u.text.strip() != u.title.strip() for u in grp):
            for u in grp:
                if u.text.strip() == u.title.strip():
                    drop.add(id(u))
    return [u for u in units if id(u) not in drop]


def parse_document(md_text: str, *, doc_id: str, rule_id: str, version: str = "",
                   overrides: dict | None = None) -> list[ClauseUnit]:
    overrides = overrides or {}
    lines = [clean_text(l) for l in md_text.splitlines()]
    has_article = any(_ARTICLE.match(l) for l in lines)

    units: list[ClauseUnit] = []
    used_ids: set[str] = set()
    chapter_title = ""
    last_article_title = ""
    seq_art = seq_tbl = seq_other = 0
    order = 0
    cur: dict | None = None

    def uniq(cid: str) -> str:
        c, k = cid, 2
        while c in used_ids:
            c = f"{cid}-{k}"
            k += 1
        used_ids.add(c)
        return c

    def open_unit(level: str, cid: str, title: str) -> dict:
        nonlocal order
        order += 1
        leaf = _trunc(title)
        if level == "CLAUSE":
            hpath = ([chapter_title] if chapter_title else []) + [leaf]
        elif level == "TABLE":
            hpath = ([chapter_title] if chapter_title else []) + \
                    ([last_article_title] if last_article_title else []) + [leaf]
        else:
            hpath = [leaf]
        return {"cid": uniq(cid), "level": level, "title": title, "hpath": hpath, "lines": [], "order": order}

    def close() -> None:
        nonlocal cur
        if cur is None:
            return
        body = "\n".join(cur["lines"]).strip()
        text = ((cur["title"] + "\n" + body) if cur["title"] else body).strip()
        if text:
            units.append(ClauseUnit(logical_clause_id=cur["cid"], level=cur["level"],
                                    heading_path=cur["hpath"], text=text, order_index=cur["order"],
                                    is_exception=_is_exc(cur["title"], text[:80]), title=cur["title"]))
        cur = None

    for raw in lines:
        ln = raw.strip()
        if not ln:
            if cur is not None:
                cur["lines"].append("")
            continue
        m = _HEADING.match(raw.strip())
        title = m.group(2).strip() if m else ln
        is_head = bool(m)

        if _TABLE.match(title) and is_head:
            close()
            seq_tbl += 1
            cur = open_unit("TABLE", f"CL-JD{rule_id}-T{seq_tbl}", title)
            continue
        if _CHAPTER.match(title) and len(title) <= 30:
            if title == chapter_title:
                continue
            close()
            chapter_title = title
            last_article_title = ""
            cnum = cn_to_int(_CHAPTER.match(title).group(1))
            seq_other += 1
            cur = open_unit("CHAPTER", f"CL-JD{rule_id}-CH{cnum}" if cnum else f"CL-JD{rule_id}-CH{seq_other}", title)
            continue
        if _ARTICLE.match(title):
            close()
            seq_art += 1
            anum = cn_to_int(_ARTICLE.match(title).group(1)) or seq_art
            last_article_title = title
            cur = open_unit("CLAUSE", f"CL-JD{rule_id}-A{anum}", title)
            continue
        if is_head:
            close()
            seq_other += 1
            cur = open_unit("CLAUSE", f"CL-JD{rule_id}-S{seq_other}", title)
            continue
        if not has_article and _NUM_ITEM.match(ln):
            close()
            seq_other += 1
            cur = open_unit("CLAUSE", f"CL-JD{rule_id}-S{seq_other}", ln)
            continue
        if cur is None:
            cur = open_unit("CLAUSE", f"CL-JD{rule_id}-PRE", "(前言)")
        cur["lines"].append(ln)
    close()

    units = _drop_toc_duplicates(units)

    out: list[ClauseUnit] = []
    for u in units:
        ov = overrides.get(u.logical_clause_id) or {}
        if ov.get("drop"):
            continue
        if "is_exception" in ov:
            u.is_exception = bool(ov["is_exception"])
        if ov.get("logical_clause_id"):
            u.logical_clause_id = ov["logical_clause_id"]
        out.append(u)
    return out


DEFAULT_OVERRIDES = Path(__file__).resolve().parents[2] / "policy" / "clause_overrides.json"


def load_overrides(path: str | Path | None = None, doc_id: str | None = None) -> dict:
    p = Path(path) if path else DEFAULT_OVERRIDES
    if not p.is_file():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    return (data.get("docs", {}) or {}).get(doc_id or "", {}) or {}