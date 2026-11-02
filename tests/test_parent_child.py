# -*- coding: utf-8 -*-
"""条款解析器 + Parent-Child 构建单测（不依赖 Docling/torch，用确定性兜底路径验证）。"""
from __future__ import annotations

from app.rag.clause_parser import parse_document, cn_to_int
from app.rag.parent_child import (get_token_counter, build_parents,
                                  build_children_split, make_parent_id)

MD = """## 第一章 概述
## 第一条 目的
为规范商家行为，制定本规则。
## 第二条 适用范围
本规则适用于第三方商家。
## 第二章 退货规范
## 第三条 发起条件
1、消费者可发起申请。
2、商品应当完好。
## 第四条 例外情形
例外：以下商品不支持 7 天无理由退货。

## 表1：分类
| A | B |
|---|---|
| 1 | 2 |
"""

NUM_MD = """某某细则
1.【违规定义】
恶意占库存的说明。
2.【处理措施】
2.1 警告；
2.2 扣款。
"""


def test_cn_to_int():
    assert cn_to_int("五") == 5
    assert cn_to_int("十") == 10
    assert cn_to_int("十一") == 11
    assert cn_to_int("二十三") == 23
    assert cn_to_int("一百") == 100


def test_parse_articles_and_chapters():
    units = parse_document(MD, doc_id="jd_4268", rule_id="4268")
    ids = [u.logical_clause_id for u in units]
    assert "CL-JD4268-CH1" in ids and "CL-JD4268-CH2" in ids
    assert "CL-JD4268-A3" in ids and "CL-JD4268-A4" in ids
    assert "CL-JD4268-T1" in ids
    a4 = next(u for u in units if u.logical_clause_id == "CL-JD4268-A4")
    assert a4.is_exception is True
    a4b = next(u for u in units if u.logical_clause_id == "CL-JD4268-A3")
    assert a4b.is_exception is False
    assert a4b.heading_path == ["第二章 退货规范", "第三条 发起条件"]


def test_numbered_fallback():
    units = parse_document(NUM_MD, doc_id="jd_x", rule_id="1206154529616302080")
    ids = [u.logical_clause_id for u in units]
    assert any(i.startswith("CL-JD1206154529616302080-S") for i in ids)
    assert not any("-A" in i for i in ids)


def test_overrides_drop_and_flag():
    ov = {"CL-JD4268-A4": {"drop": True}, "CL-JD4268-A3": {"is_exception": True}}
    units = parse_document(MD, doc_id="jd_4268", rule_id="4268", overrides=ov)
    ids = [u.logical_clause_id for u in units]
    assert "CL-JD4268-A4" not in ids
    assert next(u for u in units if u.logical_clause_id == "CL-JD4268-A3").is_exception is True


def test_parent_child_relationship_and_tokens():
    units = parse_document(MD, doc_id="jd_4268", rule_id="4268")
    parents = build_parents(units, doc_id="jd_4268", version="2024.05")
    pids = {p.parent_id for p in parents}
    assert make_parent_id("jd_4268", "2024.05", "CL-JD4268-A3") in pids
    count_fn, _ = get_token_counter("none")
    children = build_children_split(parents, 512, count_fn)
    assert children, "应产生 Child"
    child_ids = [c.clause_id for c in children]
    assert len(child_ids) == len(set(child_ids)), "clause_id 必须唯一"
    for c in children:
        assert c.parent_id in pids, "Child.parent_id 必须指向存在的 Parent"
        assert c.token_count and c.token_count > 0
    # 章级 Parent 不产 Child
    chapter_ids = {p.logical_clause_id for p in parents if p.level == "CHAPTER"}
    assert not (chapter_ids & set(c.logical_clause_id for c in children))