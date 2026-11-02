# -*- coding: utf-8 -*-
"""生成 Policy Engine 120 条金标用例（v2）。

设计原则：
1. 期望值由**人工按真实条款 + 规则口径**逐条写出，不由被测引擎回填；
2. 每条用例只变一个事实维度（品类/时效/金额/事实完整性），避免同一规则同义改写凑数；
3. 条款 ID 取自冻结的规则绑定（policy/rules.json），复核表会同时展示条款原文，供人工核对；
4. 事实一律给全（除非该用例本身就是“缺事实”），避免“缺品类”把结论意外变成 UNKNOWN。
"""
from __future__ import annotations

import io
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.policy.library import load_rules  # noqa: E402

OUT_JSONL = pathlib.Path(__file__).resolve().parent / "data" / "policy_cases_v2.jsonl"

RULES = {r.rule_id: r for r in load_rules()}

CASES: list[dict] = []


def case(cid, group, intent, rule_ref, decision, reason, *, delivered="2026-10-10T10:00:00+08:00",
         claim="2026-10-13T10:00:00+08:00", category="APPAREL", status="DELIVERED",
         reason_code="NO_LONGER_NEEDED", parcel="SIGNED", amount=None, quantity=None,
         as_of=None, clause_ids=None, execution_blocked=False, note=""):
    order = {"order_id": "O" + cid.split("-")[-1], "category": category, "status": status,
             "currency": "CNY", "version": "1"}
    if delivered is not None:
        order["delivered_at"] = delivered
    claim_facts = {"reason_code": reason_code}
    if claim is not None:
        claim_facts["claim_at"] = claim
    if quantity is not None:
        claim_facts["quantity"] = quantity
    finance = {"currency": "CNY"}
    if amount is not None:
        finance["requested_amount_minor"] = amount
    if clause_ids is None:
        clause_ids = list(RULES[rule_ref].required_clause_ids) if rule_ref in RULES else []
    CASES.append({
        "case_id": cid,
        "group": group,
        "intent": intent,
        "rule_ref": rule_ref,
        "facts": {"order": order, "logistics": {"parcel_status": parcel},
                  "finance": finance, "claim": claim_facts, "intent": intent},
        "as_of": as_of or claim or "2026-10-13T10:00:00+08:00",
        "expect": {"decision": decision, "reason_code": reason, "rule_id": rule_ref,
                   "required_clause_ids": clause_ids, "execution_blocked": execution_blocked},
        "note": note,
        "review": "pending",
    })


# =========================================================================== #
# A. 正常允许（20）
# =========================================================================== #
case("POL-A01", "正常允许", "RETURN_REFUND", "R-JD-NR-WINDOW-OK-001", "ALLOW", "NO_REASON_WITHIN_7D",
     delivered="2026-10-10T10:00:00+08:00", claim="2026-10-10T20:00:00+08:00",
     note="签收当日申请（第 1 天）")
case("POL-A02", "正常允许", "RETURN_REFUND", "R-JD-NR-WINDOW-OK-001", "ALLOW", "NO_REASON_WITHIN_7D",
     delivered="2026-10-04T09:00:00+08:00", claim="2026-10-10T09:00:00+08:00",
     note="第 7 天边界内")
case("POL-A03", "正常允许", "RETURN_REFUND", "R-JD-NR-WINDOW-OK-001", "ALLOW", "NO_REASON_WITHIN_7D",
     delivered="2026-10-08T12:00:00+08:00", claim="2026-10-11T12:00:00+08:00",
     reason_code="SIZE_ISSUE", note="尺码不合适（第 4 天）")
case("POL-A04", "正常允许", "RETURN_REFUND", "R-JD-NR-WINDOW-OK-001", "ALLOW", "NO_REASON_WITHIN_7D",
     delivered="2026-10-06T12:00:00+08:00", claim="2026-10-11T12:00:00+08:00",
     reason_code="COLOR_MISMATCH", note="颜色不符（第 6 天）")
case("POL-A05", "正常允许", "COMPLAINT", "R-JD-GOLD-WEIGHT-001", "ALLOW", "GOLD_WEIGHT_COMPENSATION",
     category="GOLD", reason_code="WEIGHT_DIFF", note="黄金克重差异")
case("POL-A06", "正常允许", "COMPLAINT", "R-JD-GOLD-QUALITY-001", "ALLOW", "GOLD_QUALITY_RETURN",
     category="JEWELRY", reason_code="QUALITY_DEFECT", note="珠宝质量问题")
case("POL-A07", "正常允许", "RETURN_REFUND", "R-JD-EBIKE-3BAO-001", "ALLOW", "EBIKE_MOTOR_RETURN",
     category="EBIKE", reason_code="DAMAGED", note="电动车三包期内损坏")
case("POL-A08", "正常允许", "RETURN_REFUND", "R-JD-Q-RETURN-001", "ALLOW", "QUALITY_RETURN_WINDOW",
     reason_code="QUALITY_DEFECT", note="服饰质量问题退货")
case("POL-A09", "正常允许", "RETURN_REFUND", "R-JD-Q-RETURN-001", "ALLOW", "QUALITY_RETURN_WINDOW",
     reason_code="NOT_AS_DESCRIBED", note="与描述不符")
case("POL-A10", "正常允许", "RETURN_REFUND", "R-JD-Q-RETURN-001", "ALLOW", "QUALITY_RETURN_WINDOW",
     category="BOOK_MEDIA", reason_code="QUALITY_DEFECT", note="图书质量问题（不适用无理由例外）")
case("POL-A11", "正常允许", "RETURN_REFUND", "R-JD-Q-RETURN-001", "ALLOW", "QUALITY_RETURN_WINDOW",
     category="FRESH", reason_code="DAMAGED", note="生鲜破损（质量类，不受无理由例外影响）")
case("POL-A12", "正常允许", "REFUND_ONLY", "R-JD-CANCEL-UNSHIPPED-001", "ALLOW", "UNSHIPPED_CANCEL_REFUND",
     status="PAID_UNSHIPPED", parcel="UNKNOWN", delivered=None, claim=None,
     as_of="2026-10-13T10:00:00+08:00", note="未发货取消（服饰）")
case("POL-A13", "正常允许", "REFUND_ONLY", "R-JD-CANCEL-UNSHIPPED-001", "ALLOW", "UNSHIPPED_CANCEL_REFUND",
     status="PAID_UNSHIPPED", parcel="UNKNOWN", category="DIGITAL", delivered=None, claim=None,
     as_of="2026-10-13T10:00:00+08:00", note="未发货取消（数码）")
case("POL-A14", "正常允许", "INTERCEPT_REFUND", "R-JD-INTERCEPT-INTRANSIT-001", "ALLOW", "INTERCEPT_OK",
     status="SHIPPED", parcel="IN_TRANSIT", note="在途拦截")
case("POL-A15", "正常允许", "COMPLAINT", "R-JD-COMP-LATE-001", "ALLOW", "LATE_DELIVERY_COMPENSATION",
     reason_code="LATE_DELIVERY", amount=600, note="延迟发货赔付 6 元")
case("POL-A16", "正常允许", "COMPLAINT", "R-JD-COMP-OOS-001", "ALLOW", "OUT_OF_STOCK_COMPENSATION",
     reason_code="OUT_OF_STOCK", amount=2000, note="缺货赔付，恰好等于自动执行阈值")
case("POL-A17", "正常允许", "COMPLAINT", "R-JD-COMP-OOS-001", "ALLOW", "OUT_OF_STOCK_COMPENSATION",
     reason_code="FALSE_SHIPMENT", amount=1000, note="虚假发货赔付")
case("POL-A18", "正常允许", "RESHIPMENT", "R-JD-BAKERY-WINDOW-OK-001", "ALLOW", "BAKERY_EXCHANGE_WITHIN_15D",
     category="BAKERY", reason_code="DATE_DISSATISFIED",
     delivered="2026-10-05T09:00:00+08:00", claim="2026-10-15T09:00:00+08:00",
     note="烘焙第 10 天换货")
case("POL-A19", "正常允许", "RESHIPMENT", "R-JD-BAKERY-WINDOW-OK-001", "ALLOW", "BAKERY_EXCHANGE_WITHIN_15D",
     category="BAKERY", reason_code="DATE_DISSATISFIED",
     delivered="2026-10-09T09:00:00+08:00", claim="2026-10-12T09:00:00+08:00",
     note="烘焙第 3 天换货")
case("POL-A20", "正常允许", "RETURN_REFUND", "R-JD-Q-RETURN-001", "ALLOW", "QUALITY_RETURN_WINDOW",
     category="CUSTOM", reason_code="DAMAGED", note="定制商品质量问题（质量类不受无理由例外影响）")

# =========================================================================== #
# B. 明确拒绝（20）
# =========================================================================== #
case("POL-B01", "明确拒绝", "RETURN_REFUND", "R-JD-NR-DENY-FRESH-001", "DENY", "NO_REASON_FRESH_EXEMPT",
     category="FRESH", note="生鲜无理由不适用（第 1 天也不行）")
case("POL-B02", "明确拒绝", "RETURN_REFUND", "R-JD-NR-DENY-CUSTOM-001", "DENY", "NO_REASON_CUSTOM_EXEMPT",
     category="CUSTOM", note="定制无理由不适用")
case("POL-B03", "明确拒绝", "RETURN_REFUND", "R-JD-NR-DENY-DIGITAL-001", "DENY", "NO_REASON_DIGITAL_EXEMPT",
     category="DIGITAL", note="数码无理由不适用")
case("POL-B04", "明确拒绝", "RETURN_REFUND", "R-JD-NR-DENY-DIGITAL-001", "DENY", "NO_REASON_DIGITAL_EXEMPT",
     category="APPLIANCE", note="家电无理由不适用")
case("POL-B05", "明确拒绝", "RETURN_REFUND", "R-JD-NR-DENY-DIGITAL-001", "DENY", "NO_REASON_DIGITAL_EXEMPT",
     category="VIRTUAL", note="虚拟商品无理由不适用")
case("POL-B06", "明确拒绝", "RETURN_REFUND", "R-JD-NR-DENY-SEALED-001", "DENY", "NO_REASON_SEALED_EXEMPT",
     category="BOOK_MEDIA", note="图书音像拆封无理由不适用")
case("POL-B07", "明确拒绝", "RETURN_REFUND", "R-JD-NR-DENY-FRESH-001", "DENY", "NO_REASON_FRESH_EXEMPT",
     category="FRESH", reason_code="SIZE_ISSUE", note="生鲜尺码类诉求仍属无理由")
case("POL-B08", "明确拒绝", "RETURN_REFUND", "R-JD-NR-DENY-CUSTOM-001", "DENY", "NO_REASON_CUSTOM_EXEMPT",
     category="CUSTOM", reason_code="COLOR_MISMATCH", note="定制颜色不符仍属无理由")
case("POL-B09", "明确拒绝", "RETURN_REFUND", "R-JD-NR-DENY-DIGITAL-001", "DENY", "NO_REASON_DIGITAL_EXEMPT",
     category="DIGITAL", reason_code="SIZE_ISSUE", note="数码尺码类诉求")
case("POL-B10", "明确拒绝", "RETURN_REFUND", "R-JD-NR-WINDOW-EXPIRED-001", "DENY", "NO_REASON_WINDOW_EXPIRED",
     delivered="2026-10-01T10:00:00+08:00", claim="2026-10-08T10:00:00+08:00", note="第 8 天申请")
case("POL-B11", "明确拒绝", "RETURN_REFUND", "R-JD-NR-WINDOW-EXPIRED-001", "DENY", "NO_REASON_WINDOW_EXPIRED",
     delivered="2026-09-01T10:00:00+08:00", claim="2026-10-01T10:00:00+08:00", note="第 31 天申请")
case("POL-B12", "明确拒绝", "RETURN_REFUND", "R-JD-NR-WINDOW-EXPIRED-001", "DENY", "NO_REASON_WINDOW_EXPIRED",
     delivered="2026-10-01T15:50:00+00:00", claim="2026-10-08T15:55:00+00:00",
     note="UTC 时间戳：东八区 10-01 23:50 签收 / 10-08 23:55 申请（第 8 天）")
case("POL-B13", "明确拒绝", "RESHIPMENT", "R-JD-BAKERY-WINDOW-EXPIRED-001", "DENY", "BAKERY_EXCHANGE_WINDOW_EXPIRED",
     category="BAKERY", reason_code="DATE_DISSATISFIED",
     delivered="2026-10-01T10:00:00+08:00", claim="2026-10-17T10:00:00+08:00", note="烘焙第 16 天")
case("POL-B14", "明确拒绝", "RESHIPMENT", "R-JD-BAKERY-WINDOW-EXPIRED-001", "DENY", "BAKERY_EXCHANGE_WINDOW_EXPIRED",
     category="BAKERY", reason_code="DATE_DISSATISFIED",
     delivered="2026-08-01T10:00:00+08:00", claim="2026-10-01T10:00:00+08:00", note="烘焙第 61 天")
case("POL-B15", "明确拒绝", "INTERCEPT_REFUND", "R-JD-INTERCEPT-DELIVERED-DENY-001", "DENY", "INTERCEPT_ALREADY_DELIVERED",
     status="DELIVERED", parcel="SIGNED", note="已签收不能再拦截")
case("POL-B16", "明确拒绝", "INTERCEPT_REFUND", "R-JD-INTERCEPT-DELIVERED-DENY-001", "DENY", "INTERCEPT_ALREADY_DELIVERED",
     status="SHIPPED", parcel="DELIVERED", note="物流已妥投")
case("POL-B17", "明确拒绝", "RETURN_REFUND", "R-JD-NR-DENY-FRESH-001", "DENY", "NO_REASON_FRESH_EXEMPT",
     category="FRESH", delivered="2026-10-09T10:00:00+08:00", claim="2026-10-11T10:00:00+08:00",
     note="冲突用例：生鲜第 3 天，通用允许规则同时命中，仍必须拒绝")
case("POL-B18", "明确拒绝", "RETURN_REFUND", "R-JD-NR-DENY-CUSTOM-001", "DENY", "NO_REASON_CUSTOM_EXEMPT",
     category="CUSTOM", delivered="2026-10-04T10:00:00+08:00", claim="2026-10-10T10:00:00+08:00",
     note="定制第 7 天，窗口允许仍被例外压过")
case("POL-B19", "明确拒绝", "RETURN_REFUND", "R-JD-NR-DENY-DIGITAL-001", "DENY", "NO_REASON_DIGITAL_EXEMPT",
     category="DIGITAL", reason_code="COLOR_MISMATCH", delivered="2026-10-09T10:00:00+08:00",
     claim="2026-10-10T10:00:00+08:00", note="数码第 2 天颜色不符")
case("POL-B20", "明确拒绝", "RETURN_REFUND", "R-JD-NR-DENY-SEALED-001", "DENY", "NO_REASON_SEALED_EXEMPT",
     category="BOOK_MEDIA", reason_code="SIZE_ISSUE", delivered="2026-10-05T10:00:00+08:00",
     claim="2026-10-10T10:00:00+08:00", note="图书第 6 天尺寸诉求")

# =========================================================================== #
# C. 缺少事实（15）
# =========================================================================== #
case("POL-C01", "缺少事实", "RETURN_REFUND", None, "NEED_INFO", "MISSING_FACT",
     reason_code=None, note="未提供退货原因")
case("POL-C02", "缺少事实", "RETURN_REFUND", None, "NEED_INFO", "MISSING_FACT",
     category="FRESH", reason_code=None, note="生鲜但原因未知：不能直接按例外拒绝，先问清原因")
case("POL-C03", "缺少事实", "RETURN_REFUND", None, "NEED_INFO", "MISSING_FACT",
     claim=None, note="缺申请时点，无法判断是否在 7 天内")
case("POL-C04", "缺少事实", "RESHIPMENT", None, "NEED_INFO", "MISSING_FACT",
     category="BAKERY", reason_code="DATE_DISSATISFIED", claim=None, note="烘焙缺申请时点")
case("POL-C05", "缺少事实", "RETURN_REFUND", None, "MANUAL", "AUTHORITATIVE_FACT_UNAVAILABLE",
     delivered=None, note="缺签收时间是系统事实，不能由用户随便填")
case("POL-C06", "缺少事实", "RETURN_REFUND", None, "MANUAL", "CATEGORY_UNKNOWN_REVIEW",
     category="新品类X", note="未知品类 fail-closed")
case("POL-C07", "缺少事实", "RETURN_REFUND", None, "MANUAL", "CATEGORY_UNKNOWN_REVIEW",
     category=None, note="品类缺失归一为 UNKNOWN")
case("POL-C08", "缺少事实", "INTERCEPT_REFUND", None, "MANUAL", "AUTHORITATIVE_FACT_UNAVAILABLE",
     status="SHIPPED", parcel=None, note="缺物流状态，无法判断是否可拦截")
case("POL-C09", "缺少事实", "REFUND_ONLY", None, "MANUAL", "AUTHORITATIVE_FACT_UNAVAILABLE",
     status=None, parcel="UNKNOWN", note="缺订单状态")
case("POL-C10", "缺少事实", "RETURN_REFUND", None, "MANUAL", "NO_APPLICABLE_POLICY",
     reason_code="SOMETHING_NEW", note="事实齐全但无任何规则覆盖该原因")
case("POL-C11", "缺少事实", "COMPLAINT", "R-JD-COMP-LATE-001", "ALLOW", "LATE_DELIVERY_COMPENSATION",
     reason_code="LATE_DELIVERY", amount=None, execution_blocked=True,
     note="缺金额：保留资格但阻断执行")
case("POL-C12", "缺少事实", "COMPLAINT", "R-JD-COMP-OOS-001", "ALLOW", "OUT_OF_STOCK_COMPENSATION",
     reason_code="OUT_OF_STOCK", amount=None, execution_blocked=True,
     note="缺金额（缺货赔付）")
case("POL-C13", "缺少事实", "RETURN_REFUND", "R-JD-NR-WINDOW-OK-001", "ALLOW", "NO_REASON_WITHIN_7D",
     delivered="2026-10-13T09:00:00+08:00", claim="2026-10-13T09:00:00+08:00",
     note="签收当日申请（第 1 天，时间事实完整）")
case("POL-C14", "缺少事实", "RETURN_REFUND", None, "MANUAL", "AUTHORITATIVE_FACT_UNAVAILABLE",
     delivered="2026-10-13T10:00:00+08:00", claim="2026-10-12T10:00:00+08:00",
     note="申请时点早于签收（权威事实冲突/异常）-> 转人工，不能让用户改时间绕过")
case("POL-C15", "缺少事实", "RETURN_REFUND", None, "MANUAL", "AUTHORITATIVE_FACT_UNAVAILABLE",
     delivered="2026-13-45T10:00:00+08:00", note="签收时间不可解析")

# =========================================================================== #
# D. 时间边界（20）
# =========================================================================== #
case("POL-D01", "时间边界", "RETURN_REFUND", "R-JD-NR-WINDOW-OK-001", "ALLOW", "NO_REASON_WITHIN_7D",
     delivered="2026-10-01T00:00:00+08:00", claim="2026-10-07T23:59:00+08:00", note="第 7 天 23:59")
case("POL-D02", "时间边界", "RETURN_REFUND", "R-JD-NR-WINDOW-EXPIRED-001", "DENY", "NO_REASON_WINDOW_EXPIRED",
     delivered="2026-10-01T00:00:00+08:00", claim="2026-10-08T00:00:00+08:00", note="第 8 天 00:00")
case("POL-D03", "时间边界", "RETURN_REFUND", "R-JD-NR-WINDOW-OK-001", "ALLOW", "NO_REASON_WITHIN_7D",
     delivered="2026-10-01T23:50:00+08:00", claim="2026-10-07T23:55:00+08:00", note="深夜签收仍按自然日，第 7 天")
case("POL-D04", "时间边界", "RETURN_REFUND", "R-JD-NR-WINDOW-EXPIRED-001", "DENY", "NO_REASON_WINDOW_EXPIRED",
     delivered="2026-10-01T23:50:00+08:00", claim="2026-10-08T23:55:00+08:00", note="评审原例：第 8 天")
case("POL-D05", "时间边界", "RETURN_REFUND", "R-JD-NR-WINDOW-EXPIRED-001", "DENY", "NO_REASON_WINDOW_EXPIRED",
     delivered="2026-10-01T23:50:00+08:00", claim="2026-10-08T00:05:00+08:00", note="跨零点即进入第 8 天")
case("POL-D06", "时间边界", "RETURN_REFUND", "R-JD-NR-WINDOW-OK-001", "ALLOW", "NO_REASON_WITHIN_7D",
     delivered="2026-10-02T00:05:00+08:00", claim="2026-10-08T23:55:00+08:00", note="签收在 0 点后，第 7 天")
case("POL-D07", "时间边界", "RETURN_REFUND", "R-JD-NR-WINDOW-OK-001", "ALLOW", "NO_REASON_WITHIN_7D",
     delivered="2026-09-30T16:30:00+00:00", claim="2026-10-07T15:00:00+00:00",
     note="UTC 输入换算东八区后为第 7 天（10-01 00:30 签收）")
case("POL-D08", "时间边界", "RETURN_REFUND", "R-JD-NR-WINDOW-EXPIRED-001", "DENY", "NO_REASON_WINDOW_EXPIRED",
     delivered="2026-09-30T15:30:00+00:00", claim="2026-10-07T15:00:00+00:00",
     note="UTC 输入换算东八区后为第 8 天（09-30 23:30 签收）")
case("POL-D09", "时间边界", "RESHIPMENT", "R-JD-BAKERY-WINDOW-OK-001", "ALLOW", "BAKERY_EXCHANGE_WITHIN_15D",
     category="BAKERY", reason_code="DATE_DISSATISFIED",
     delivered="2026-10-01T00:00:00+08:00", claim="2026-10-15T23:59:00+08:00", note="烘焙次日口径第 14 天")
case("POL-D10", "时间边界", "RESHIPMENT", "R-JD-BAKERY-WINDOW-OK-001", "ALLOW", "BAKERY_EXCHANGE_WITHIN_15D",
     category="BAKERY", reason_code="DATE_DISSATISFIED",
     delivered="2026-10-01T00:00:00+08:00", claim="2026-10-16T00:00:00+08:00", note="烘焙次日口径第 15 天 0 点")
case("POL-D11", "时间边界", "RESHIPMENT", "R-JD-BAKERY-WINDOW-OK-001", "ALLOW", "BAKERY_EXCHANGE_WITHIN_15D",
     category="BAKERY", reason_code="DATE_DISSATISFIED",
     delivered="2026-10-01T23:50:00+08:00", claim="2026-10-16T23:55:00+08:00",
     note="烘焙次日口径第 15 天（若按签收日口径会误判为第 16 天）")
case("POL-D12", "时间边界", "RESHIPMENT", "R-JD-BAKERY-WINDOW-EXPIRED-001", "DENY", "BAKERY_EXCHANGE_WINDOW_EXPIRED",
     category="BAKERY", reason_code="DATE_DISSATISFIED",
     delivered="2026-10-01T00:00:00+08:00", claim="2026-10-17T00:00:00+08:00", note="烘焙次日口径第 16 天")
case("POL-D13", "时间边界", "RESHIPMENT", "R-JD-BAKERY-WINDOW-EXPIRED-001", "DENY", "BAKERY_EXCHANGE_WINDOW_EXPIRED",
     category="BAKERY", reason_code="DATE_DISSATISFIED",
     delivered="2026-10-01T23:50:00+08:00", claim="2026-10-17T00:05:00+08:00", note="烘焙跨零点进入第 16 天")
case("POL-D14", "时间边界", "RESHIPMENT", "R-JD-BAKERY-WINDOW-OK-001", "ALLOW", "BAKERY_EXCHANGE_WITHIN_15D",
     category="BAKERY", reason_code="DATE_DISSATISFIED",
     delivered="2026-10-01T23:50:00+08:00", claim="2026-10-16T23:59:59+08:00", note="烘焙第 15 天 23:59:59")
case("POL-D15", "时间边界", "RETURN_REFUND", "R-JD-NR-WINDOW-OK-001", "ALLOW", "NO_REASON_WITHIN_7D",
     delivered="2026-10-10T08:00:00+08:00", claim="2026-10-10T23:59:00+08:00", note="第 1 天深夜申请")
case("POL-D16", "时间边界", "RETURN_REFUND", "R-JD-NR-WINDOW-OK-001", "ALLOW", "NO_REASON_WITHIN_7D",
     delivered="2026-10-10T23:59:00+08:00", claim="2026-10-11T00:00:00+08:00", note="跨零点申请，第 2 天")
case("POL-D17", "时间边界", "RETURN_REFUND", "R-JD-NR-WINDOW-OK-001", "ALLOW", "NO_REASON_WITHIN_7D",
     delivered="2026-10-09T00:00:00+08:00", claim="2026-10-15T23:59:59+08:00", note="第 7 天最后一秒")
case("POL-D18", "时间边界", "RETURN_REFUND", "R-JD-NR-WINDOW-EXPIRED-001", "DENY", "NO_REASON_WINDOW_EXPIRED",
     delivered="2026-10-09T00:00:00+08:00", claim="2026-10-16T00:00:00+08:00", note="第 8 天第一秒")
case("POL-D19", "时间边界", "RESHIPMENT", "R-JD-BAKERY-WINDOW-EXPIRED-001", "DENY", "BAKERY_EXCHANGE_WINDOW_EXPIRED",
     category="BAKERY", reason_code="DATE_DISSATISFIED",
     delivered="2026-10-01T00:00:00+08:00", claim="2026-10-17T00:00:01+08:00", note="烘焙第 16 天第一秒")
case("POL-D20", "时间边界", "RETURN_REFUND", "R-JD-NR-WINDOW-OK-001", "ALLOW", "NO_REASON_WITHIN_7D",
     delivered="2026-10-09T16:00:00+00:00", claim="2026-10-16T15:59:00+00:00",
     note="UTC 输入：东八区 10-10 00:00 签收 / 10-16 23:59 申请，第 7 天")

# =========================================================================== #
# E. 金额与数量边界（15）
# =========================================================================== #
case("POL-E01", "金额与数量", "COMPLAINT", "R-JD-COMP-LATE-001", "ALLOW", "LATE_DELIVERY_COMPENSATION",
     reason_code="LATE_DELIVERY", amount=1999, note="阈值下 1 分")
case("POL-E02", "金额与数量", "COMPLAINT", "R-JD-COMP-LATE-001", "ALLOW", "LATE_DELIVERY_COMPENSATION",
     reason_code="LATE_DELIVERY", amount=2000, note="阈值上界（含）")
case("POL-E03", "金额与数量", "COMPLAINT", "R-JD-COMP-LATE-001", "MANUAL", "AMOUNT_EXCEEDS_AUTO_THRESHOLD",
     reason_code="LATE_DELIVERY", amount=2001, note="超阈值 1 分：有资格但需人工审批")
case("POL-E04", "金额与数量", "COMPLAINT", "R-JD-COMP-LATE-001", "MANUAL", "AMOUNT_EXCEEDS_AUTO_THRESHOLD",
     reason_code="LATE_DELIVERY", amount=100000, note="大额赔付")
case("POL-E05", "金额与数量", "COMPLAINT", "R-JD-COMP-LATE-001", "ALLOW", "LATE_DELIVERY_COMPENSATION",
     reason_code="LATE_DELIVERY", amount=0, note="金额为 0（政策未设下限）")
case("POL-E06", "金额与数量", "COMPLAINT", "R-JD-COMP-OOS-001", "ALLOW", "OUT_OF_STOCK_COMPENSATION",
     reason_code="OUT_OF_STOCK", amount=2000, note="缺货赔付阈值上界")
case("POL-E07", "金额与数量", "COMPLAINT", "R-JD-COMP-OOS-001", "MANUAL", "AMOUNT_EXCEEDS_AUTO_THRESHOLD",
     reason_code="OUT_OF_STOCK", amount=2001, note="缺货赔付超阈值")
case("POL-E08", "金额与数量", "COMPLAINT", "R-JD-COMP-OOS-001", "MANUAL", "AMOUNT_EXCEEDS_AUTO_THRESHOLD",
     reason_code="FALSE_SHIPMENT", amount=5000, note="虚假发货超阈值")
case("POL-E09", "金额与数量", "COMPLAINT", "R-JD-COMP-LATE-001", "ALLOW", "LATE_DELIVERY_COMPENSATION",
     reason_code="LATE_DELIVERY", amount=None, execution_blocked=True, note="金额缺失：阻断执行")
case("POL-E10", "金额与数量", "COMPLAINT", "R-JD-COMP-OOS-001", "ALLOW", "OUT_OF_STOCK_COMPENSATION",
     reason_code="OUT_OF_STOCK", amount=None, execution_blocked=True, note="金额缺失（缺货）")
case("POL-E11", "金额与数量", "RETURN_REFUND", "R-JD-Q-RETURN-001", "ALLOW", "QUALITY_RETURN_WINDOW",
     reason_code="QUALITY_DEFECT", quantity=1, note="数量 1")
case("POL-E12", "金额与数量", "RETURN_REFUND", "R-JD-Q-RETURN-001", "ALLOW", "QUALITY_RETURN_WINDOW",
     reason_code="QUALITY_DEFECT", quantity=3, note="数量 3：205 条真实条款无按数量改判的规则")
case("POL-E13", "金额与数量", "RETURN_REFUND", "R-JD-Q-RETURN-001", "ALLOW", "QUALITY_RETURN_WINDOW",
     reason_code="QUALITY_DEFECT", quantity=999, note="极端数量 999：验证不存在隐式数量上限")
case("POL-E14", "金额与数量", "RETURN_REFUND", "R-JD-Q-RETURN-001", "ALLOW", "QUALITY_RETURN_WINDOW",
     reason_code="QUALITY_DEFECT", quantity=None, note="数量缺失：当前政策不依赖数量，不阻断")
case("POL-E15", "金额与数量", "RETURN_REFUND", "R-JD-NR-WINDOW-OK-001", "ALLOW", "NO_REASON_WITHIN_7D",
     quantity=2, note="无理由退货带数量 2")

# =========================================================================== #
# F. 特殊例外及冲突（20）
# =========================================================================== #
case("POL-F01", "特殊例外", "RETURN_REFUND", "R-JD-NR-DENY-FRESH-001", "DENY", "NO_REASON_FRESH_EXEMPT",
     category="FRESH", delivered="2026-10-10T10:00:00+08:00", claim="2026-10-11T10:00:00+08:00",
     note="例外优先于窗口允许（第 2 天）")
case("POL-F02", "特殊例外", "RETURN_REFUND", "R-JD-NR-DENY-CUSTOM-001", "DENY", "NO_REASON_CUSTOM_EXEMPT",
     category="CUSTOM", delivered="2026-10-10T10:00:00+08:00", claim="2026-10-10T18:00:00+08:00",
     note="定制第 1 天也不适用无理由")
case("POL-F03", "特殊例外", "RETURN_REFUND", "R-JD-NR-DENY-DIGITAL-001", "DENY", "NO_REASON_DIGITAL_EXEMPT",
     category="DIGITAL", reason_code="SIZE_ISSUE", delivered="2026-10-08T10:00:00+08:00",
     claim="2026-10-11T10:00:00+08:00", note="数码第 4 天")
case("POL-F04", "特殊例外", "RETURN_REFUND", "R-JD-NR-DENY-SEALED-001", "DENY", "NO_REASON_SEALED_EXEMPT",
     category="BOOK_MEDIA", delivered="2026-10-07T10:00:00+08:00", claim="2026-10-11T10:00:00+08:00",
     note="图书第 5 天")
case("POL-F05", "特殊例外", "RETURN_REFUND", "R-JD-CATEGORY-UNKNOWN-001", "MANUAL", "CATEGORY_UNKNOWN_REVIEW",
     category="UNKNOWN", delivered="2026-10-09T10:00:00+08:00", claim="2026-10-11T10:00:00+08:00",
     note="未知品类压过通用允许")
case("POL-F06", "特殊例外", "RETURN_REFUND", "R-JD-CATEGORY-UNKNOWN-001", "MANUAL", "CATEGORY_UNKNOWN_REVIEW",
     category="UNKNOWN", reason_code="QUALITY_DEFECT", note="未知品类压过质量问题允许")
case("POL-F07", "特殊例外", "REFUND_ONLY", "R-JD-CANCEL-SPECIAL-REVIEW-001", "MANUAL", "SPECIAL_CANCEL_REVIEW",
     status="PAID_UNSHIPPED", parcel="UNKNOWN", category="FRESH", delivered=None, claim=None,
     as_of="2026-10-13T10:00:00+08:00", note="生鲜未发货取消：需人工，不能直接同意")
case("POL-F08", "特殊例外", "REFUND_ONLY", "R-JD-CANCEL-SPECIAL-REVIEW-001", "MANUAL", "SPECIAL_CANCEL_REVIEW",
     status="PAID_UNSHIPPED", parcel="UNKNOWN", category="CUSTOM", delivered=None, claim=None,
     as_of="2026-10-13T10:00:00+08:00", note="定制未发货取消")
case("POL-F09", "特殊例外", "REFUND_ONLY", "R-JD-CANCEL-SPECIAL-REVIEW-001", "MANUAL", "SPECIAL_CANCEL_REVIEW",
     status="PAID_UNSHIPPED", parcel="UNKNOWN", category="VIRTUAL", delivered=None, claim=None,
     as_of="2026-10-13T10:00:00+08:00", note="虚拟商品未发货取消")
case("POL-F10", "特殊例外", "REFUND_ONLY", "R-JD-CANCEL-UNSHIPPED-001", "ALLOW", "UNSHIPPED_CANCEL_REFUND",
     status="PAID_UNSHIPPED", parcel="UNKNOWN", delivered=None, claim=None,
     as_of="2026-10-13T10:00:00+08:00", note="对照组：普通品类未发货取消直接允许")
case("POL-F11", "特殊例外", "RESHIPMENT", "R-JD-BAKERY-WINDOW-OK-001", "ALLOW", "BAKERY_EXCHANGE_WITHIN_15D",
     category="BAKERY", reason_code="DATE_DISSATISFIED",
     delivered="2026-10-01T10:00:00+08:00", claim="2026-10-16T10:00:00+08:00",
     note="明确窗口允许压过兜底人工")
case("POL-F12", "特殊例外", "RESHIPMENT", "R-JD-BAKERY-WINDOW-EXPIRED-001", "DENY", "BAKERY_EXCHANGE_WINDOW_EXPIRED",
     category="BAKERY", reason_code="DATE_DISSATISFIED",
     delivered="2026-10-01T10:00:00+08:00", claim="2026-10-17T10:00:00+08:00",
     note="窗口外拒绝")
case("POL-F13", "特殊例外", "REFUND_ONLY", "R-JD-COMP-SIGN-ABNORMAL-001", "MANUAL", "SIGN_ABNORMAL_REVIEW",
     status="SHIPPED", reason_code="MISSING_ITEM", note="少件且未签收：转人工核实")
case("POL-F14", "特殊例外", "REFUND_ONLY", "R-JD-COMP-SIGN-ABNORMAL-001", "MANUAL", "SIGN_ABNORMAL_REVIEW",
     status="SHIPPED", reason_code="WRONG_ITEM", note="错件且未签收")
case("POL-F15", "特殊例外", "RETURN_REFUND", "R-JD-Q-RETURN-001", "ALLOW", "QUALITY_RETURN_WINDOW",
     status="DELIVERED", reason_code="MISSING_ITEM", note="已签收少件：通用质量问题允许压过兜底人工")
case("POL-F16", "特殊例外", "RETURN_REFUND", "R-JD-GOLD-WEIGHT-001", "ALLOW", "GOLD_WEIGHT_COMPENSATION",
     category="GOLD", reason_code="WEIGHT_DIFF", delivered=None, claim=None,
     as_of="2026-10-13T10:00:00+08:00", note="克重补差不依赖签收时效")
case("POL-F17", "特殊例外", "COMPLAINT", "R-JD-COMP-DELIVERY-ABNORMAL-001", "MANUAL", "DELIVERY_ABNORMAL_REVIEW",
     reason_code="DELIVERY_ABNORMAL", note="配送异常转人工")
case("POL-F18", "特殊例外", "COMPLAINT", "R-JD-INSTALL-DISPUTE-001", "MANUAL", "INSTALL_DISPUTE_REVIEW",
     reason_code="INSTALL_LATE", note="安装延误纠纷")
case("POL-F19", "特殊例外", "COMPLAINT", "R-JD-INSTALL-DISPUTE-001", "MANUAL", "INSTALL_DISPUTE_REVIEW",
     reason_code="INSTALL_FEE", category="APPLIANCE", note="安装乱收费")
case("POL-F20", "特殊例外", "RETURN_REFUND", "R-JD-EBIKE-3BAO-001", "ALLOW", "EBIKE_MOTOR_RETURN",
     category="EBIKE", reason_code="NOT_AS_DESCRIBED", delivered="2026-09-01T10:00:00+08:00",
     claim="2026-10-10T10:00:00+08:00", note="电动车三包不适用于 7 天无理由窗口")

# =========================================================================== #
# G. 品类归一化与规则版本（10）
# =========================================================================== #
case("POL-G01", "品类与版本", "RETURN_REFUND", "R-JD-NR-DENY-FRESH-001", "DENY", "NO_REASON_FRESH_EXEMPT",
     category="生鲜", note="中文别名归一：生鲜 -> FRESH")
case("POL-G02", "品类与版本", "RETURN_REFUND", "R-JD-NR-WINDOW-OK-001", "ALLOW", "NO_REASON_WITHIN_7D",
     category="apparel", note="小写英文别名归一：apparel -> APPAREL")
case("POL-G03", "品类与版本", "RETURN_REFUND", "R-JD-NR-DENY-SEALED-001", "DENY", "NO_REASON_SEALED_EXEMPT",
     category="图书", note="中文别名归一：图书 -> BOOK_MEDIA")
case("POL-G04", "品类与版本", "RETURN_REFUND", "R-JD-NR-DENY-CUSTOM-001", "DENY", "NO_REASON_CUSTOM_EXEMPT",
     category="定制", note="中文别名归一：定制 -> CUSTOM")
case("POL-G05", "品类与版本", "RESHIPMENT", "R-JD-BAKERY-WINDOW-OK-001", "ALLOW", "BAKERY_EXCHANGE_WITHIN_15D",
     category="烘焙", reason_code="DATE_DISSATISFIED", note="中文别名归一：烘焙 -> BAKERY")
case("POL-G06", "品类与版本", "COMPLAINT", "R-JD-GOLD-WEIGHT-001", "ALLOW", "GOLD_WEIGHT_COMPENSATION",
     category="黄金", reason_code="WEIGHT_DIFF", note="中文别名归一：黄金 -> GOLD")
case("POL-G07", "品类与版本", "RETURN_REFUND", "R-JD-EBIKE-3BAO-001", "ALLOW", "EBIKE_MOTOR_RETURN",
     category="电动车", reason_code="DAMAGED", note="中文别名归一：电动车 -> EBIKE")
case("POL-G08", "品类与版本", "RETURN_REFUND", "R-JD-CATEGORY-UNKNOWN-001", "MANUAL", "CATEGORY_UNKNOWN_REVIEW",
     category="unknown-thing", note="无法识别 -> UNKNOWN，fail-closed")
case("POL-G09", "品类与版本", "RETURN_REFUND", None, "MANUAL", "NO_APPLICABLE_POLICY",
     delivered="2025-12-20T10:00:00+08:00", claim="2025-12-31T10:00:00+08:00",
     as_of="2025-12-31T10:00:00+08:00", note="规则生效前（effective_from=2026-01-01）无适用规则")
case("POL-G10", "品类与版本", "RETURN_REFUND", "R-JD-NR-WINDOW-OK-001", "ALLOW", "NO_REASON_WITHIN_7D",
     delivered="2025-12-30T10:00:00+08:00", claim="2026-01-01T08:00:00+08:00",
     as_of="2026-01-01T08:00:00+08:00", note="生效边界时刻（effective_from=2026-01-01T00:00:00Z = 东八区 08:00）")


def main() -> int:
    ids = [c["case_id"] for c in CASES]
    assert len(ids) == len(set(ids)), "case_id 重复"
    assert len(CASES) == 120, f"用例数应为 120，实际 {len(CASES)}"
    OUT_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with io.open(OUT_JSONL, "w", encoding="utf-8", newline="\n") as f:
        for c in CASES:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    groups: dict[str, int] = {}
    for c in CASES:
        groups[c["group"]] = groups.get(c["group"], 0) + 1
    print("written", OUT_JSONL)
    print("groups:", groups)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())