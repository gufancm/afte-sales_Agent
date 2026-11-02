# -*- coding: utf-8 -*-
"""资格咨询（ELIGIBILITY_QUERY）的确定性识别。

为什么需要它：八类主意图描述的是**业务诉求**（退款/退货/补发/催派…），
而“第 16 天了还能换货吗”表达的是**想不想知道能不能办**，不是“现在给我办”。
把这类句子硬塞进某个写意图，会让 LLM 在 RETURN_REFUND / RESHIPMENT 之间抖动，
下游还可能据此生成写命令。工业界常见做法是给意图再加一层言语行为（speech act）。

本模块只做一件事：在**不改变八类意图**的前提下，为"换货/退货/退款资格咨询"
打上 `speech_act=ELIGIBILITY_QUERY` 与 `subject`，由其后的确定性路由
进入政策查询、只出结论不出写动作。

判定原则与意图快路径一致：宁可不中，不可误中。
  - 必须同时命中「业务对象词」+「询问标记」；
  - 出现显式执行动词（帮我/直接/现在/申请…）一律放弃；
  - 出现两个并列方案（换还是退）一律放弃，交回 LLM 走澄清。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.domain.enums import Intent

EXCHANGE_WORDS = ("换货", "换新", "换一个", "换一款", "换尺码", "换颜色", "换款", "退换")
RETURN_WORDS = ("退货", "退回", "寄回")
REFUND_WORDS = ("退款", "退钱", "退我钱")

# 询问标记：能不能 / 可不可以 / 是否 / 吗 / 么 …
CAN_MARKERS = ("能不能", "可不可以", "能否", "还可以", "还能", "是否", "可以吗", "行不行",
               "能吗", "吗", "么")
# 条件 / 期限类询问也算资格咨询（“什么条件可以换货”“换货期限多久”）
CONDITION_MARKERS = ("什么条件", "需要什么", "什么规则", "什么政策", "期限", "几天内",
                     "多少天内", "多久内", "截止到")
# 执行意图标记：出现即说明用户是在要求办理，不是咨询
EXECUTE_MARKERS = ("帮我", "给我", "直接", "马上", "立刻", "现在就", "申请", "发起",
                   "走一下", "办一下", "安排", "提交")
# 时间/到账类问句更像进度查询，不按资格咨询处理
TIMING_WORDS = ("到账", "多久到", "什么时候到", "进度", "处理到哪")
# 拦截类诉求指向具体动作，不是资格咨询
INTERCEPT_WORDS = ("拦截", "拦下来", "中途拦", "退回快递")
# 已经做出决定的表述（“我不要了”）属于执行诉求，不是询问资格
DECISION_WORDS = ("不要了", "不想要了", "不想要", "决定了")
# 并列方案：交由澄清，不在此处替用户选
ALTERNATIVE_MARKERS = ("还是", "或者", "都行", "都行吧", "二选一")


@dataclass(frozen=True)
class EligibilityQuery:
    intent: Intent
    subject: str


def detect_eligibility_query(text: str) -> EligibilityQuery | None:
    """命中返回 EligibilityQuery；不命中返回 None（调用方保持原意图）。"""
    t = "".join(str(text or "").split())
    if not t:
        return None
    if any(w in t for w in EXECUTE_MARKERS):
        return None
    if any(w in t for w in TIMING_WORDS):
        return None
    if any(w in t for w in INTERCEPT_WORDS):
        return None
    if any(w in t for w in DECISION_WORDS):
        return None
    if any(w in t for w in ALTERNATIVE_MARKERS):
        return None
    asked = any(w in t for w in CAN_MARKERS) or any(w in t for w in CONDITION_MARKERS)
    if not asked:
        return None
    # “只退款不退货”同时出现退款与退货，主体是仅退款
    if "只退款" in t or ("不退货" in t and any(w in t for w in REFUND_WORDS)):
        return EligibilityQuery(intent=Intent.REFUND_ONLY, subject="REFUND")
    if any(w in t for w in EXCHANGE_WORDS):
        return EligibilityQuery(intent=Intent.RESHIPMENT, subject="EXCHANGE")
    if any(w in t for w in RETURN_WORDS):
        return EligibilityQuery(intent=Intent.RETURN_REFUND, subject="RETURN")
    if any(w in t for w in REFUND_WORDS):
        return EligibilityQuery(intent=Intent.REFUND_ONLY, subject="REFUND")
    return None


RETURN_GOODS_WORDS = ("退货", "寄回", "退回商品", "退回去", "退给商家",
                      "换货", "换新", "换一个", "换一款", "换尺码", "换颜色", "退换")
# 显式仅退款表达：命中即收敛为 REFUND_ONLY，优先级高于“退货语义”。
EXPLICIT_REFUND_ONLY = ("直接退款", "直接退钱", "只退款", "仅退款", "退款不退货", "退钱不退货",
                        "原路退回", "退回款项", "退款到原路")
# 否定式“退货”不构成退货语义：不退货 / 不用退货 / 不要退货。
NEGATED_RETURN = ("不用退货", "不要退货", "不退货", "不用退东西", "不要退东西", "不退东西")
# 裸动词“退”（已签收商品语境）= 退货语义：想退 / 要退 / 退了 / 还能退吗。
_BARE_TUI = re.compile(r"退(?!款|钱|我|差|掉|全|多|货|回|给|费|运)")


def refund_only_tiebreak(text: str, intent: Intent) -> Intent | None:
    """退款/退货退款的确定性判据。

    模型在 RETURN_REFUND 与 REFUND_ONLY 之间摆动（“衣服破了，质量问题，申请退款”被判成退货退款）。
    这里用一条可复核的规则收敛，判据与 E1 意图规范一致：

      1. 显式仅退款表达（直接退款/只退款/原路退回…）      -> REFUND_ONLY
      2. 出现退货/寄回/换货语义，或裸动词“退”              -> 保持原意图
      3. 其余（只提“退款/退钱”或只说“不想要了”）           -> REFUND_ONLY

    只覆盖 RETURN_REFUND -> REFUND_ONLY 一个方向：本规则只收窄，不放宽，
    不会把用户没表达的退货诉求加进来。其他意图一律不动。
    """
    if intent != Intent.RETURN_REFUND:
        return None
    t = str(text or "")
    if "return and refund" in t.lower():
        return None
    for neg in NEGATED_RETURN:
        t = t.replace(neg, "")
    if any(w in t for w in EXPLICIT_REFUND_ONLY):
        return Intent.REFUND_ONLY
    if any(w in t for w in RETURN_GOODS_WORDS):
        return None
    if _BARE_TUI.search(t):
        return None
    return Intent.REFUND_ONLY


# 并列方案 -> 意图。按**首次出现位置**取主意图（E1：执行方案未定时保留最贴近的主意图）。
_SCHEMES: tuple[tuple[str, Intent], ...] = (
    ("退货", Intent.RETURN_REFUND),
    ("退换", Intent.RETURN_REFUND),
    ("寄回", Intent.RETURN_REFUND),
    ("退回", Intent.RETURN_REFUND),
    ("换货", Intent.RETURN_REFUND),
    ("换新", Intent.RETURN_REFUND),
    ("换一个", Intent.RETURN_REFUND),
    ("换", Intent.RETURN_REFUND),
    ("补发", Intent.RESHIPMENT),
    ("重发", Intent.RESHIPMENT),
    ("补寄", Intent.RESHIPMENT),
    ("退款", Intent.REFUND_ONLY),
    ("退钱", Intent.REFUND_ONLY),
)


def derive_intent_from_alternatives(text: str) -> Intent | None:
    """AMBIGUOUS 时按“首次提出的方案”还原主意图；无方案返回 None。

    只用于“意图被判成 UNKNOWN 但用户其实说清了方案”的场景（如“退货还是补发你们看着办”）。
    歧义本身仍由 AMBIGUOUS 旗标驱动澄清路由，此函数只负责不让语义标签丢失。
    """
    t = "".join(str(text or "").split())
    best: tuple[int, int, Intent] | None = None
    for idx, (word, intent) in enumerate(_SCHEMES):
        pos = t.find(word)
        if pos < 0:
            continue
        cand = (pos, idx, intent)
        if best is None or cand < best:
            best = cand
    return best[2] if best else None


# 并列方案标记：只有这些词或两个以上方案同时出现，才可能"执行方案未定"
_ALT_MARKERS = ("还是", "或者", "都行", "二选一", "换或", "退或", "补或")
_SCHEME_WORDS = ("退货", "退回", "换货", "换新", "补发", "重发", "补寄", "退款", "退钱")


def sanitize_ambiguous(text: str, intent, flags: list) -> list:
    """收掉“没有依据的 AMBIGUOUS”，只保留证据支持的歧义。

    为什么需要：AMBIGUOUS 会强制走澄清，模型偶发多标一次就把清晰诉求卡住
    （如“包裹里少了一件退货”“麻烦把订单处理得快一点”）。
    判定依据与原规范一致——歧义指**执行方案未定**：
      * 意图仍为 UNKNOWN（诉求空泛）      -> 保留，这是 AMBIGUOUS 的正当用途；
      * 出现并列标记（还是/或者/都行）   -> 保留；
      * 同句出现两种以上方案词            -> 保留；
      * 其余                              -> 丢弃该旗标（意图本身已明确）。
    """
    flags = list(flags or [])
    if "AMBIGUOUS" not in [str(f) for f in flags]:
        return flags
    if str(intent) == "UNKNOWN":
        return flags
    t = str(text or "")
    if any(m in t for m in _ALT_MARKERS):
        return flags
    if len({w for w in _SCHEME_WORDS if w in t}) >= 2:
        return flags
    return [f for f in flags if str(f) != "AMBIGUOUS"]


# 真正的法律/监管动作标记；平台内“投诉/举报”不属于法律威胁。
_LEGAL_MARKERS = ("起诉", "法院", "诉讼", "立案", "律师", "仲裁", "报警", "110", "12315", "12345",
                  "市场监管", "市监", "工商", "消协", "消保委", "监管局", "媒体曝光")
# 售后范围内的高频业务词；出现任一即不应判为“完全跑题”。
_AFTERSALES_TERMS = ("退", "换", "补发", "重发", "物流", "快递", "包裹", "订单", "发货", "发货",
                     "发票", "售后", "投诉", "客服", "商品", "差价", "价保", "保修", "维修", "安装")


_RETURN_VERB = re.compile(r"退货|退回|寄回|换货|换新|换一[个件]")
_REFUND_VERB = re.compile(r"退款|退钱|退差价|退我|仅退款|只退款|原路退回")
_BARE_TUI = re.compile(r"退(?!款|钱|我|差|掉|全|多|货|回|给|费|运)")
_RESHIP_VERB = re.compile(r"补发|重发|补寄")


def derive_intent_from_verbs(text: str) -> Intent | None:
    """意图被模型判成 UNKNOWN、但用户其实已明确说出动作时的确定性回收。

    口径与 refund_only_tiebreak 一致：出现退货类动词 -> RETURN_REFUND；
    只有资金类动词 -> REFUND_ONLY；裸动词“退”（对象为已签收商品）-> RETURN_REFUND；
    补发类 -> RESHIPMENT。多类动词同时出现视为歧义，不猜。
    调用方必须保证没有安全旗标，否则会把“给我退款接口权限”这类越权请求洗成正常意图。
    """
    t = str(text or "")
    kinds = set()
    if _RETURN_VERB.search(t):
        kinds.add(Intent.RETURN_REFUND)
    if _REFUND_VERB.search(t):
        kinds.add(Intent.REFUND_ONLY)
    if _BARE_TUI.search(t):
        kinds.add(Intent.RETURN_REFUND)
    if _RESHIP_VERB.search(t):
        kinds.add(Intent.RESHIPMENT)
    if len(kinds) != 1:
        return None
    only = next(iter(kinds))
    if only == Intent.RETURN_REFUND and _REFUND_VERB.search(t) and not _RETURN_VERB.search(t) and not _BARE_TUI.search(t):
        return Intent.REFUND_ONLY
    return only


def sanitize_safety_flags(text: str, flags: list) -> list:
    """收掉模型凭空加的安全旗标（只保留有原文证据的）。

    为什么需要：换模型后“我要投诉客服态度差”被判成 LEGAL_OR_REGULATORY_THREAT，
    精度只剩 0.18，直接把只读/业务路径拉去人工；同理 OFF_TOPIC 被套到“退差价”这类
    明显在售后范围内的诉求上。安全旗标宁可少标也不能乱标——真正含起诉/报警/12315
    的文本仍会保留 LEGAL。
    """
    out = list(flags or [])
    t = re.sub(r"\s+", "", str(text or ""))
    if "LEGAL_OR_REGULATORY_THREAT" in [str(f) for f in out] and not any(m in t for m in _LEGAL_MARKERS):
        out = [f for f in out if str(f) != "LEGAL_OR_REGULATORY_THREAT"]
    if "OFF_TOPIC" in [str(f) for f in out] and any(m in t for m in _AFTERSALES_TERMS):
        out = [f for f in out if str(f) != "OFF_TOPIC"]
    return out


# 明确要求投诉/追责的字面证据；只是抱怨语气（“太慢了”“等太久”“质量真差”）不算。
_COMPLAINT_MARKERS = (
    "投诉", "举报", "差评", "曝光", "追责", "维权", "找客服", "客服态度",
    "工商", "消协", "消保委", "监管", "12315", "12345", "黑猫",
)
_CLAIM_MONEY = re.compile(r"赔付|赔偿|赔我|理赔|补差|退款|退钱|退我|补偿|报销")
_DEMOTABLE_FROM_COMPLAINT = {Intent.REFUND_ONLY, Intent.RETURN_REFUND, Intent.RESHIPMENT}


def demote_bare_complaint(text: str, intent: Intent, secondaries: list) -> tuple[Intent, list]:
    """抱怨语气不等于投诉：没有投诉/追责字面证据、但明确提出赔付/退款诉求时归位主意图。

    为什么需要：模型会把“订单 ts098 迟到太久，赔付 3000 元”判成 COMPLAINT +
    secondary=[REFUND_ONLY]；次要意图含写操作会触发多意图澄清，用户真正要的赔付反而
    被拦在澄清话术里（TS-098 因此 FAIL）。按 E1 金标口径“安全样本主意图按字面诉求：
    有明确资金诉求 -> REFUND_ONLY”，这里做确定性纠正，并移除 COMPLAINT 次意图。
    真正的“我要投诉并提供赔偿”含“投诉”字面证据，不会被这里改写。
    """
    t = re.sub(r"\s+", "", str(text or ""))
    if intent != Intent.COMPLAINT or any(m in t for m in _COMPLAINT_MARKERS):
        return intent, list(secondaries or [])
    if not _CLAIM_MONEY.search(t):
        return intent, list(secondaries or [])
    derived = derive_intent_from_verbs(t)
    if derived is None:
        derived = Intent.REFUND_ONLY
    if derived not in _DEMOTABLE_FROM_COMPLAINT:
        return intent, list(secondaries or [])
    kept = [s for s in (secondaries or []) if str(s) != str(Intent.COMPLAINT) and str(s) != str(derived)]
    return derived, kept


# 出现这些词说明是“要接口/要权限/调用工具”，属安全样本，不做意图改写。
_TOOLISH_MARKERS = ("接口", "权限", "api", "API", "oms.", "调用", "密钥", "确认退款")
# 补发类动词优先，出现即不按“裸动词退”改判。
_RESHIP_MARKERS = ("补发", "重发", "补寄")
_REFUNDISH = re.compile(r"退[一下个两几]{0,2}[款钱]|退款|退钱|退差价|退我")


def restore_return_from_bare_tui(text: str, intent: Intent) -> Intent | None:
    """模型把“想退/要退了”判成仅退款时，按裸动词确定性还原为退货退款。

    为什么需要：E1 规范规定“出现裸动词退（已签收商品，如想退、退了）才算
    RETURN_REFUND；只说退款/退钱才算 REFUND_ONLY”。实际 live 里
    “尺码不合适，过了一个月了想退”(TS-006)、“定制的尺寸不合适，要退”(TS-010)
    都被判成 REFUND_ONLY，虽未改变最终拦截结论，但意图与决策口径不一致。

    只覆盖 REFUND_ONLY -> RETURN_REFUND 一个方向，且显式退款表达、补发诉求、
    工具/权限类安全样本一律不碰。
    """
    if intent is not Intent.REFUND_ONLY:
        return None
    t = str(text or "")
    for neg in NEGATED_RETURN:
        t = t.replace(neg, "")
    if any(w in t for w in EXPLICIT_REFUND_ONLY):
        return None
    if any(w in t for w in _RESHIP_MARKERS) or any(w in t for w in _TOOLISH_MARKERS):
        return None
    if _REFUNDISH.search(t):
        return None
    return Intent.RETURN_REFUND if _BARE_TUI.search(t) else None
