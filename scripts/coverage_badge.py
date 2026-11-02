# -*- coding: utf-8 -*-
"""由 coverage.xml 生成自包含的覆盖率徽章 SVG（无需外部服务 / 无需额外依赖）。

用法：
  pytest --cov=app --cov-report=xml:coverage.xml
  python scripts/coverage_badge.py docs/coverage.svg coverage.xml

CI 中每次运行都会重新生成，并作为产物上传；README 直接引用 docs/coverage.svg。
"""
from __future__ import annotations

import pathlib
import sys
import xml.etree.ElementTree as ET

# 阈值 -> 颜色（与 shields.io 常见配色一致）
TIERS = [(95, "#4c1", "brightgreen"), (90, "#4c1", "green"), (80, "#97ca00", "green"),
         (70, "#a4a61d", "yellowgreen"), (60, "#dfb317", "yellow"), (50, "#fe7d37", "orange"),
         (0, "#e05d44", "red")]


def _color(pct: float) -> str:
    for bound, color, _name in TIERS:
        if pct >= bound:
            return color
    return "#e05d44"


def _text_width(text: str, size: float = 11.0) -> float:
    """粗略估算文本宽度（等宽近似），避免引入字体度量依赖。"""
    return len(text) * size * 0.62


def read_percent(xml_path: pathlib.Path) -> float:
    root = ET.parse(xml_path).getroot()
    rate = root.get("line-rate")
    if rate is None:
        raise ValueError("coverage.xml 缺少 line-rate")
    return round(float(rate) * 100, 1)


def badge_svg(pct: float) -> str:
    label = "coverage"
    value = f"{pct:.1f}%"
    pad = 10.0
    label_w = round(_text_width(label) + pad)
    value_w = round(_text_width(value) + pad)
    total_w = label_w + value_w
    color = _color(pct)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_w}" height="20" '
        f'role="img" aria-label="{label}: {value}">\n'
        f'  <title>{label}: {value}</title>\n'
        f'  <linearGradient id="s" x2="0" y2="100%">\n'
        f'    <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>\n'
        f'    <stop offset="1" stop-opacity=".1"/>\n'
        f'  </linearGradient>\n'
        f'  <clipPath id="r"><rect width="{total_w}" height="20" rx="3" fill="#fff"/></clipPath>\n'
        f'  <g clip-path="url(#r)">\n'
        f'    <rect width="{label_w}" height="20" fill="#555"/>\n'
        f'    <rect x="{label_w}" width="{value_w}" height="20" fill="{color}"/>\n'
        f'    <rect width="{total_w}" height="20" fill="url(#s)"/>\n'
        f'  </g>\n'
        f'  <g fill="#fff" text-anchor="middle" '
        f'font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">\n'
        f'    <text x="{label_w / 2:.0f}" y="15" fill="#010101" fill-opacity=".3">{label}</text>\n'
        f'    <text x="{label_w / 2:.0f}" y="14">{label}</text>\n'
        f'    <text x="{label_w + value_w / 2:.0f}" y="15" fill="#010101" '
        f'fill-opacity=".3">{value}</text>\n'
        f'    <text x="{label_w + value_w / 2:.0f}" y="14">{value}</text>\n'
        f'  </g>\n'
        f'</svg>\n'
    )


def main() -> None:
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "docs/coverage.svg")
    src = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else "coverage.xml")
    if not src.exists():
        print(f"跳过：找不到 {src}（先跑 pytest --cov-report=xml:{src}）")
        return
    pct = read_percent(src)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(badge_svg(pct), encoding="utf-8")
    print(f"coverage badge -> {out}（{pct:.1f}%）")


if __name__ == "__main__":
    main()