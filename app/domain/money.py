"""金额工具：以最小货币单位（分）为整数运算，避免浮点误差（LLD §2.4/§17.4）。"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

_MINOR = Decimal("0.01")


def to_minor(amount: Decimal) -> int:
    """Decimal 元 → 最小单位整数（四舍五入 HALF_UP）。"""
    return int((amount.quantize(_MINOR, rounding=ROUND_HALF_UP) * 100).to_integral_value())


def from_minor(minor: int) -> Decimal:
    return (Decimal(minor) / 100).quantize(_MINOR)


def parse_amount_text(text: str | None) -> int | None:
    """从用户文本提取金额；无法明确解析返回 None（交由澄清，不臆造）。"""
    if not text:
        return None
    s = text.strip().replace("，", "").replace(",", "")
    s = s.replace("元", "").replace("块", "").replace("￥", "").replace("¥", "").strip()
    try:
        return to_minor(Decimal(s))
    except Exception:
        return None


def allocate_minor(total_minor: int, weights: list[int]) -> list[int]:
    """按权重分摊总额，保证各份之和 == total（最大余数法，§17.4）。"""
    if not weights:
        return []
    if sum(weights) <= 0:
        raise ValueError("weights 之和必须为正")
    raw = [total_minor * w / sum(weights) for w in weights]
    floors = [int(r // 1) for r in raw]
    remainder = total_minor - sum(floors)
    order = sorted(range(len(weights)), key=lambda i: raw[i] - floors[i], reverse=True)
    for i in order[:remainder]:
        floors[i] += 1
    assert sum(floors) == total_minor
    return floors