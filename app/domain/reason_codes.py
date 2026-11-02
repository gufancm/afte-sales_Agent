# -*- coding: utf-8 -*-
"""原因码（claim.reason_code）的确定性解析与优先级。

优先级（高 → 低）：
  1. **权威事实**：OMS / 售后单已经记录的申请原因（authoritative）。
     用户口述和模型猜测都不得覆盖它。
  2. **用户原文中的显式客观证据**：与描述不符 / 发错货 / 少件 / 破损 …
  3. **LLM 抽取的槽位**。

为什么需要第 2 层：一句话里常常同时出现“客观事实”和“主观意愿”，
例如“颜色和页面不一样，不想要了，退钱”——只抽到“不想要了”（NO_LONGER_NEEDED）
就会命中“七天无理由”，而正确结论应是“与描述不符”（NOT_AS_DESCRIBED）走质量问题条款。
客观事实决定适用哪条政策，主观意愿只影响能否无理由，因此客观类必须优先。

本模块只消费**用户原文**与**已存在的权威事实**，不读取评测夹具（fixture 属答案，注入即泄漏）。
"""
from __future__ import annotations

import re

# 客观事实类在前，主观意愿类在后；同句多命中取更靠前者。
REASON_PRIORITY: tuple[str, ...] = (
    "NOT_AS_DESCRIBED",
    "WRONG_ITEM",
    "MISSING_ITEM",
    "DAMAGED",
    "QUALITY_DEFECT",
    "COLOR_MISMATCH",
    "SIZE_ISSUE",
    "DATE_DISSATISFIED",
    "WEIGHT_DIFF",
    "INSTALL_LATE",
    "INSTALL_FEE",
    "INSTALL_DEFECT",
    "FALSE_SHIPMENT",
    "OUT_OF_STOCK",
    "LATE_DELIVERY",
    "DELIVERY_ABNORMAL",
    "NO_LONGER_NEEDED",
)

REASON_SIGNALS: dict[str, tuple[str, ...]] = {
    "NOT_AS_DESCRIBED": ("和页面不一样", "与页面不一样", "跟页面不一样", "和图片不一样", "与图片不一样",
                         "跟图片不一样", "和照片不一样", "和描述不一样", "与描述不符", "跟描述不符",
                         "描述不符", "和实物不符", "与实物不符", "货不对板", "和详情页不一样",
                         "与详情页不符", "和宣传不符", "功能与说明不符"),
    "WRONG_ITEM": ("发错货", "发错颜色", "发错尺码", "发错型号", "发错了", "寄错", "型号不对",
                   "版本发错", "不是我要的型号", "不是我要的款式"),
    "MISSING_ITEM": ("少发", "少了一件", "漏发", "缺件", "少了配件", "少了个", "少配件", "缺零件",
                     "没收到配件", "少一样"),
    "DAMAGED": ("破损", "破了", "损坏", "压扁", "碎了", "裂了", "有裂痕", "划痕", "坏点", "摔坏",
                "变形", "污损", "被压坏", "碰伤", "不转", "不通电", "无法启动", "开不了机",
                "烧坏", "烧了", "坏掉了", "电机故障", "电机坏了", "电池鼓包"),
    "QUALITY_DEFECT": ("质量问题", "有瑕疵", "瑕疵", "起球", "开线", "失灵", "异味", "掉色",
                       "不达标", "二手货", "质量太差", "质量差"),
    "COLOR_MISMATCH": ("色差", "颜色不对", "颜色不一致", "有色差", "颜色偏差"),
    "SIZE_ISSUE": ("尺码", "尺寸不合适", "尺寸不对", "偏大", "偏小", "穿不下", "码数不对"),
    "DATE_DISSATISFIED": ("日期不满意", "生产日期", "保质期短", "临期"),
    "WEIGHT_DIFF": ("克重", "缺斤少两", "重量不够", "重量不足"),
    "INSTALL_LATE": ("安装延迟", "安装太慢", "一直没来安装", "约不上安装"),
    "INSTALL_FEE": ("安装费", "乱收费", "加收费用"),
    "INSTALL_DEFECT": ("安装质量", "装坏了", "安装不当"),
    "FALSE_SHIPMENT": ("虚假发货", "假发货", "没发货显示已发货"),
    "OUT_OF_STOCK": ("缺货", "没货了", "无货"),
    "LATE_DELIVERY": ("迟到", "超时未发", "发货太慢", "一直没发货", "逾期", "迟迟不发货", "晚到"),
    "DELIVERY_ABNORMAL": ("物流异常", "揽收异常", "物流停滞", "配送异常", "快递不动"),
    # “买错/拍错”是买家自身选择，不是“发错货”（WRONG_ITEM）；判成发错货会误走质量问题条款。
    "NO_LONGER_NEEDED": ("不想要", "不需要了", "改主意", "不想买了", "不要了",
                         "买错", "拍错", "买重", "多买了"),
}

_WS = re.compile(r"\s+")


def resolve_reason_code(text: str | None, llm_slot: str | None = None,
                        authoritative: str | None = None) -> tuple[str | None, str]:
    """返回 (reason_code, 来源)。

    来源取值：AUTHORITATIVE / TEXT_SIGNAL / LLM_SLOT / NONE，写审计用，便于解释“为什么是这个原因码”。
    """
    if authoritative:
        return str(authoritative), "AUTHORITATIVE"
    t = _WS.sub("", str(text or ""))
    if t:
        for code in REASON_PRIORITY:
            for sig in REASON_SIGNALS[code]:
                if sig in t:
                    return code, "TEXT_SIGNAL"
    if llm_slot:
        slot = str(llm_slot).strip().upper()
        if slot in REASON_SIGNALS:
            return slot, "LLM_SLOT"
        # 模型自造的原因码（如 EBIKE_MOTOR_RETURN）会让政策规则一条都命中不了，
        # 直接把正常售后打成 MANUAL/NO_APPLICABLE_POLICY。枚举外一律丢弃。
    return None, "NONE"
