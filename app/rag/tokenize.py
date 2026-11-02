# -*- coding: utf-8 -*-
"""BM25 分词（中文 + 英文）。jieba 可用时用搜索引擎模式；不可用时退化为确定性正则 + CJK 二元组。

纯 Python，无外部服务；jieba 首次调用会构建词典缓存。
"""
from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[0-9A-Za-z]+|[\u4e00-\u9fff]+")
_CJK_RE = re.compile(r"^[\u4e00-\u9fff]+$")

# 极简停用词（中英）：避免高频虚词贡献 BM25 权重
_STOPWORDS = frozenset({
    "的", "了", "和", "与", "及", "或", "是", "在", "为", "对", "以", "按", "并", "等", "有", "由", "至", "如",
    "后", "前", "内", "外", "上", "下", "中", "之", "其", "该", "本", "此", "若", "则", "向", "从", "被", "将",
    "a", "an", "the", "of", "to", "and", "or", "is", "are", "for", "in", "on", "by", "with", "at", "be", "as",
})

_jieba = None
_jieba_tried = False


def _get_jieba():
    global _jieba, _jieba_tried
    if not _jieba_tried:
        _jieba_tried = True
        try:
            import jieba  # type: ignore

            jieba.setLogLevel(60)
            _jieba = jieba
        except Exception:
            _jieba = None
    return _jieba


def _fallback_tokens(text: str) -> list[str]:
    """无 jieba：英文按词、中文按二元组（bigram），保证中文也有可用词法信号。"""
    out: list[str] = []
    for m in _TOKEN_RE.findall(text or ""):
        if _CJK_RE.match(m):
            if len(m) == 1:
                out.append(m)
            else:
                out.extend(m[i:i + 2] for i in range(len(m) - 1))
        else:
            out.append(m.lower())
    return out


def _all_tokens(text: str) -> list[str]:
    jb = _get_jieba()
    if jb is not None:
        raw = [t.strip().lower() for t in jb.cut_for_search(text or "")]
    else:
        raw = _fallback_tokens(text or "")
    return [t for t in raw if t and t not in _STOPWORDS and not t.isspace()]


def tokenize(text: str, *, keep_duplicates: bool = False) -> list[str]:
    """返回查询/文档的 term 列表；默认去重且保序。"""
    toks = _all_tokens(text)
    if keep_duplicates:
        return toks
    seen: dict[str, None] = {}
    for t in toks:
        seen.setdefault(t, None)
    return list(seen)


def token_counts(text: str) -> dict[str, int]:
    """term -> tf（BM25 的 term frequency）。"""
    counts: dict[str, int] = {}
    for t in _all_tokens(text):
        counts[t] = counts.get(t, 0) + 1
    return counts


def backend_name() -> str:
    return "jieba" if _get_jieba() is not None else "regex-bigram"