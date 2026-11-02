# -*- coding: utf-8 -*-
"""平台规则 PDF 解析脚本（本期：只解析，不切分、不入库）。

主干：Docling      -> DoclingDocument(JSON) + Markdown（版面/阅读顺序/表格/页码溯源）
兜底：pdfplumber   -> Docling 失败或输出不可信时补救，并对每份做交叉校验

用法：
  python scripts/parse_platform_pdfs.py --src "E:\\agent\\政策原文\\京东" --out "E:\\agent\\政策解析结果\\jd"
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import time
import traceback
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag.table_merge import (TableChunk, merge_cross_page_tables,  # noqa: E402
                                 replace_table_blocks, to_markdown)


def slug(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).strip()
    t = re.sub(r"[\s/\\:*?\"<>|]+", "-", t)
    t = re.sub(r"[^\w\u4e00-\u9fff-]", "", t)
    return t.strip("-")[:60] or "doc"


def extract_pdfplumber(path: Path):
    import pdfplumber
    texts = []
    tables = 0
    with pdfplumber.open(str(path)) as pdf:
        pages = len(pdf.pages)
        for pg in pdf.pages:
            texts.append(pg.extract_text() or "")
            tables += len(pg.extract_tables() or [])
    return "\n".join(texts), tables, pages


def build_docling_converter(do_ocr: bool):
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    opts = PdfPipelineOptions()
    opts.do_ocr = do_ocr
    opts.do_table_structure = True
    opts.table_structure_options.do_cell_matching = True
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
    )


def save_json(doc, path: Path) -> None:
    try:
        doc.save_as_json(path)
    except Exception:
        doc.save_as_json(str(path))


def _cell_text(cell) -> str:
    if isinstance(cell, dict):
        return str(cell.get("text") or "")
    return str(getattr(cell, "text", "") or "")


def table_rows(table) -> list[list[str]]:
    """把 Docling TableItem 转成二维字符串网格（优先用已对齐的 grid）。"""
    data = getattr(table, "data", None)
    grid = getattr(data, "grid", None)
    if grid:
        return [[_cell_text(c) for c in row] for row in grid]
    cells = getattr(data, "table_cells", None) or []
    n_rows = int(getattr(data, "num_rows", 0) or 0)
    n_cols = int(getattr(data, "num_cols", 0) or 0)
    if not cells or not n_rows or not n_cols:
        return []
    matrix = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    for c in cells:
        r = int(getattr(c, "start_row_offset_idx", 0))
        col = int(getattr(c, "start_col_offset_idx", 0))
        if 0 <= r < n_rows and 0 <= col < n_cols:
            matrix[r][col] = _cell_text(c)
    return matrix


def table_page_no(table) -> int | None:
    prov = getattr(table, "prov", None) or []
    if not prov:
        return None
    return getattr(prov[0], "page_no", None)


def page_sizes(doc) -> dict[int, tuple[float, float]]:
    """页码 -> (宽, 高)，用于把表格 bbox 归一化为页内比例。"""
    out: dict[int, tuple[float, float]] = {}
    for key, page in (getattr(doc, "pages", {}) or {}).items():
        size = getattr(page, "size", None)
        if size is None:
            continue
        page_no = getattr(page, "page_no", key)
        out[int(page_no)] = (float(size.width), float(size.height))
    return out


def table_geometry(table, sizes: dict[int, tuple[float, float]]) -> dict:
    """从 prov.bbox 推出归一化的 top/bottom/left/right（统一从页面左上角算起）。"""
    prov = getattr(table, "prov", None) or []
    if not prov:
        return {}
    page_no = getattr(prov[0], "page_no", None)
    bbox = getattr(prov[0], "bbox", None)
    if page_no is None or bbox is None or int(page_no) not in sizes:
        return {}
    width, height = sizes[int(page_no)]
    if width <= 0 or height <= 0:
        return {}
    origin = getattr(bbox, "coord_origin", None)
    origin = getattr(origin, "value", origin)
    if str(origin).upper().endswith("BOTTOMLEFT"):
        top, bottom = height - float(bbox.t), height - float(bbox.b)
    else:
        top, bottom = float(bbox.t), float(bbox.b)
    return {"top": top / height, "bottom": bottom / height,
            "left": float(bbox.l) / width, "right": float(bbox.r) / width}


def main() -> int:
    ap = argparse.ArgumentParser(description="平台规则 PDF 解析（Docling 主干 + pdfplumber 兜底）")
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-docling", action="store_true", help="只用 pdfplumber（排障）")
    ap.add_argument("--do-ocr", action="store_true", help="Docling 开 OCR（默认关）")
    ap.add_argument("--no-merge-tables", action="store_true",
                    help="关闭跨页表格合并（默认开启，仅用于对照）")
    args = ap.parse_args()

    src = Path(args.src)
    out = Path(args.out)
    fb = out / "fallback"
    out.mkdir(parents=True, exist_ok=True)
    fb.mkdir(parents=True, exist_ok=True)

    pdfs = sorted(src.glob("*.pdf"))
    if args.limit:
        pdfs = pdfs[: args.limit]
    print(f"[i] PDF 数：{len(pdfs)}；输出：{out}")

    converter = None
    if not args.no_docling:
        try:
            converter = build_docling_converter(args.do_ocr)
            print("[i] Docling converter 就绪")
        except Exception:
            print("[!] Docling 初始化失败 -> pdfplumber-only：")
            traceback.print_exc()

    rows = []
    for i, pdf in enumerate(pdfs, 1):
        rid = pdf.name.split("_", 1)[0]
        title = pdf.stem.split("_", 1)[1] if "_" in pdf.stem else pdf.stem
        stem = f"{rid}_{slug(title)}"
        row = {"rule_id": rid, "title": title, "pages": "", "docling_chars": "",
               "pdfplumber_chars": "", "docling_tables": "", "pdfplumber_tables": "",
               "tables_merged": "", "table_merge_pairs": "", "md_tables_replaced": "",
               "status": "", "note": ""}
        print(f"\n[{i}/{len(pdfs)}] {pdf.name}")

        try:
            pp_text, pp_tables, pp_pages = extract_pdfplumber(pdf)
            row["pdfplumber_chars"] = len(pp_text)
            row["pdfplumber_tables"] = pp_tables
            row["pages"] = pp_pages
        except Exception as e:
            pp_text, pp_tables, pp_pages = "", 0, 0
            row["note"] += f"pdfplumber失败:{e}; "

        dl_ok, dl_chars, dl_tables = False, 0, 0
        if converter is not None:
            t0 = time.time()
            try:
                res = converter.convert(str(pdf))
                doc = res.document
                md = doc.export_to_markdown()
                dl_chars = len(md)
                try:
                    tables = list(getattr(doc, "tables", []) or [])
                except Exception:
                    tables = []
                dl_tables = len(tables) or md.count("\n|")
                (out / f"{stem}.docling.md").write_text(md, encoding="utf-8")
                save_json(doc, out / f"{stem}.docling.json")
                dl_ok = dl_chars > 0
                row["docling_chars"] = dl_chars
                row["docling_tables"] = dl_tables

                # 跨页表格合并：同一张表被页边界切开时拼回，去掉续页重复表头
                if not args.no_merge_tables and tables:
                    sizes = page_sizes(doc)
                    chunks = [TableChunk(page_no=table_page_no(t), rows=table_rows(t),
                                         **table_geometry(t, sizes))
                              for t in tables]
                    chunks = [c for c in chunks if c.rows]
                    rep = merge_cross_page_tables(chunks)
                    row["tables_merged"] = rep.output_count
                    row["table_merge_pairs"] = rep.merged_pairs
                    if rep.merged_groups:
                        md_parts = []
                        for mt in rep.tables:
                            md_parts.append(f"\n<!-- table pages {mt.page_span} -->\n"
                                            + to_markdown(mt.rows) + "\n")
                        (out / f"{stem}.tables.merged.md").write_text(
                            "\n".join(md_parts), encoding="utf-8")
                        merged_md, replaced = replace_table_blocks(md, rep)
                        row["md_tables_replaced"] = replaced
                        if replaced:
                            (out / f"{stem}.docling.merged.md").write_text(
                                merged_md, encoding="utf-8")
                        row["note"] += f"跨页表合并{rep.merged_pairs}处; "
                    print(f"    Docling ok: {dl_chars}字 / {dl_tables}表"
                          f"（合并后{rep.output_count}，跨页{rep.merged_pairs}处）"
                          f" / {time.time()-t0:.1f}s")
                else:
                    row["tables_merged"] = dl_tables
                    row["table_merge_pairs"] = 0
                    row["md_tables_replaced"] = 0
                    print(f"    Docling ok: {dl_chars}字 / {dl_tables}表 / {time.time()-t0:.1f}s")
            except Exception as e:
                row["note"] += f"Docling失败:{type(e).__name__}:{e}; "
                print(f"    [!] Docling 失败：{type(e).__name__}: {e}")

        if dl_ok:
            ratio = (dl_chars / row["pdfplumber_chars"]) if row["pdfplumber_chars"] else 1.0
            if row["pdfplumber_chars"] and ratio < 0.8:
                row["status"] = "fallback"
                row["note"] += f"Docling=pdfplumber*{ratio:.2f}<0.8; "
                (fb / f"{stem}.pdfplumber.md").write_text(pp_text, encoding="utf-8")
            else:
                row["status"] = "ok"
        else:
            row["status"] = "fallback"
            (fb / f"{stem}.pdfplumber.md").write_text(pp_text, encoding="utf-8")
            row["note"] += "Docling不可用,已pdfplumber兜底; "

        rows.append(row)

    fields = list(rows[0].keys()) if rows else [
        "rule_id", "title", "pages", "docling_chars", "pdfplumber_chars",
        "docling_tables", "pdfplumber_tables", "tables_merged", "table_merge_pairs",
        "md_tables_replaced", "status", "note"]
    rep = out / "_parse_report.csv"
    with rep.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    n_ok = sum(1 for r in rows if r["status"] == "ok")
    n_fb = sum(1 for r in rows if r["status"] == "fallback")
    print(f"\n[done] 共 {len(rows)}；ok={n_ok} fallback={n_fb}；报告：{rep}")
    return 0 if n_fb == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
