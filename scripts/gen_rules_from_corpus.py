# -*- coding: utf-8 -*-
"""从真实语料（policy/clauses_jd.json）生成 policy/rules.json。

为什么用生成器而不是手写 JSON：
  1) 每条规则引用的 clause_id 都必须真实存在——生成时强校验，缺失即 exit 1（CI 断链检查）；
  2) 规则集（消费裁决）与非裁决清单（服务资格/商家合规）分开维护，避免把需要额外事实
     或不属于消费者裁决的条目塞进 PolicyEngine 的求值链；
  3) documents 直接来自真实语料，保证「规则 → 决策 → 依据条款」完全自洽。

用法：python scripts/gen_rules_from_corpus.py

产物：policy/rules.json（rules = 进入确定性引擎的规则；catalog = 非裁决清单，仅覆盖度审计）
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
CORPUS = ROOT / "policy" / "clauses_jd.json"
OUT = ROOT / "policy" / "rules.json"

VERSION = "2026.10-jd"
TENANT = "tenant_demo"
EFFECTIVE_FROM = "2026-01-01T00:00:00Z"

SUBJ = ["NO_LONGER_NEEDED", "SIZE_ISSUE", "COLOR_MISMATCH"]
QUAL = ["QUALITY_DEFECT", "DAMAGED", "WRONG_ITEM", "MISSING_ITEM", "NOT_AS_DESCRIBED"]


def C(fact, op, value):
    return {"fact": fact, "op": op, "value": value}


def R(rule_id, priority, decision, reason_code, when, clauses, cap=None, note=""):
    return {"rule_id": rule_id, "version": VERSION, "priority": priority, "when": when,
            "decision": decision, "reason_code": reason_code, "required_clause_ids": clauses,
            "amount_cap_minor": cap, "scope": {}, "status": "PUBLISHED",
            "effective_from": EFFECTIVE_FROM, "note": note}


RULES = [
    # ---------- DENY：7 天无理由法定豁免（依赖真实豁免清单条款）----------
    R("R-JD-NR-DENY-FRESH-001", 130, "DENY", "NO_REASON_FRESH_EXEMPT",
      [C("claim.reason_code", "in", SUBJ), C("order.category", "eq", "FRESH"),
       C("order.status", "eq", "DELIVERED")],
      ["CL-JD4268-T1"], note="鲜活易腐类不适用 7 天无理由"),
    R("R-JD-NR-DENY-CUSTOM-001", 130, "DENY", "NO_REASON_CUSTOM_EXEMPT",
      [C("claim.reason_code", "in", SUBJ), C("order.category", "eq", "CUSTOM"),
       C("order.status", "eq", "DELIVERED")],
      ["CL-JD4268-T1", "CL-JD4268-S5#3"], note="消费者定作不适用 7 天无理由"),
    R("R-JD-NR-DENY-DIGITAL-001", 130, "DENY", "NO_REASON_DIGITAL_EXEMPT",
      [C("claim.reason_code", "in", SUBJ),
       C("order.category", "in", ["DIGITAL", "APPLIANCE", "VIRTUAL"]),
       C("order.status", "eq", "DELIVERED")],
      ["CL-JD4268-T1#2", "CL-JD4268-S5"], note="拆封/激活数字化商品不适用 7 天无理由"),
    R("R-JD-NR-DENY-SEALED-001", 130, "DENY", "NO_REASON_SEALED_EXEMPT",
      [C("claim.reason_code", "in", SUBJ), C("order.category", "eq", "BOOK_MEDIA"),
       C("order.status", "eq", "DELIVERED")],
      ["CL-JD4268-S4#2", "CL-JD4268-S5"], note="一次性密封破损商品不适用 7 天无理由"),

    # ---------- DENY：已签收不可在途拦截 ----------
    R("R-JD-INTERCEPT-DELIVERED-DENY-001", 130, "DENY", "INTERCEPT_ALREADY_DELIVERED",
      [C("logistics.parcel_status", "in", ["DELIVERED", "SIGNED"]),
       C("intent", "eq", "INTERCEPT_REFUND")],
      ["CL-JD590903538377428992-S9"], note="已出库/已签收订单不适用拦截"),

    # ---------- ALLOW：品类专项（优先级高于通用质量规则，否则永不命中）----------
    R("R-JD-GOLD-WEIGHT-001", 118, "ALLOW", "GOLD_WEIGHT_COMPENSATION",
      [C("order.category", "in", ["GOLD", "JEWELRY"]), C("claim.reason_code", "eq", "WEIGHT_DIFF")],
      ["CL-JD1013329543295012864-CH1", "CL-JD1013329543295012864-S4"],
      note="计价黄金克重补差（以签收克重与页面标识克重比对）"),
    R("R-JD-GOLD-QUALITY-001", 116, "ALLOW", "GOLD_QUALITY_RETURN",
      [C("order.category", "in", ["GOLD", "JEWELRY"]), C("claim.reason_code", "in", QUAL)],
      ["CL-JD1013329543295012864-CH3"], note="贵金属质量问题的退换修"),
    R("R-JD-EBIKE-3BAO-001", 114, "ALLOW", "EBIKE_MOTOR_RETURN",
      [C("order.category", "eq", "EBIKE"), C("claim.reason_code", "in", QUAL)],
      ["CL-JD3836-CH2", "CL-JD3836-CH2#2"], note="电动车类目三包（电机等主要部件）"),

    # ---------- ALLOW：通用质量 / 无理由 / 取消 / 拦截 / 履约赔付 ----------
    R("R-JD-Q-RETURN-001", 110, "ALLOW", "QUALITY_RETURN_WINDOW",
      [C("claim.reason_code", "in", QUAL), C("order.status", "eq", "DELIVERED")],
      ["CL-JD2595-S16", "CL-JD4268-A5"], note="签收后质量问题退换修"),
    R("R-JD-Q-REPAIR-001", 105, "ALLOW", "QUALITY_REPAIR",
      [C("claim.reason_code", "in", QUAL), C("intent", "eq", "RETURN_REFUND")],
      ["CL-JD2595-S15", "CL-JD2595-S16"], note="质量问题维修（返修）"),
    R("R-JD-NR-ALLOW-001", 100, "ALLOW", "NO_REASON_RETURN_OK",
      [C("claim.reason_code", "in", SUBJ), C("order.status", "eq", "DELIVERED")],
      ["CL-JD4268-A3", "CL-JD4268-A5", "CL-JD4268-S4"], note="7 天无理由退货（未被豁免品类）"),
    R("R-JD-CANCEL-UNSHIPPED-001", 100, "ALLOW", "UNSHIPPED_CANCEL_REFUND",
      [C("order.status", "eq", "PAID_UNSHIPPED"), C("intent", "eq", "REFUND_ONLY")],
      ["CL-JD590903538377428992-S3#2", "CL-JD590903538377428992-S3"], note="未出库订单取消退款"),
    R("R-JD-INTERCEPT-INTRANSIT-001", 100, "ALLOW", "INTERCEPT_OK",
      [C("logistics.parcel_status", "eq", "IN_TRANSIT"), C("intent", "eq", "INTERCEPT_REFUND")],
      ["CL-JD590903538377428992-A9#2"], note="在途包裹拦截"),
    R("R-JD-COMP-LATE-001", 96, "ALLOW", "LATE_DELIVERY_COMPENSATION",
      [C("claim.reason_code", "eq", "LATE_DELIVERY")],
      ["CL-JD1190896203588571136-S13", "CL-JD1190896203588571136-S20"], cap=2000,
      note="延迟发货违约金（封顶 20 元）"),
    R("R-JD-COMP-OOS-001", 96, "ALLOW", "OUT_OF_STOCK_COMPENSATION",
      [C("claim.reason_code", "in", ["OUT_OF_STOCK", "FALSE_SHIPMENT"])],
      ["CL-JD1190896203588571136-S17", "CL-JD1190896203588571136-S20"], cap=2000,
      note="缺货/虚假发货违约金（封顶 20 元）"),

    # ---------- MANUAL：需人工复核 ----------
    R("R-JD-CANCEL-SPECIAL-REVIEW-001", 105, "MANUAL", "SPECIAL_CANCEL_REVIEW",
      [C("order.status", "eq", "PAID_UNSHIPPED"), C("order.category", "in", ["FRESH", "CUSTOM", "VIRTUAL"])],
      ["CL-JD590903538377428992-A9", "CL-JD590903538377428992-S3#2"],
      note="生鲜/定制/虚拟等特殊类目未出库取消需人工确认"),
    R("R-JD-COMP-DELIVERY-ABNORMAL-001", 95, "MANUAL", "DELIVERY_ABNORMAL_REVIEW",
      [C("claim.reason_code", "eq", "DELIVERY_ABNORMAL")],
      ["CL-JD1190896203588571136-S14", "CL-JD1190896203588571136-S21"],
      note="配送/揽收异常需人工核实"),
    R("R-JD-INSTALL-DISPUTE-001", 95, "MANUAL", "INSTALL_DISPUTE_REVIEW",
      [C("claim.reason_code", "in", ["INSTALL_LATE", "INSTALL_FEE", "INSTALL_DEFECT"])],
      ["CL-JD3724-A5", "CL-JD3724-A7", "CL-JD3724-A9"], note="安装问题纠纷按责任判定"),
    R("R-JD-FRESH-BAKERY-001", 95, "MANUAL", "FRESH_BAKERY_EXCHANGE",
      [C("order.category", "eq", "BAKERY"), C("claim.reason_code", "eq", "DATE_DISSATISFIED")],
      ["CL-JD1148155761021554688-A1", "CL-JD1148155761021554688-S4"],
      note="新鲜烘焙类生产/签收日期不符，需人工核实后换货"),
    R("R-JD-COMP-SIGN-ABNORMAL-001", 90, "MANUAL", "SIGN_ABNORMAL_REVIEW",
      [C("claim.reason_code", "in", ["MISSING_ITEM", "WRONG_ITEM"])],
      ["CL-JD1190896203588571136-S18", "CL-JD1190896203588571136-S22"],
      note="签收异常（少件/错件）需人工核实后处理"),
]

CATALOG = [
    {"item_id": "R-JD-NR-DENY-INTACT-001", "decision": "DENY", "reason_code": "ITEM_NOT_INTACT",
     "required_clause_ids": ["CL-JD4268-S5#4"],
     "note": "吊牌/防伪标识被拆除、穿戴污损的服饰不支持 7 天无理由；需新增事实 order.item_intact 才能自动判定"},
    {"item_id": "R-JD-FLASH-REFUND-001", "decision": "MANUAL", "reason_code": "FLASH_REFUND_ELIGIBILITY",
     "required_clause_ids": ["CL-JD616214789504503808-CH3", "CL-JD616214789504503808-S4"],
     "note": "闪电退款为服务资格，需实付金额/信誉/服务标识，v1 不自动判定"},
    {"item_id": "R-JD-DOOR-EXCHANGE-001", "decision": "MANUAL", "reason_code": "DOOR_EXCHANGE_ELIGIBILITY",
     "required_clause_ids": ["CL-JD845914392943005696-CH3", "CL-JD845914392943005696-S3"],
     "note": "上门换新为服务资格，需服务标识/信誉/金额上限"},
    {"item_id": "R-JD-REPLACE-ONLY-001", "decision": "MANUAL", "reason_code": "REPLACE_ONLY_ELIGIBILITY",
     "required_clause_ids": ["CL-JD1023830024685490176-A1", "CL-JD1023830024685490176-S7"],
     "note": "180 天只换不修需订单完成天数（第 8-180 天）与类目"},
    {"item_id": "R-JD-EXT-WARRANTY-001", "decision": "MANUAL", "reason_code": "EXTENDED_WARRANTY_ELIGIBILITY",
     "required_clause_ids": ["CL-JD1023834451186159616-A1", "CL-JD1023834451186159616-A2"],
     "note": "电器延保需延保期与服务标识"},
    {"item_id": "R-JD-HOME-REPAIR-001", "decision": "MANUAL", "reason_code": "HOME_REPAIR_SERVICE_REVIEW",
     "required_clause_ids": ["CL-JD1189815359696408576-S3", "CL-JD1189815359696408576-S12"],
     "note": "家电家居维修服务纠纷按类目服务规范处理"},
    {"item_id": "R-JD-FREIGHT-BUYER-001", "decision": "ADVISORY", "reason_code": "FREIGHT_BUYER_PAYS",
     "required_clause_ids": ["CL-JD4268-A6", "CL-JD3727-A3#2"],
     "note": "无理由退货的运费由消费者承担（非资金裁决，随回复说明）"},
    {"item_id": "R-JD-FREIGHT-SELLER-001", "decision": "ADVISORY", "reason_code": "FREIGHT_SELLER_PAYS",
     "required_clause_ids": ["CL-JD3727-CH1", "CL-JD3727-A3#2", "CL-JD3727-A9"],
     "note": "质量类/商家过错场景运费由商家承担（非资金裁决）"},
    {"item_id": "R-JD-COMPLIANCE-PRIVACY-001", "decision": "MANUAL", "reason_code": "PRIVACY_SHIPPING_REQUIRED",
     "required_clause_ids": ["CL-JD1244115246973587456-A1"], "note": "隐私发货合规约束（对商家）"},
    {"item_id": "R-JD-COMPLIANCE-STOCK-001", "decision": "MANUAL", "reason_code": "MALICIOUS_STOCK_HOLD",
     "required_clause_ids": ["CL-JD1206154529616302080-S1", "CL-JD1206154529616302080-S2"],
     "note": "恶意占库存违规处罚（对商家）"},
    {"item_id": "R-JD-COMPLIANCE-RESALE-001", "decision": "MANUAL", "reason_code": "MALICIOUS_RESALE",
     "required_clause_ids": ["CL-JD902003809503547392-CH2"], "note": "恶意倒卖纠纷处理（对商家）"},
]


def main() -> int:
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    known = {c["clause_id"] for c in corpus["clauses"]}
    known |= {c["logical_clause_id"] for c in corpus["clauses"] if c.get("logical_clause_id")}

    missing = []
    for r in RULES:
        missing += [c for c in r["required_clause_ids"] if c not in known]
    for it in CATALOG:
        missing += [c for c in it["required_clause_ids"] if c not in known]
    if missing:
        print("[FATAL] 规则引用了不存在的条款：")
        for m in sorted(set(missing)):
            print("   -", m)
        return 1

    payload = {
        "version": VERSION,
        "tenant": TENANT,
        "corpus": "京东开放平台官方规则（17 份 PDF，见 policy/clauses_jd.json）",
        "note": ("真实平台语料为唯一事实源；决策由确定性引擎求出，不调用 LLM。"
                 "rules=进入 PolicyEngine 的消费者裁决规则；catalog=非裁决清单（服务资格/商家合规），"
                 "仅用于覆盖度审计，不参与求值。语料确无对应政策的诉求（生鲜破损赔付/服饰质量/图书质量/"
                 "预售/价保）不设规则→由 NO_APPLICABLE_POLICY 转人工。"),
        "documents": [{"doc_id": d["doc_id"], "title": d["title"], "version": d.get("version", "v1")}
                      for d in corpus["documents"]],
        "rules": RULES,
        "catalog": CATALOG,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT}  rules={len(RULES)}  catalog={len(CATALOG)}  docs={len(payload['documents'])}")
    print("clause links verified against", CORPUS.name, f"({len(known)} known clause ids)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())