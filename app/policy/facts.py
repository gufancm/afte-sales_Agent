# -*- coding: utf-8 -*-
"""政策事实装配：把原始事实转成规则可直接比较的标量（确定性，不调 LLM）。

时间口径（容易出错，集中在这里）：
- 一律保存两个原始时间：``delivered_at``（签收）与 ``claim_at``（用户提出申请的时刻）；
  二者必须带时区，统一换算到东八区后按**自然日**计算。
- 绝不使用“每次现取系统当前时间”，否则重放/审计时同一订单会得到不同结论。
- 两种起算方式，按政策原文选择，不混用：
  * ``SIGN_DATE`` 签收日算第 1 天（7 天无理由：自签收之日起 7 个自然日）；
  * ``NEXT_DAY``   签收次日 0 点算第 1 天（新鲜烘焙：签收次日 0 点起算 15 个自然日）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

POLICY_TZ = timezone(timedelta(hours=8))
NO_REASON_CODES = ("NO_LONGER_NEEDED", "SIZE_ISSUE", "COLOR_MISMATCH")
BASIS_SIGN_DATE = "SIGN_DATE"
BASIS_NEXT_DAY = "NEXT_DAY"


def _parse(ts: object) -> datetime | None:
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=POLICY_TZ)
    if not isinstance(ts, str) or not ts.strip():
        return None
    text = ts.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:            # 无时区按东八区解释，避免与 UTC 混算
        dt = dt.replace(tzinfo=POLICY_TZ)
    return dt


def policy_date(ts: object) -> object:
    dt = _parse(ts)
    return None if dt is None else dt.astimezone(POLICY_TZ).date()


def day_index(delivered_at: object, claim_at: object, basis: str = BASIS_SIGN_DATE) -> int | None:
    """返回“申请发生在签收后的第几个自然日”，按政策起算方式。

    例：10-01 23:50 签收，10-08 23:55 申请 →
      SIGN_DATE = 8（超过 7 天），NEXT_DAY = 7。
    任一时间为空/不可解析/申请早于签收 → None（上层必须走 MISSING_FACT，不得当成 0）。
    """
    start, end = policy_date(delivered_at), policy_date(claim_at)
    if start is None or end is None:
        return None
    diff = (end - start).days
    if diff < 0:
        return None
    return diff + 1 if basis == BASIS_SIGN_DATE else diff


def parse_ts(ts: object) -> datetime | None:
    """公开的时间解析（带时区）；供规则有效期比较等确定性逻辑复用。"""
    return _parse(ts)


def ts_before(a: object, b: object) -> bool:
    """a < b（均按带时区 datetime 比较）。任一不可解析时返回 False（不排除规则）。"""
    da, db = _parse(a), _parse(b)
    if da is None or db is None:
        return False
    return da < db


def ts_after(a: object, b: object) -> bool:
    da, db = _parse(a), _parse(b)
    if da is None or db is None:
        return False
    return da > db


def compute_days_since_delivery(delivered_at: object, as_of: object) -> int | None:
    """兼容旧接口：等价于 SIGN_DATE 口径的天数索引。"""
    return day_index(delivered_at, as_of, BASIS_SIGN_DATE)


def normalize_category_value(raw: object) -> str:
    """延迟导入避免循环依赖（enums -> 无依赖，可安全直接导入）。"""
    from app.domain.enums import normalize_category
    return normalize_category(raw)


def enrich_facts(facts: dict) -> dict:
    """事实装配的**唯一入口**：补齐品类归一化与时间派生字段。

    - ``order.category`` 统一归一为受控词表（缺失 -> UNKNOWN，由规则 fail-closed）；
    - ``claim.day_index_signed`` / ``claim.day_index_next`` 只由原始
      ``order.delivered_at`` + ``claim.claim_at`` 派生，已有值不覆盖；
    - 返回新 dict，不修改调用方对象（保证 facts_snapshot_id 可复现）。
    """
    import copy
    out = copy.deepcopy(facts or {})
    order = out.setdefault("order", {})
    order["category"] = normalize_category_value(order.get("category"))
    claim = out.setdefault("claim", {})
    delivered_at, claim_at = order.get("delivered_at"), claim.get("claim_at")
    if claim.get("day_index_signed") is None:
        claim["day_index_signed"] = day_index(delivered_at, claim_at, BASIS_SIGN_DATE)
    if claim.get("day_index_next") is None:
        claim["day_index_next"] = day_index(delivered_at, claim_at, BASIS_NEXT_DAY)
    return out


def claim_time_facts(*, delivered_at: object, claim_at: object) -> dict:
    """装配给规则引擎的时间事实（原始值 + 两种口径的派生值）。"""
    return {
        "claim_at": claim_at,
        "delivered_at": delivered_at,
        "day_index_signed": day_index(delivered_at, claim_at, BASIS_SIGN_DATE),
        "day_index_next": day_index(delivered_at, claim_at, BASIS_NEXT_DAY),
    }
