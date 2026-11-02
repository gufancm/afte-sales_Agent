# -*- coding: utf-8 -*-
"""独立留出集（holdout）v1：40 条**未参与规则开发**的边界场景。

与 120 条主集的区别：
- 主集覆盖每条规则的正/负/边界；本集只挑“政策原文能推出、但主集没用过的组合”；
- 期望值在运行引擎**之前**按条款写死（`basis` 字段写明依据），运行后不改金标；
- 每个用例都记录它相对主集“新在哪”（`novelty`），便于人工审查独立性。
"""
from __future__ import annotations

import io
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.policy.library import load_rules  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parent / "data" / "policy_holdout_v1.jsonl"
RULES = {r.rule_id: r for r in load_rules()}
CASES: list[dict] = []


def case(cid, group, intent, rule_ref, decision, reason, basis, novelty, *,
         delivered="2026-10-05T10:00:00+08:00", claim="2026-10-11T10:00:00+08:00",
         category="APPAREL", status="DELIVERED", reason_code="NO_LONGER_NEEDED",
         parcel="SIGNED", amount=None, quantity=None, as_of=None,
         clause_ids=None, execution_blocked=False):
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
        "case_id": cid, "group": group, "intent": intent, "rule_ref": rule_ref,
        "facts": {"order": order, "logistics": {"parcel_status": parcel}, "finance": finance,
                  "claim": claim_facts, "intent": intent},
        "as_of": as_of or claim or "2026-10-11T10:00:00+08:00",
        "expect": {"decision": decision, "reason_code": reason, "rule_id": rule_ref,
                   "required_clause_ids": clause_ids,
                   "execution_blocked": execution_blocked},
        "basis": basis, "novelty": novelty, "review": "pending",
    })


# ---------------- 允许路径（12） ----------------
case("HV-01", "允许", "RETURN_REFUND", "R-JD-Q-RETURN-001", "ALLOW", "QUALITY_RETURN_WINDOW",
     "CL-JD2595-S16 + CL-JD4268-A5：质量问题签收后可退，未排除食品类",
     "主集质量类只用过 APPAREL/FRESH/BOOK_MEDIA/CUSTOM，未用 GROCERY",
     category="GROCERY", reason_code="DAMAGED",
     delivered="2026-10-09T10:00:00+08:00", claim="2026-10-11T10:00:00+08:00")
case("HV-02", "允许", "RETURN_REFUND", "R-JD-Q-REPAIR-001", "ALLOW", "QUALITY_REPAIR",
     "CL-JD2595-S15/S16：未签收阶段的质量问题走维修/换货，不要求已签收",
     "主集 Q-REPAIR 只在 APPAREL+PAID_SHIPPED 验证过一次",
     category="GROCERY", reason_code="NOT_AS_DESCRIBED", status="SHIPPED", parcel="IN_TRANSIT")
case("HV-03", "允许", "RETURN_REFUND", "R-JD-Q-RETURN-001", "ALLOW", "QUALITY_RETURN_WINDOW",
     "CL-JD4268-T1#2：虚拟商品仅限制“无理由”，质量问题仍在质量条款内",
     "主集虚拟品类只验证了无理由被拒，未验证质量路径放行",
     category="VIRTUAL", reason_code="QUALITY_DEFECT")
case("HV-04", "允许", "REFUND_ONLY", "R-JD-Q-RETURN-001", "ALLOW", "QUALITY_RETURN_WINDOW",
     "CL-JD4268-T1#2：家电仅限制无理由退货，不影响质量问题",
     "主集家电只用过无理由例外与安装纠纷，未验证质量退货",
     category="APPLIANCE", reason_code="DAMAGED")
case("HV-05", "允许", "RETURN_REFUND", "R-JD-Q-RETURN-001", "ALLOW", "QUALITY_RETURN_WINDOW",
     "CL-JD4268-S4#2：拆封仅影响“无理由”，质量问题仍可退",
     "主集图书只验证了拆封无理由例外，未验证质量问题",
     category="BOOK_MEDIA", reason_code="DAMAGED")
case("HV-06", "允许", "RETURN_REFUND", "R-JD-Q-RETURN-001", "ALLOW", "QUALITY_RETURN_WINDOW",
     "CL-JD1190896203588571136-S18：错件属签收异常，但已签收且要求退货时走质量退货",
     "主集错件只在未签收 + REFUND_ONLY 下验证过；此处验证已签收 + 退货",
     category="FRESH", reason_code="WRONG_ITEM")
case("HV-07", "允许", "COMPLAINT", "R-JD-GOLD-WEIGHT-001", "ALLOW", "GOLD_WEIGHT_COMPENSATION",
     "CL-JD1013329543295012864-CH1/S4：克重补差以“自称重与标识不符”为条件，不含签收时效",
     "主集克重补差都带签收时间；此处验证无时间事实时仍应放行（不依赖时效）",
     category="JEWELRY", reason_code="WEIGHT_DIFF", delivered=None, claim=None,
     as_of="2026-10-11T10:00:00+08:00")
case("HV-08", "允许", "RETURN_REFUND", "R-JD-GOLD-QUALITY-001", "ALLOW", "GOLD_QUALITY_RETURN",
     "CL-JD1013329543295012864-CH3：计价黄金质量问题，不限订单状态",
     "主集黄金质量只验证 DELIVERED；此处验证未签收(PAID_SHIPPED)仍命中",
     category="GOLD", reason_code="NOT_AS_DESCRIBED", status="PAID_SHIPPED", parcel="UNKNOWN")
case("HV-09", "允许", "RETURN_REFUND", "R-JD-EBIKE-3BAO-001", "ALLOW", "EBIKE_MOTOR_RETURN",
     "CL-JD3836-CH2：电动车三包按整车/电池/电机分档，质量问题适用",
     "主集电动车只用过 DAMAGED/NOT_AS_DESCRIBED；此处用 WRONG_ITEM",
     category="EBIKE", reason_code="WRONG_ITEM")
case("HV-10", "允许", "COMPLAINT", "R-JD-COMP-LATE-001", "ALLOW", "LATE_DELIVERY_COMPENSATION",
     "CL-JD1190896203588571136-S20：超时 3 天内扣 6 元/单，1 分钱申请不超上限",
     "主集金额最小 0/1999；此处验证 1 分的极小值",
     reason_code="LATE_DELIVERY", amount=1)
case("HV-11", "允许", "COMPLAINT", "R-JD-COMP-OOS-001", "ALLOW", "OUT_OF_STOCK_COMPENSATION",
     "CL-JD1190896203588571136-S17/S20：缺货赔付按单，1999 分未超自动执行阈值",
     "主集缺货只用 1000/2000/2001；此处用 1999（阈值下 1 分）",
     category="GROCERY", reason_code="OUT_OF_STOCK", amount=1999)
case("HV-12", "允许", "RETURN_REFUND", "R-JD-NR-WINDOW-OK-001", "ALLOW", "NO_REASON_WITHIN_7D",
     "CL-JD4268-A3：自签收之日起 7 个自然日内",
     "主集第 7 天边界只用 APPAREL；此处验证 GROCERY（非例外品类）第 7 天",
     category="GROCERY", delivered="2026-10-05T00:00:00+08:00", claim="2026-10-11T23:59:00+08:00")

# ---------------- 拒绝路径（10） ----------------
case("HV-13", "拒绝", "RETURN_REFUND", "R-JD-NR-DENY-DIGITAL-001", "DENY", "NO_REASON_DIGITAL_EXEMPT",
     "CL-JD4268-T1#2：虚拟类不适用 7 天无理由（与是否超期无关）",
     "主集虚拟无理由在第 1 天；此处第 9 天，验证例外优先于超期两条拒绝规则",
     category="VIRTUAL", reason_code="SIZE_ISSUE",
     delivered="2026-10-09T10:00:00+08:00", claim="2026-10-17T10:00:00+08:00")
case("HV-14", "拒绝", "RETURN_REFUND", "R-JD-NR-DENY-DIGITAL-001", "DENY", "NO_REASON_DIGITAL_EXEMPT",
     "CL-JD4268-T1#2：已联网/激活类不适用无理由（家电同样归入该例外档）",
     "主集家电用 SIZE_ISSUE 在第 3 天；此处 COLOR_MISMATCH 第 6 天（仍在 7 天内）",
     category="APPLIANCE", reason_code="COLOR_MISMATCH",
     delivered="2026-10-05T10:00:00+08:00", claim="2026-10-10T10:00:00+08:00")
case("HV-15", "拒绝", "RETURN_REFUND", "R-JD-NR-WINDOW-EXPIRED-001", "DENY", "NO_REASON_WINDOW_EXPIRED",
     "CL-JD4268-A3：7 个自然日，第 8 天起不再适用",
     "主集第 8 天用 APPAREL；此处用 GROCERY 且时间点为 00:00:00",
     category="GROCERY", delivered="2026-10-04T00:00:00+08:00", claim="2026-10-11T00:00:00+08:00")
case("HV-16", "拒绝", "RETURN_REFUND", "R-JD-NR-DENY-SEALED-001", "DENY", "NO_REASON_SEALED_EXEMPT",
     "CL-JD4268-S4#2：图书音像拆封后不适用无理由",
     "主集图书用 NO_LONGER_NEEDED/COLOR_MISMATCH；此处用 SIZE_ISSUE 第 2 天",
     category="BOOK_MEDIA", reason_code="SIZE_ISSUE",
     delivered="2026-10-09T10:00:00+08:00", claim="2026-10-10T10:00:00+08:00")
case("HV-17", "拒绝", "RETURN_REFUND", "R-JD-NR-DENY-CUSTOM-001", "DENY", "NO_REASON_CUSTOM_EXEMPT",
     "CL-JD4268-T1：定做类商品不适用 7 天无理由",
     "主集定制用第 2/7 天；此处签收当日申请（第 1 天）",
     category="CUSTOM", delivered="2026-10-10T10:00:00+08:00", claim="2026-10-10T20:00:00+08:00")
case("HV-18", "拒绝", "RESHIPMENT", "R-JD-BAKERY-WINDOW-EXPIRED-001", "DENY", "BAKERY_EXCHANGE_WINDOW_EXPIRED",
     "CL-JD1148155761021554688-S4：签收次日 0 点起算 15 个自然日",
     "主集烘焙超期用 16/30/61 天；此处第 19 天且跨月",
     category="BAKERY", reason_code="DATE_DISSATISFIED",
     delivered="2026-09-01T10:00:00+08:00", claim="2026-09-20T10:00:00+08:00")
case("HV-19", "拒绝", "INTERCEPT_REFUND", "R-JD-INTERCEPT-DELIVERED-DENY-001", "DENY", "INTERCEPT_ALREADY_DELIVERED",
     "CL-JD590903538377428992-S5 第 3 项：仅在消费者签收前才通知承运方拦截",
     "主集已签收拦截用 parcel=DELIVERED；此处 parcel=SIGNED 且订单仍是 DELIVERED，同时命中 7 天允许",
     parcel="SIGNED", status="DELIVERED", delivered="2026-10-09T10:00:00+08:00",
     claim="2026-10-11T10:00:00+08:00")
case("HV-20", "拒绝", "INTERCEPT_REFUND", "R-JD-INTERCEPT-DELIVERED-DENY-001", "DENY", "INTERCEPT_ALREADY_DELIVERED",
     "同上：物流已妥投，退款应走售后而非拦截",
     "主集此规则只用 DELIVERED 订单状态；此处订单 SHIPPED + 物流 DELIVERED",
     category="GROCERY", status="SHIPPED", parcel="DELIVERED")
case("HV-21", "拒绝", "RETURN_REFUND", "R-JD-NR-WINDOW-EXPIRED-001", "DENY", "NO_REASON_WINDOW_EXPIRED",
     "CL-JD4268-A3：超期即不再适用无理由，金额大小不影响时效结论",
     "主集超期用例都不带金额；此处带 100 分，验证金额不改变政策结论",
     delivered="2026-10-02T10:00:00+08:00", claim="2026-10-10T10:00:00+08:00", amount=100)
case("HV-22", "拒绝", "RETURN_REFUND", "R-JD-NR-DENY-DIGITAL-001", "DENY", "NO_REASON_DIGITAL_EXEMPT",
     "CL-JD4268-T1#2：数码不适用无理由；第 8 天时两条拒绝规则同时命中，例外档优先",
     "主集第 8 天只对 APPAREL 验证；此处验证 DIGITAL+第 8 天的双拒绝冲突",
     category="DIGITAL", reason_code="SIZE_ISSUE",
     delivered="2026-10-09T10:00:00+08:00", claim="2026-10-16T10:00:00+08:00")

# ---------------- 时间边界（6） ----------------
case("HV-23", "时间边界", "RETURN_REFUND", "R-JD-NR-WINDOW-OK-001", "ALLOW", "NO_REASON_WITHIN_7D",
     "CL-JD4268-A3：第 6 天 23:59 仍在 7 个自然日内",
     "主集没有第 6 天末的用例",
     delivered="2026-10-04T12:00:00+08:00", claim="2026-10-09T23:59:00+08:00")
case("HV-24", "时间边界", "RETURN_REFUND", "R-JD-NR-WINDOW-OK-001", "ALLOW", "NO_REASON_WITHIN_7D",
     "CL-JD4268-A3：签收与申请都在 00:00:00，按自然日算第 2 天",
     "主集没有“两端都是零点”的用例",
     delivered="2026-10-06T00:00:00+08:00", claim="2026-10-07T00:00:00+08:00")
case("HV-25", "时间边界", "RESHIPMENT", "R-JD-BAKERY-WINDOW-OK-001", "ALLOW", "BAKERY_EXCHANGE_WITHIN_15D",
     "CL-JD1148155761021554688-S4：次日 0 点起算，第 14 天仍在窗口内",
     "主集烘焙边界集中在 15/16 天与第 3/10 天；此处第 14 天",
     category="BAKERY", reason_code="DATE_DISSATISFIED",
     delivered="2026-10-01T00:00:00+08:00", claim="2026-10-15T12:00:00+08:00")
case("HV-26", "时间边界", "RESHIPMENT", "R-JD-BAKERY-WINDOW-EXPIRED-001", "DENY", "BAKERY_EXCHANGE_WINDOW_EXPIRED",
     "CL-JD1148155761021554688-S4：次日 0 点起算第 16 天即超期",
     "主集第 16 天用例在 00:00/00:05；此处 11:59（同日不同时刻）",
     category="BAKERY", reason_code="DATE_DISSATISFIED",
     delivered="2026-10-01T12:00:00+08:00", claim="2026-10-17T11:59:00+08:00")
case("HV-32", "时间边界", "RESHIPMENT", "R-JD-BAKERY-WINDOW-OK-001", "ALLOW", "BAKERY_EXCHANGE_WITHIN_15D",
     "CL-JD1148155761021554688-S4：第 15 天 23:59:59 仍在窗口内（含临界秒）",
     "主集该秒点用于 7 天规则；此处用于烘焙 15 天规则",
     category="BAKERY", reason_code="DATE_DISSATISFIED",
     delivered="2026-10-01T00:00:00+08:00", claim="2026-10-16T23:59:59+08:00")
case("HV-40", "时间边界", "RETURN_REFUND", "R-JD-NR-WINDOW-EXPIRED-001", "DENY", "NO_REASON_WINDOW_EXPIRED",
     "CL-JD4268-A3：第 20 天已远超 7 个自然日",
     "主集超期最大 61 天（烘焙）与 31 天（无理由）；此处 20 天且为普通品类",
     delivered="2026-10-01T10:00:00+08:00", claim="2026-10-21T10:00:00+08:00")

# ---------------- 金额边界（5） ----------------
case("HV-27", "金额边界", "COMPLAINT", "R-JD-COMP-OOS-001", "MANUAL", "AMOUNT_EXCEEDS_AUTO_THRESHOLD",
     "自动执行阈值 2000 分：超 1 分应转人工审批，但资格仍在",
     "主集虚假发货只用 1000/5000；此处验证 2001 这一临界值",
     category="GROCERY", reason_code="FALSE_SHIPMENT", amount=2001)
case("HV-28", "金额边界", "COMPLAINT", "R-JD-COMP-OOS-001", "ALLOW", "OUT_OF_STOCK_COMPENSATION",
     "CL-JD1190896203588571136-S17：虚假发货同属缺货/无货类赔付，2000 分恰在上限",
     "主集虚假发货用 1000 与 5000；此处验证阈值上界 2000",
     reason_code="FALSE_SHIPMENT", amount=2000)
case("HV-29", "金额边界", "COMPLAINT", "R-JD-CATEGORY-UNKNOWN-001", "MANUAL", "CATEGORY_UNKNOWN_REVIEW",
     "未知品类 fail-closed：即使赔付规则命中，也必须人工确认品类",
     "主集未验证“未知品类 + 赔付类诉求”的冲突",
     category="UNKNOWN", reason_code="LATE_DELIVERY", amount=2500)
case("HV-30", "金额边界", "COMPLAINT", "R-JD-COMP-OOS-001", "ALLOW", "OUT_OF_STOCK_COMPENSATION",
     "CL-JD1190896203588571136-S17：赔付按单，0 分不超上限（政策未设下限）",
     "主集 0 分用在延迟发货；此处用于缺货",
     reason_code="OUT_OF_STOCK", amount=0)
case("HV-31", "金额边界", "STATUS_QUERY", "R-JD-COMP-LATE-001", "MANUAL", "AMOUNT_EXCEEDS_AUTO_THRESHOLD",
     "赔偿规则不限制意图：查询意图下命中赔付规则同样受阈值约束",
     "主集金额超阈值都在 COMPLAINT 意图下；此处验证意图不改变赔付规则命中",
     reason_code="LATE_DELIVERY", amount=2001)

# ---------------- 缺事实 / 品类归一（7） ----------------
case("HV-33", "缺事实", "RETURN_REFUND", "R-JD-NR-WINDOW-EXPIRED-001", "NEED_INFO", "MISSING_FACT",
     "缺退货原因时无法判断适用例外还是窗口，必须先问清",
     "主集缺原因用例用 APPAREL/FRESH；此处用 GROCERY 且时间完整（只剩原因未知）",
     category="GROCERY", reason_code=None,
     delivered="2026-10-05T10:00:00+08:00", claim="2026-10-11T10:00:00+08:00")
case("HV-34", "缺事实", "RETURN_REFUND", "R-JD-Q-RETURN-001", "ALLOW", "QUALITY_RETURN_WINDOW",
     "CL-JD2595-S16：质量问题退货不以“签收时间”为前提条件",
     "主集所有质量用例都带签收时间；此处验证缺签收时间不影响质量路径",
     reason_code="QUALITY_DEFECT", delivered=None, claim=None,
     as_of="2026-10-11T10:00:00+08:00")
case("HV-35", "缺事实", "RESHIPMENT", "R-JD-BAKERY-WINDOW-EXPIRED-001", "NEED_INFO", "MISSING_FACT",
     "CL-JD1148155761021554688-S4：烘焙 15 天窗口必须知道申请时刻",
     "主集烘焙缺时间只测过第 15 天前；此处验证缺 claim_at 走 NEED_INFO 而不是直接放行",
     category="BAKERY", reason_code="DATE_DISSATISFIED", claim=None,
     delivered="2026-10-01T10:00:00+08:00")
case("HV-36", "品类归一", "RETURN_REFUND", "R-JD-NR-DENY-DIGITAL-001", "DENY", "NO_REASON_DIGITAL_EXEMPT",
     "CL-JD4268-T1#2：家电属不适用无理由的商品档",
     "主集中文别名用“生鲜/图书/定制/烘焙/黄金/电动车”；此处用“家电”",
     category="家电")
case("HV-37", "品类归一", "RETURN_REFUND", "R-JD-CATEGORY-UNKNOWN-001", "MANUAL", "CATEGORY_UNKNOWN_REVIEW",
     "未知品类无法确定适用哪一档例外/质量政策，必须人工",
     "主集未知品类用 no-reason；此处用质量原因，验证未知品类压过质量允许",
     category="未知新类", reason_code="QUALITY_DEFECT")
case("HV-38", "品类归一", "INTERCEPT_REFUND", "R-JD-CATEGORY-UNKNOWN-001", "MANUAL", "CATEGORY_UNKNOWN_REVIEW",
     "未知品类 + 可拦截状态：拦截允许不得越过品类未知的强制人工",
     "主集未验证“未知品类 + 在途拦截允许”的冲突",
     category="UNKNOWN", parcel="IN_TRANSIT", status="SHIPPED")
case("HV-39", "缺事实", "REFUND_ONLY", None, "MANUAL", "AUTHORITATIVE_FACT_UNAVAILABLE",
     "签收时间是 OMS 权威事实，缺失时不能要求用户自行填写（只能人工核实）",
     "主集缺签收时间都在“无理由 + DELIVERED”下；此处订单未发货、没有任何规则命中，验证兜底路径同样区分权威事实",
     status="PAID_UNSHIPPED", parcel="UNKNOWN", delivered=None, claim=None,
     as_of="2026-10-11T10:00:00+08:00")


def main() -> int:
    ids = [c["case_id"] for c in CASES]
    assert len(ids) == len(set(ids))
    assert len(CASES) == 40, len(CASES)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with io.open(OUT, "w", encoding="utf-8", newline="\n") as f:
        for c in CASES:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    groups: dict[str, int] = {}
    for c in CASES:
        groups[c["group"]] = groups.get(c["group"], 0) + 1
    print("written", OUT, len(CASES), groups)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())