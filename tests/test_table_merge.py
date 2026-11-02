# -*- coding: utf-8 -*-
"""跨页表格合并：只合并可信的续页，不做模糊拼接。"""
from __future__ import annotations

from app.rag.table_merge import (TableChunk, merge_cross_page_tables, normalize_cell,
                                 replace_table_blocks, to_markdown)


def test_repeated_header_on_next_page_is_merged_and_deduped():
    chunks = [
        TableChunk(1, [["时效", "赔付比例"], ["48 小时", "30%"], ["72 小时", "50%"]]),
        TableChunk(2, [["时效", "赔付比例"], ["7 天", "80%"]]),
    ]
    rep = merge_cross_page_tables(chunks)
    assert rep.output_count == 1
    assert rep.merged_pairs == 1
    assert rep.tables[0].rows == [["时效", "赔付比例"], ["48 小时", "30%"],
                                  ["72 小时", "50%"], ["7 天", "80%"]]
    assert rep.tables[0].pages == [1, 2]


def test_page_gap_too_large_is_not_merged():
    chunks = [
        TableChunk(1, [["a", "b"], ["1", "2"]]),
        TableChunk(3, [["a", "b"], ["3", "4"]]),
    ]
    assert merge_cross_page_tables(chunks).output_count == 2


def test_different_column_count_is_not_merged():
    chunks = [
        TableChunk(1, [["a", "b"], ["1", "2"]]),
        TableChunk(2, [["a", "b", "c"], ["3", "4", "5"]]),
    ]
    assert merge_cross_page_tables(chunks).output_count == 2


def test_different_header_on_adjacent_page_is_not_merged():
    chunks = [
        TableChunk(1, [["时效", "赔付"], ["48 小时", "30%"]]),
        TableChunk(2, [["品类", "限额"], ["生鲜", "200"]]),
    ]
    assert merge_cross_page_tables(chunks).output_count == 2


def test_truncated_row_continues_without_header():
    # 上一页最后一行被切断（末列为空），续页首行直接是数据
    chunks = [
        TableChunk(1, [["品类", "时效", "赔付"], ["生鲜", "48 小时", ""]]),
        TableChunk(2, [["服饰", "7 天", "50%"]]),
    ]
    rep = merge_cross_page_tables(chunks)
    assert rep.output_count == 1
    assert rep.tables[0].rows[-2:] == [["生鲜", "48 小时", ""], ["服饰", "7 天", "50%"]]


def test_empty_rows_dropped_and_unknown_page_never_merges():
    chunks = [
        TableChunk(None, [["a", "b"], ["", ""], ["1", "2"]]),
        TableChunk(None, [["a", "b"], ["3", "4"]]),
    ]
    rep = merge_cross_page_tables(chunks)
    assert rep.output_count == 2
    assert rep.tables[0].rows == [["a", "b"], ["1", "2"]]


def test_layout_adjacent_same_width_merges_without_repeated_header():
    # 真实语料常见：续页不重复表头，只能靠「上表贴页底 + 下表贴页顶 + 同版心」判定
    chunks = [
        TableChunk(1, [["商品分类", "举例说明"], ["手机", "已联网"]],
                   top=0.10, bottom=0.87, left=0.15, right=0.85),
        TableChunk(2, [["家具", "货;"]], top=0.08, bottom=0.40, left=0.15, right=0.85),
    ]
    rep = merge_cross_page_tables(chunks)
    assert rep.output_count == 1
    assert rep.tables[0].rows[-1] == ["家具", "货;"]
    assert rep.tables[0].pages == [1, 2]


def test_header_only_block_absorbs_body_on_same_page():
    chunks = [
        TableChunk(3, [["商品详情文案", "说明"]]),
        TableChunk(3, [["不支持", "商品不支持7天无理由退货"], ["支持", "支持退货"]]),
    ]
    rep = merge_cross_page_tables(chunks)
    assert rep.output_count == 1
    assert rep.tables[0].rows == [["商品详情文案", "说明"],
                                  ["不支持", "商品不支持7天无理由退货"],
                                  ["支持", "支持退货"]]


def test_same_page_two_full_tables_not_merged():
    chunks = [
        TableChunk(3, [["a", "b"], ["1", "2"]]),
        TableChunk(3, [["c", "d"], ["3", "4"]]),
    ]
    assert merge_cross_page_tables(chunks).output_count == 2


def test_layout_adjacent_but_different_width_not_merged():
    chunks = [
        TableChunk(1, [["a", "b"], ["1", "2"]],
                   top=0.10, bottom=0.87, left=0.15, right=0.85),
        TableChunk(2, [["3", "4"]], top=0.08, bottom=0.40, left=0.15, right=0.55),
    ]
    assert merge_cross_page_tables(chunks).output_count == 2


def test_layout_not_at_page_edges_not_merged():
    chunks = [
        TableChunk(1, [["a", "b"], ["1", "2"]],
                   top=0.10, bottom=0.60, left=0.15, right=0.85),
        TableChunk(2, [["3", "4"]], top=0.08, bottom=0.40, left=0.15, right=0.85),
    ]
    assert merge_cross_page_tables(chunks).output_count == 2


def test_normalize_cell_handles_fullwidth_and_spaces():
    assert normalize_cell("  ４８\n小时 ") == "48 小时"


def test_to_markdown_pads_ragged_rows():
    md = to_markdown([["a", "b"], ["1"]])
    assert md.splitlines()[1] == "| --- | --- |"
    assert md.splitlines()[2] == "| 1 |  |"


def test_replace_table_blocks_rewrites_only_merged_tables():
    md = "前言\n\n| h | v |\n| --- | --- |\n| 1 | 2 |\n\n中间\n\n| h | v |\n| --- | --- |\n| 3 | 4 |\n"
    chunks = [TableChunk(1, [["h", "v"], ["1", "2"]], top=0.1, bottom=0.87, left=0.1, right=0.9),
              TableChunk(2, [["3", "4"]], top=0.08, bottom=0.4, left=0.1, right=0.9)]
    rep = merge_cross_page_tables(chunks)
    out, replaced = replace_table_blocks(md, rep)
    assert replaced == 2
    assert out.count("| h | v |") == 1
    # 原表块的分隔线与数据行必须被整体替换，不能残留出第二张表
    assert out.count("| --- | --- |") == 1
    assert "| 3 | 4 |" in out
    assert "中间" in out


def test_replace_table_blocks_refuses_on_count_mismatch():
    md = "| h | v |\n| --- | --- |\n| 1 | 2 |\n"
    rep = merge_cross_page_tables([TableChunk(1, [["a", "b"], ["1", "2"]]),
                                   TableChunk(2, [["c", "d"]])])
    out, replaced = replace_table_blocks(md, rep)
    assert replaced == 0 and out == md
