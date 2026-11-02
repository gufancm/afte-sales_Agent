# -*- coding: utf-8 -*-
"""面向用户的回复模板加载与渲染（数据驱动，见 policy/response_templates.json）。

为什么不是硬编码：拒答/受理文案里含法定情形、期限、赔付封顶等业务事实，
它们是政策内容的投影，必须与 Policy Engine 真正命中的规则绑定。
把话术写死在图节点里，会产生「代码里的政策」与「版本化政策」两套真相。

渲染规则：只有 reason_code + decision 匹配，且模板 basis_rule_ids 与本次命中 rule_ids
有交集时才使用模板；否则返回 (None, {})，由调用方走不含承诺的通用兜底。
"""
from __future__ import annotations

import json
import pathlib
from functools import lru_cache
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TEMPLATES_PATH = REPO_ROOT / "policy" / "response_templates.json"


@lru_cache
def _raw() -> dict[str, Any]:
    if not TEMPLATES_PATH.is_file():
        return {"version": "", "templates": []}
    return json.loads(TEMPLATES_PATH.read_text(encoding="utf-8"))


def templates_version() -> str:
    return str(_raw().get("version", ""))


def render_decision_message(decision: dict) -> tuple[str | None, dict]:
    """按命中的规则渲染文案；不满足绑定条件时返回 (None, {})。"""
    if not decision:
        return None, {}
    reason = decision.get("reason_code") or ""
    kind = decision.get("decision")
    rule_ids = set(decision.get("rule_ids") or [])
    for t in _raw().get("templates", []):
        if t.get("reason_code") != reason or t.get("expected_decision") != kind:
            continue
        basis = set(t.get("basis_rule_ids") or [])
        if basis and not (basis & rule_ids):
            continue
        meta = {"template_id": t.get("template_id"), "basis_rule_ids": sorted(basis),
                "templates_version": templates_version(), "policy_version": decision.get("policy_version"),
                "matched_rule_ids": sorted(rule_ids)}
        return str(t.get("text") or ""), meta
    return None, {}