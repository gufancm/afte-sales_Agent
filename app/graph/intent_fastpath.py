# -*- coding: utf-8 -*-
"""确定性意图快路径 + 复杂度分级（意图识别的工程化基础，LLD §2.4 / §7.4.1）。

工业界意图识别通常分三层，本模块实现前两层：
  1) 规则快路径（fast path）：高频、低风险、表述高度固定的意图（物流查询 / 催发货）
     直接用规则判定，不调用 LLM —— 零幻觉、零 token 成本、低延迟；
  2) 复杂度分级：简单单诉求走小模型，复杂 / 多诉求走大模型（客户端由 intent_guardrail 选择）；
  3) 复杂或不确定的请求交给 LLM 做结构化判定。

快路径的核心原则是“宁可不中，不可误中”：
  - 只在无歧义时判定 STATUS_QUERY / EXPEDITE 两类意图；
  - 一旦出现多诉求标记、多个订单号、写操作动词、金额、安全/注入词，立即放弃并回退 LLM，
    避免把“退款 + 查物流”这类多意图误判成单一 STATUS_QUERY。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.domain.enums import Intent

MAX_FASTPATH_LEN = 80

# 状态查询类关键词（想知道“到哪了 / 什么时候到”）
STATUS_KEYWORDS = (
    "物流", "快递", "到哪", "到哪儿", "什么时候到", "多久到", "几天到",
    "发货了吗", "发货了没", "发货没有", "发货没", "查快递", "查物流", "查一下物流",
    "查订单", "物流信息", "配送进度", "派送", "运单", "单号", "签收了吗", "到货了吗", "进度",
)
# 催发货 / 加急类关键词
EXPEDITE_KEYWORDS = (
    "催", "加急", "尽快发货", "赶紧发", "快点发", "尽快发", "快一点发", "催单", "催促",
    "尽快安排发货", "快点发货", "赶紧发货",
)
# 多诉求标记：出现即说明可能是一句话多意图，交 LLM
MULTI_INTENT_MARKERS = (
    "而且", "另外", "还有", "同时", "然后", "顺便", "并且", "以及", "加上", "再加", "再者",
    "此外", "一并", "一起处理", "两个诉求", "多个问题",
)
# 写操作动词：快路径不得把带写操作的句子判成只读意图
WRITE_MARKERS = (
    "退款", "退货", "退钱", "退我", "赔偿", "赔付", "报损", "拦截", "补发", "重发", "换货",
    "换新", "仅退款", "补寄", "重新发货", "投诉", "举报", "差评", "曝光",
)
# 安全 / 注入 / 法律敏感词
SAFETY_MARKERS_CN = (
    "忽略", "无视", "系统提示", "提示词", "管理员", "越权", "权限", "密码", "验证码", "密钥",
    "报警", "起诉", "律师", "法院", "监管", "工商", "消协", "315", "自杀", "轻生", "自残",
)
SAFETY_MARKERS_EN = ("ignore", "system prompt", "api key", "jailbreak", "sudo")
# 条件 / 转折句式
CONDITIONAL_MARKERS = ("如果", "但是", "不过", "万一", "除非", "要么", "否则", "假如")

AMOUNT_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:元|块|块钱|人民币)")
LABELLED_ORDER_RE = re.compile(r"(?:订单号|订单编号|订单|单号)\s*[:：]?\s*([A-Za-z0-9_\-]{4,32})")
DEMO_ORDER_RE = re.compile(r"\b(o_\d{2,})\b", re.IGNORECASE)


@dataclass(frozen=True)
class FastPathResult:
    """快路径判定结果（不经过 LLM；confidence 由调用方置 1.0）。"""

    intent: Intent
    matched_rule: str
    order_id: str | None = None
    waybill_no: str | None = None


def _order_ids(text: str) -> set[str]:
    ids = set(LABELLED_ORDER_RE.findall(text))
    ids |= set(DEMO_ORDER_RE.findall(text))
    return ids


def _extract_order_id(text: str) -> str | None:
    m = LABELLED_ORDER_RE.search(text)
    if m:
        return m.group(1)
    m = DEMO_ORDER_RE.search(text)
    return m.group(1) if m else None


def _blocked(text: str) -> str | None:
    """返回拦截原因；非空表示不得走快路径（回退 LLM）。"""
    if len(text) > MAX_FASTPATH_LEN:
        return "TOO_LONG"
    if any(k in text for k in MULTI_INTENT_MARKERS):
        return "MULTI_INTENT"
    if any(k in text for k in WRITE_MARKERS):
        return "WRITE_INTENT"
    low = text.lower()
    if any(k in text for k in SAFETY_MARKERS_CN) or any(k in low for k in SAFETY_MARKERS_EN):
        return "SAFETY"
    if AMOUNT_RE.search(text):
        return "AMOUNT"
    if len(_order_ids(text)) > 1:
        return "MULTI_ORDER"
    return None


def try_fastpath(text: str) -> FastPathResult | None:
    """尝试用规则直出意图；不确定时返回 None（由调用方回退 LLM）。"""
    t = (text or "").strip()
    if not t or _blocked(t):
        return None
    is_status = any(k in t for k in STATUS_KEYWORDS)
    is_expedite = any(k in t for k in EXPEDITE_KEYWORDS)
    if is_status and is_expedite:
        return None  # 两个诉求都要处理 -> 交 LLM 判定主/次意图
    if is_expedite:
        return FastPathResult(Intent.EXPEDITE, "expedite_kw", order_id=_extract_order_id(t))
    if is_status:
        return FastPathResult(Intent.STATUS_QUERY, "status_kw", order_id=_extract_order_id(t))
    return None


def estimate_complexity(text: str) -> int:
    """确定性复杂度打分：分越高越应交给大模型（0 表示最简单）。"""
    t = (text or "").strip()
    if not t:
        return 0
    low = t.lower()
    score = 0
    if len(t) > 50:
        score += 1
    if len(t) > 120:
        score += 1
    if any(k in t for k in MULTI_INTENT_MARKERS):
        score += 2
    if len(_order_ids(t)) > 1:
        score += 1
    if t.count("?") + t.count("？") > 1:
        score += 1
    if any(k in t for k in CONDITIONAL_MARKERS):
        score += 1
    if any(k in t for k in SAFETY_MARKERS_CN) or any(k in low for k in SAFETY_MARKERS_EN):
        score += 2
    if AMOUNT_RE.search(t):
        score += 1
    if any(k in t for k in WRITE_MARKERS):
        score += 1
    return score
