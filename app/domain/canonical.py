"""规范化 JSON 与哈希（LLD §9.3）。金额 Decimal 定点、时间 UTC ISO、键排序、去 None。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


def _norm(value: Any) -> Any:
    if isinstance(value, Decimal):
        # 归一化：Decimal("1.10") 与 Decimal("1.1") 必须产生相同哈希（§9.3）
        return format(value.normalize(), "f")
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {str(k): _norm(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0])) if v is not None}
    if isinstance(value, (list, tuple)):
        return [_norm(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # 兜底：枚举/其它对象转字符串
    return str(value)


def canonical_json(value: Any) -> str:
    return json.dumps(_norm(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(data: str | bytes) -> str:
    b = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(b).hexdigest()


def payload_hash(value: Any) -> str:
    """返回 'sha256:<hex>'。用于幂等与 payload 冲突检测（§9.2/§9.3）。"""
    return "sha256:" + sha256_hex(canonical_json(value))