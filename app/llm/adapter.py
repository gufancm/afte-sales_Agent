"""OpenAI 兼容 LLM 适配器（Chat Completions / Responses 双协议，LLD §17.2 §19.3）。
只负责模型输出；金额授权与写操作在 LangGraph / Finance 层完成。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, TypeVar

import httpx
from openai import APIConnectionError, APIStatusError, AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.domain.canonical import canonical_json

T = TypeVar("T", bound=BaseModel)


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """粗略 token 估算（中文≈1.6字符/token）。生产应替换为所选模型真实 tokenizer。"""
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            total += int(len(content) / 1.6) + 4
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total += int(len(part["text"]) / 1.6) + 4
        total += 8
    return total


@dataclass(frozen=True)
class LLMSettings:
    base_url: str
    api_key: str
    model: str
    api_style: str
    context_window: int
    max_output_tokens: int
    supports_json_schema: bool
    supports_json_object: bool
    timeout_seconds: float
    connect_timeout_seconds: float = 5.0
    safety_margin_tokens: int = 2048
    max_retries: int = 1
    chat_token_param: str = "max_tokens"
    extra_body: dict[str, Any] | None = None

    @classmethod
    def from_app(cls, s: Settings) -> "LLMSettings":
        return cls(
            base_url=s.llm_base_url,
            api_key=s.llm_api_key,
            model=s.llm_model,
            api_style=s.llm_api_style,
            context_window=s.llm_context_window,
            max_output_tokens=s.llm_max_output_tokens,
            supports_json_schema=s.llm_supports_json_schema,
            supports_json_object=s.llm_supports_json_object,
            timeout_seconds=s.llm_timeout_ms / 1000,
            connect_timeout_seconds=s.llm_connect_timeout_ms / 1000,
            safety_margin_tokens=s.llm_safety_margin_tokens,
            max_retries=s.llm_max_retries,
            extra_body=_parse_extra_body(s.llm_extra_body),
        )


def _parse_extra_body(raw: str) -> dict[str, Any] | None:
    """端点私有参数透传（百炼 enable_thinking 等）；配置非法直接启动失败。"""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM_EXTRA_BODY 不是合法 JSON：{exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("LLM_EXTRA_BODY 必须是 JSON 对象")
    return parsed


class ModelRefusal(Exception):
    pass


class ModelTruncated(Exception):
    pass


class ModelFiltered(Exception):
    pass


class ContextBudgetExceeded(Exception):
    pass


def provider_strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """转换为通用 strict 子集；启动时还须做端点能力校验（§2.4/§19.3）。"""
    schema = model.model_json_schema()

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for v in node:
                visit(v)
        elif isinstance(node, dict):
            if node.get("type") == "object":
                props = node.get("properties", {})
                ap = node.get("additionalProperties")
                if ap not in (None, False) and not isinstance(ap, bool):
                    raise ValueError("不支持动态字典额外字段；请定义明确的 Pydantic Schema（§19.3）")
                node["additionalProperties"] = False
                node["required"] = list(props)
            for unsupported in ("default", "minimum", "maximum", "minLength", "maxLength",
                                "minItems", "maxItems", "pattern", "format", "examples"):
                node.pop(unsupported, None)
            for v in node.values():
                visit(v)

    visit(schema)
    return schema


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """把 $ref/$defs 内联，得到自包含 schema（供不支持 json_schema 的端点用文本传 schema）。"""
    defs = schema.get("$defs", {})

    def resolve(node: Any, seen: tuple[str, ...] = ()) -> Any:
        if isinstance(node, list):
            return [resolve(v, seen) for v in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.split("/")[-1]
            if name in seen:
                return {}
            merged = {k: v for k, v in node.items() if k != "$ref"}
            out = resolve(defs.get(name, {}), seen + (name,))
            if isinstance(out, dict) and merged:
                out.update(resolve(merged, seen))
            return out
        return {k: resolve(v, seen) for k, v in node.items() if k != "$defs"}

    return resolve(schema)


def schema_instruction(schema_model: type[BaseModel]) -> dict[str, Any]:
    """端点不支持严格 json_schema 时，把 schema 作为文本注入，避免模型自造字段名/枚举值。"""
    schema = _inline_refs(provider_strict_schema(schema_model))
    return {"role": "system", "content":
            "必须只输出一个 JSON 对象，且严格符合以下 JSON Schema：字段名与枚举值必须完全一致，"
            "不得新增、改名或省略任何字段。\n" + canonical_json(schema)}


class OpenAICompatibleLLM:
    def __init__(self, settings: LLMSettings,
                 count_tokens: Callable[[list[dict[str, Any]]], int] = estimate_tokens) -> None:
        if settings.api_style not in ("chat_completions", "responses"):
            raise ValueError("无效 LLM_API_STYLE")
        self.s = settings
        self.count_tokens = count_tokens
        self.client = AsyncOpenAI(
            base_url=settings.base_url,
            api_key=settings.api_key,
            timeout=httpx.Timeout(settings.timeout_seconds, connect=settings.connect_timeout_seconds),
            max_retries=0,
        )

    def ensure_budget(self, messages: list[dict[str, Any]]) -> None:
        hard = self.s.context_window - self.s.max_output_tokens - self.s.safety_margin_tokens
        if hard <= 0 or self.count_tokens(messages) > hard:
            raise ContextBudgetExceeded("CONTEXT_BUDGET_EXCEEDED：先压缩 Memory/ContextBuilder")

    async def _send_once(self, messages, schema_name, schema, streaming: bool = False):
        if self.s.api_style == "chat_completions":
            kw: dict[str, Any] = {"model": self.s.model, "messages": messages,
                                  "stream": streaming, self.s.chat_token_param: self.s.max_output_tokens}
            if schema is not None and self.s.supports_json_schema:
                kw["response_format"] = {"type": "json_schema",
                                         "json_schema": {"name": schema_name, "strict": True, "schema": schema}}
            elif schema is not None and self.s.supports_json_object:
                kw["response_format"] = {"type": "json_object"}
            elif schema is not None:
                raise ValueError("MODEL_STRUCTURED_OUTPUT_UNSUPPORTED")
            if self.s.extra_body:
                kw["extra_body"] = self.s.extra_body
            return await self.client.chat.completions.create(**kw)

        kw = {"model": self.s.model, "input": messages,
              "max_output_tokens": self.s.max_output_tokens, "stream": streaming}
        if schema is not None and self.s.supports_json_schema:
            kw["text"] = {"format": {"type": "json_schema", "name": schema_name, "strict": True, "schema": schema}}
        elif schema is not None and self.s.supports_json_object:
            kw["text"] = {"format": {"type": "json_object"}}
        elif schema is not None:
            raise ValueError("MODEL_STRUCTURED_OUTPUT_UNSUPPORTED")
        return await self.client.responses.create(**kw)

    async def _send(self, messages, schema_name, schema, streaming: bool = False):
        for attempt in range(self.s.max_retries + 1):
            try:
                return await self._send_once(messages, schema_name, schema, streaming)
            except APIStatusError as exc:
                retryable = exc.status_code == 429 or 500 <= exc.status_code <= 599
                if not retryable or attempt >= self.s.max_retries:
                    raise
            except APIConnectionError:
                if attempt >= self.s.max_retries:
                    raise
            await asyncio.sleep(min(2.0, 0.3 * (2 ** attempt)))
        raise RuntimeError("unreachable")

    def _text(self, response) -> tuple[str, Any, Any]:
        if self.s.api_style == "chat_completions":
            choice = response.choices[0]
            if getattr(choice.message, "refusal", None):
                raise ModelRefusal("MODEL_REFUSAL")
            if choice.finish_reason == "length":
                raise ModelTruncated("MODEL_TRUNCATED")
            if choice.finish_reason == "content_filter":
                raise ModelFiltered("MODEL_FILTERED")
            return choice.message.content or "", response.usage, choice.finish_reason
        if response.status != "completed":
            raise ValueError(f"MODEL_RESPONSE_{response.status}")
        return response.output_text or "", response.usage, response.status

    async def structured(self, *, messages: list[dict[str, Any]], schema_model: type[T],
                         prompt_version: str, temperature: float = 0.0) -> tuple[T, dict[str, Any]]:
        if not self.s.supports_json_schema:
            # 端点不支持严格 JSON Schema 时 schema 无法由 provider 强制，改为文本注入（§19.3）。
            messages = [*messages, schema_instruction(schema_model)]
        self.ensure_budget(messages)
        schema = provider_strict_schema(schema_model)
        started = time.perf_counter()
        response = await self._send(messages, schema_model.__name__, schema)
        raw, usage, finish = self._text(response)
        try:
            parsed = schema_model.model_validate_json(raw)
        except ValidationError as exc:
            locs = ";".join(".".join(map(str, e["loc"])) for e in exc.errors())
            repair = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": "请仅修复 JSON 字段/类型。错误位置：" + locs},
            ]
            self.ensure_budget(repair)
            rr = await self._send(repair, schema_model.__name__, schema)
            raw, usage, finish = self._text(rr)
            parsed = schema_model.model_validate_json(raw)  # 二次失败直接抛出 -> 转人工
        latency_ms = int((time.perf_counter() - started) * 1000)
        audit = {
            "model": self.s.model,
            "api_style": self.s.api_style,
            "extra_body": self.s.extra_body,
            "latency_ms": latency_ms,
            "prompt_version": prompt_version,
            "request_hash": "sha256:" + hashlib.sha256(canonical_json(messages).encode()).hexdigest(),
            "response_hash": "sha256:" + hashlib.sha256(raw.encode()).hexdigest(),
            "usage": usage.model_dump() if usage is not None else None,
            "finish_reason": finish,
        }
        return parsed, audit

    async def text(self, *, messages: list[dict[str, Any]],
                   prompt_version: str) -> tuple[str, dict[str, Any]]:
        """纯文本补全（非结构化）。用于只读答复的措辞润色等场景。"""
        self.ensure_budget(messages)
        started = time.perf_counter()
        response = await self._send(messages, None, None)
        raw, usage, finish = self._text(response)
        latency_ms = int((time.perf_counter() - started) * 1000)
        audit = {
            "model": self.s.model,
            "api_style": self.s.api_style,
            "extra_body": self.s.extra_body,
            "latency_ms": latency_ms,
            "prompt_version": prompt_version,
            "request_hash": "sha256:" + hashlib.sha256(canonical_json(messages).encode()).hexdigest(),
            "response_hash": "sha256:" + hashlib.sha256(raw.encode()).hexdigest(),
            "usage": usage.model_dump() if usage is not None else None,
            "finish_reason": finish,
        }
        return raw, audit

    async def stream_text(self, *, messages: list[dict[str, Any]],
                          prompt_version: str) -> AsyncIterator[str]:
        self.ensure_budget(messages)
        stream = await self._send(messages, None, None, streaming=True)
        async for event in stream:
            if self.s.api_style == "chat_completions":
                if event.choices:
                    delta = event.choices[0].delta.content
                    if delta:
                        yield delta
            else:
                if getattr(event, "type", "") == "response.output_text.delta":
                    delta = getattr(event, "delta", "")
                    if delta:
                        yield delta


def build_llm(settings: Settings) -> OpenAICompatibleLLM | None:
    """LLM_MODEL 为空 -> 返回 None（LLM_DISABLED：仅只读/人工路径，§2.2）。"""
    if not settings.llm_enabled:
        return None
    return OpenAICompatibleLLM(LLMSettings.from_app(settings))


def build_llm_small(settings: Settings) -> OpenAICompatibleLLM | None:
    """二级模型路由用的小模型客户端。

    LLM_MODEL_SMALL 未配置 -> 返回 None（不做分级，意图识别一律走主模型）。
    可用 LLM_SMALL_BASE_URL / LLM_SMALL_API_KEY 指向与主模型不同的端点。
    """
    if not settings.llm_small_enabled:
        return None
    small = settings.model_copy(update={
        "llm_model": settings.llm_model_small,
        "llm_base_url": settings.llm_small_base_url or settings.llm_base_url,
        "llm_api_key": settings.llm_small_api_key or settings.llm_api_key,
        "llm_max_output_tokens": settings.llm_small_max_output_tokens or settings.llm_max_output_tokens,
        "llm_context_window": settings.llm_small_context_window or settings.llm_context_window,
    })
    return OpenAICompatibleLLM(LLMSettings.from_app(small))
