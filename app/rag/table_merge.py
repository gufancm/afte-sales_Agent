# -*- coding: utf-8 -*-
"""跨页表格合并（Docling 表格后处理）。

问题：售后政策 PDF 里「赔付标准」「时效对照」这类表格经常跨页。Docling 的
`TableFormer` 按页识别，会把同一张表切成两个 `TableItem`，续页还会重复表头。
若不合并：
  - 切块后每页半张表各自成块，检索到「第 2 页」时拿不到表头，模型无法判断列含义；
  - 条款与表格的对应关系被拆断，证据闭包校验容易判为「证据不足」。

本模块只做**确定性**合并，判据可解释、可单测，不做模糊对齐：
  1. 页码相邻（`max_page_gap`，默认 1 页）；
  2. 列数相同；
  3. 满足以下任一「同一张表」的证据：
     a) 表头归一化后相同 -> 拼接正文并去掉重复表头；
     b) 上一页最后一行「不完整」（末尾空单元格）-> 同一行被切断，直接拼接；
     c) **版面邻接**：上表结束于页面底部（>=78%）且下表起于页面顶部（<=18%），
        且表格左右边界一致（同一版心）-> 续页未重复表头，按数据行拼接。
任一条不满足就视为两张不同的表，避免把正文里恰巧同列数的表格误并。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

_WS_RE = re.compile(r"\s+")


def normalize_cell(text: str | None) -> str:
    """单元格归一化：NFKC、压缩空白、去首尾。表头比对与去重都用它。"""
    if not text:
        return ""
    return _WS_RE.sub(" ", unicodedata.normalize("NFKC", str(text))).strip()


def normalize_row(row: list[str]) -> list[str]:
    return [normalize_cell(c) for c in row]


@dataclass(frozen=True)
class TableChunk:
    """一页上的一张表（含表头）。

    page_no 未知时传 None（永不参与合并）。top/bottom/left/right 为**页内归一化**坐标
    （0..1，top/bottom 从页面顶部算起），来自 Docling `prov.bbox`；缺省则不参与版面判定。
    """

    page_no: int | None
    rows: list[list[str]]
    top: float | None = None
    bottom: float | None = None
    left: float | None = None
    right: float | None = None


@dataclass
class MergedTable:
    rows: list[list[str]]
    pages: list[int] = field(default_factory=list)
    source_indexes: list[int] = field(default_factory=list)
    last_bottom: float | None = None
    left: float | None = None
    right: float | None = None

    @property
    def page_span(self) -> str:
        if not self.pages:
            return ""
        return str(self.pages[0]) if len(self.pages) == 1 else f"{self.pages[0]}-{self.pages[-1]}"


@dataclass
class MergeReport:
    tables: list[MergedTable]
    input_count: int
    merged_groups: list[list[int]] = field(default_factory=list)

    @property
    def output_count(self) -> int:
        return len(self.tables)

    @property
    def merged_pairs(self) -> int:
        """合并次数 = 源表数 - 合并后表数。"""
        return self.input_count - self.output_count

    def summary(self) -> dict:
        return {
            "input_tables": self.input_count,
            "output_tables": self.output_count,
            "merged_pairs": self.merged_pairs,
            "merged_groups": self.merged_groups,
        }


def _drop_empty(rows: list[list[str]]) -> list[list[str]]:
    out: list[list[str]] = []
    for row in rows:
        norm = normalize_row(row)
        if any(norm):
            out.append(norm)
    return out


def _column_count(rows: list[list[str]]) -> int:
    return max((len(r) for r in rows), default=0)


def _row_incomplete(row: list[str]) -> bool:
    """末行被页边界切断的确定性信号：末尾为空且前面有内容。"""
    if len(row) < 2:
        return False
    return row[-1] == "" and any(row[:-1])


def _layout_adjacent(cur: MergedTable, chunk: TableChunk,
                     *, min_prev_end: float = 0.78, max_next_start: float = 0.18) -> bool:
    """版面证据：上表贴页底 + 下表贴页顶 + 同一版心宽度。"""
    if cur.last_bottom is None or chunk.top is None:
        return False
    if cur.last_bottom < min_prev_end or chunk.top > max_next_start:
        return False
    if cur.left is None or cur.right is None or chunk.left is None or chunk.right is None:
        return False
    return abs(cur.left - chunk.left) <= 0.02 and abs(cur.right - chunk.right) <= 0.02


def merge_cross_page_tables(chunks: list[TableChunk], *, max_page_gap: int = 1) -> MergeReport:
    """把相邻页的同构表格拼成一张；返回合并结果与分组明细。"""
    tables: list[MergedTable] = []
    merged_groups: list[list[int]] = []
    cur: MergedTable | None = None

    for idx, chunk in enumerate(chunks):
        rows = _drop_empty(chunk.rows)
        if not rows:
            continue
        if cur is None:
            cur = MergedTable(rows=rows, pages=[chunk.page_no] if chunk.page_no else [],
                              source_indexes=[idx], last_bottom=chunk.bottom,
                              left=chunk.left, right=chunk.right)
            continue

        last_page = cur.pages[-1] if cur.pages else None
        page_no = chunk.page_no
        same_page = last_page is not None and page_no == last_page
        # 同页「只有表头的块 + 紧随其后的表体」：Docling 也会把一张表切成两块
        header_only_prev = len(cur.rows) == 1 and len(cur.source_indexes) == 1
        mergeable = (
            last_page is not None
            and page_no is not None
            and _column_count(rows) == _column_count(cur.rows)
            and (0 < page_no - last_page <= max_page_gap or (same_page and header_only_prev))
        )
        if mergeable:
            repeated_header = normalize_row(rows[0]) == normalize_row(cur.rows[0])
            if repeated_header:
                body = rows[1:]
            elif same_page and header_only_prev:
                body = rows  # 表头在上一个块里，本块整段都是表体
            elif _row_incomplete(cur.rows[-1]):
                body = rows  # 页边界把一行切成两半，续页首行是数据
            elif _layout_adjacent(cur, chunk):
                body = rows  # 续页未重复表头：版面贴边 + 同版心
            else:
                body = None
            if body is not None:
                cur.rows = cur.rows + body
                cur.pages.append(page_no)
                cur.source_indexes.append(idx)
                cur.last_bottom = chunk.bottom
                continue

        # 不满足合并条件：收尾并开启新表
        tables.append(cur)
        if len(cur.source_indexes) > 1:
            merged_groups.append(cur.source_indexes)
        cur = MergedTable(rows=rows, pages=[chunk.page_no] if chunk.page_no else [],
                          source_indexes=[idx], last_bottom=chunk.bottom,
                          left=chunk.left, right=chunk.right)

    if cur is not None:
        tables.append(cur)
        if len(cur.source_indexes) > 1:
            merged_groups.append(cur.source_indexes)

    return MergeReport(tables=tables, input_count=len(chunks), merged_groups=merged_groups)


def to_markdown(rows: list[list[str]]) -> str:
    """把合并后的网格导出为 Markdown（首行表头 + 分隔线）。"""
    if not rows:
        return ""
    width = _column_count(rows)

    def line(cells: list[str]) -> str:
        padded = list(cells) + [""] * (width - len(cells))
        return "| " + " | ".join(c.replace("|", "\\|") for c in padded) + " |"

    out = [line(rows[0]), "| " + " | ".join(["---"] * width) + " |"]
    out.extend(line(r) for r in rows[1:])
    return "\n".join(out)


def _markdown_table_blocks(md: str) -> list[tuple[int, int]]:
    """返回 Markdown 中连续表格块的行号区间 [start, end)。"""
    lines = md.splitlines()
    blocks: list[tuple[int, int]] = []
    start: int | None = None
    for i, line in enumerate(lines):
        is_row = line.lstrip().startswith("|")
        if is_row and start is None:
            start = i
        elif not is_row and start is not None:
            blocks.append((start, i))
            start = None
    if start is not None:
        blocks.append((start, len(lines)))
    return blocks


def replace_table_blocks(md: str, report: MergeReport) -> tuple[str, int]:
    """把 Markdown 里被拆开的表格块替换为合并后的表格。

    只有当「Markdown 表格块数 == 源表数」时才替换（否则说明 Docling 导出与
    `doc.tables` 不一致，宁可放弃改写也不产出错位文档）。返回 (新 md, 替换块数)。
    """
    blocks = _markdown_table_blocks(md)
    if len(blocks) != report.input_count:
        return md, 0
    lines = md.splitlines()
    replacements: dict[int, list[str]] = {}
    drop: set[int] = set()
    replaced = 0
    for table in report.tables:
        if len(table.source_indexes) <= 1:
            continue
        start, end = blocks[table.source_indexes[0]]
        replacements[start] = to_markdown(table.rows).splitlines()
        # 原表块除首行外的内容必须一并丢弃，否则会残留重复的表头/数据行
        drop.update(range(start + 1, end))
        # 后续页的表格块整体丢弃；两页之间的其它文字（页眉页脚等）保留，避免误删正文
        for idx in table.source_indexes[1:]:
            s, e = blocks[idx]
            drop.update(range(s, e))
        replaced += len(table.source_indexes)

    out: list[str] = []
    for i, line in enumerate(lines):
        if i in replacements:
            out.extend(replacements[i])
            continue
        if i in drop:
            continue
        out.append(line)
    return "\n".join(out) + ("\n" if md.endswith("\n") else ""), replaced
