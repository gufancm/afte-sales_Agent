# -*- coding: utf-8 -*-
"""查询变换（Retrieval 前置增强）：多查询改写 + HyDE。

- 多查询改写：把用户查询改写成 N 条同义/补全表述，分别检索后 RRF 融合（提高召回）。
- HyDE：让 LLM 生成“假设的政策条款原文”，用其向量/词法去匹配真实条款（语义对齐）。
- 只影响**发现**，不参与政策裁决；授权事实永远来自 required_clause_ids 的精确补齐。
- 全部 LLM 调用失败/未配置时整段降级为原始查询，绝不影响主检索。
- 内置 TTL/LRU 缓存：同一 query（同一进程、同一模型）只付一次改写费用。
- 对模型输出做确定性清洗：拒绝超长、含指令注入/越权语句的变体，避免污染检索。

变体列表首个永远是原始查询。调用方据此做多路 RRF 融合（见 app/rag/retrieval.py）。
"""
from __future__ import annotations

import re
import time
from collections import OrderedDict
from typing import Any

PROMPT_VERSION_MULTI_QUERY = "rag-multi-query-v1"
PROMPT_VERSION_HYDE = "rag-hyde-v1"
_NUM_PREFIX = "0123456789.-、) "

# 只拦截明显的“指令注入/越权”片段；正常的“退款/退货/运费”等业务词不受影响。
_UNSAFE_RE = re.compile(
    r"(?i)(ignore\s+(all\s+)?previous|system\s*prompt|developer\s*message|"
    r"忽略(之前|以上|上述)?(的)?(所有)?(指令|规则)|系统提示|开发者消息|"
    r"绕过(审批|审核|风控)|跳过(审批|审核)|直接(给我)?退款|把退款金额|"
    r"api[\s_-]?key|sk-[a-z0-9]{8,})"
)


def _user_block(query: str, context: str, instruction: str) -> str:
    """组装用户侧提示：上下文只用于消除指代，不得被当作新的业务事实。"""
    parts = [f"原始查询：{query}"]
    if context:
        parts.append(f"对话上下文（仅用于消解指代，不是新问题）：{context}")
    if instruction:
        parts.append(instruction)
    return "\n".join(parts)


def _clean(text: str) -> str:
    return " ".join((text or "").split()).strip(" -•\t")


class LLMQueryTransformer:
    def __init__(self, llm: Any, prompts: dict[str, str] | None = None, *, multi_n: int = 3,
                 use_rewrite: bool = True, use_hyde: bool = True,
                 cache_ttl_s: float = 900.0, cache_max: int = 512,
                 max_variant_chars: int = 300, max_calls_per_day: int = 0,
                 clock: Any = None) -> None:
        self.llm = llm
        self.prompts = prompts or {}
        self.multi_n = max(0, int(multi_n))
        self.use_rewrite = bool(use_rewrite)
        self.use_hyde = bool(use_hyde)
        self.cache_ttl_s = max(0.0, float(cache_ttl_s))
        self.cache_max = max(0, int(cache_max))
        self.max_variant_chars = max(20, int(max_variant_chars))
        self.max_calls_per_day = max(0, int(max_calls_per_day))
        self._calls_today = 0
        self._budget_day = ""
        self._clock = clock or time.monotonic
        self._cache: OrderedDict[str, tuple[float, list[str], dict[str, Any]]] = OrderedDict()

    @property
    def enabled(self) -> bool:
        return self.llm is not None and (self.use_rewrite or self.use_hyde)

    def _budget_ok(self) -> bool:
        """进程级日调用预算：0 表示不限。跨天自动归零。"""
        if self.max_calls_per_day <= 0:
            return True
        today = time.strftime("%Y-%m-%d")
        if self._budget_day != today:
            self._budget_day, self._calls_today = today, 0
        return self._calls_today < self.max_calls_per_day

    def _spend(self) -> None:
        if self.max_calls_per_day > 0:
            self._calls_today += 1

    def _safe(self, text: str, query: str, seen: list[str]) -> tuple[str | None, str | None]:
        s = _clean(text)
        if not s:
            return None, "empty"
        if s == query:
            return None, "same_as_query"
        if s in seen:
            return None, "duplicate"
        if len(s) > self.max_variant_chars:
            return None, "too_long"
        if _UNSAFE_RE.search(s):
            return None, "unsafe_instruction"
        return s, None

    def _cache_get(self, key: str) -> tuple[list[str], dict[str, Any]] | None:
        if self.cache_ttl_s <= 0 or not self._cache:
            return None
        hit = self._cache.get(key)
        if hit is None:
            return None
        ts, variants, diag = hit
        if self._clock() - ts > self.cache_ttl_s:
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        out_diag = dict(diag)
        out_diag["cache_hit"] = True
        # 命中缓存不产生新的计费调用；审计里不能再记一次 LLM call。
        out_diag["llm_calls"] = []
        return list(variants), out_diag

    def _cache_put(self, key: str, variants: list[str], diag: dict[str, Any]) -> None:
        if self.cache_ttl_s <= 0 or self.cache_max <= 0:
            return
        # 失败降级结果不缓存，避免短暂故障被固化 15 分钟。
        if len(variants) <= 1 or diag.get("errors"):
            return
        self._cache[key] = (self._clock(), list(variants), dict(diag))
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_max:
            self._cache.popitem(last=False)

    async def variants(self, query: str, *, context: str | None = None) -> tuple[list[str], dict]:
        """返回 (查询变体, 诊断)。首个恒为原始查询；任何失败均降级。

        context 为会话上下文锚点（近期用户话术），只用于消解“这个/那个”等指代，
        不改变改写目标；有上下文时缓存键包含上下文，避免串味。
        """
        out: list[str] = [query]
        diag: dict[str, Any] = {
            "multi_query": [], "hyde": None, "errors": [], "rejected": [],
            "enabled": self.enabled, "llm_calls": [], "cache_hit": False,
        }
        q = (query or "").strip()
        ctx = " ".join(str(context or "").split())[:240]
        if not self.enabled or not q:
            return out, diag

        cache_key = q if not ctx else q + "\u0001" + ctx
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        if not self._budget_ok():
            diag["errors"].append("budget_exhausted")
            return out, diag

        if self.use_rewrite and self.multi_n > 0:
            prompt = self.prompts.get("query_rewrite")
            if prompt:
                try:
                    text, audit = await self.llm.text(
                        messages=[{"role": "system", "content": prompt},
                                  {"role": "user", "content": _user_block(q, ctx, f"请给出 {self.multi_n} 条改写。")}],
                        prompt_version=PROMPT_VERSION_MULTI_QUERY)
                    self._spend()
                    diag["llm_calls"].append({"prompt_version": PROMPT_VERSION_MULTI_QUERY, **(audit or {})})
                    for line in (text or "").splitlines():
                        s, why = self._safe(line.strip().lstrip(_NUM_PREFIX), q, out)
                        if s is None:
                            if why not in (None, "empty", "same_as_query"):
                                diag["rejected"].append({"text": line.strip()[:80], "reason": why})
                            continue
                        out.append(s)
                        if len(out) >= 1 + self.multi_n:
                            break
                    diag["multi_query"] = out[1:]
                except Exception as exc:  # noqa: BLE001 - 降级
                    diag["errors"].append("multi_query:" + str(exc)[:120])

        if self.use_hyde:
            prompt = self.prompts.get("hyde")
            if prompt:
                try:
                    text, audit = await self.llm.text(
                        messages=[{"role": "system", "content": prompt},
                                  {"role": "user", "content": _user_block(q, ctx, "")}],
                        prompt_version=PROMPT_VERSION_HYDE)
                    self._spend()
                    diag["llm_calls"].append({"prompt_version": PROMPT_VERSION_HYDE, **(audit or {})})
                    s, why = self._safe(text or "", q, out)
                    if s is None:
                        if why not in (None, "empty", "same_as_query"):
                            diag["rejected"].append({"text": _clean(text or "")[:80], "reason": why})
                    else:
                        out.append(s)
                        diag["hyde"] = s[:200]
                except Exception as exc:  # noqa: BLE001 - 降级
                    diag["errors"].append("hyde:" + str(exc)[:120])

        self._cache_put(cache_key, out, diag)
        return out, diag