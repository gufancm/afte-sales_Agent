"""端点私有参数透传：百炼 enable_thinking 等必须真正进到请求体。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.llm.adapter import LLMSettings, OpenAICompatibleLLM, _parse_extra_body


def test_parse_extra_body_variants():
    assert _parse_extra_body("") is None
    assert _parse_extra_body("   ") is None
    assert _parse_extra_body('{"enable_thinking": false}') == {"enable_thinking": False}
    with pytest.raises(ValueError, match="合法 JSON"):
        _parse_extra_body("{enable_thinking: false}")
    with pytest.raises(ValueError, match="JSON 对象"):
        _parse_extra_body("[1, 2]")


def test_settings_reads_extra_body_from_env(monkeypatch):
    monkeypatch.setenv("LLM_EXTRA_BODY", '{"enable_thinking": false}')
    settings = Settings(_env_file=None, llm_model="deepseek-v4-pro")
    assert LLMSettings.from_app(settings).extra_body == {"enable_thinking": False}


@pytest.mark.asyncio
async def test_extra_body_reaches_chat_completions_call():
    captured: list[dict] = []

    class _Completions:
        async def create(self, **kw):
            captured.append(kw)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="{}", refusal=None),
                                          finish_reason="stop")],
                usage=None,
            )

    llm = OpenAICompatibleLLM(LLMSettings(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="sk-test", model="deepseek-v4-pro", api_style="chat_completions",
        context_window=131072, max_output_tokens=1024,
        supports_json_schema=False, supports_json_object=True, timeout_seconds=10,
        extra_body={"enable_thinking": False},
    ))
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))

    await llm._send_once([{"role": "user", "content": "hi"}], None, None)

    assert captured[0]["extra_body"] == {"enable_thinking": False}
    assert captured[0]["model"] == "deepseek-v4-pro"


@pytest.mark.asyncio
async def test_no_extra_body_key_when_unset():
    captured: list[dict] = []

    class _Completions:
        async def create(self, **kw):
            captured.append(kw)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="", refusal=None),
                                          finish_reason="stop")],
                usage=None,
            )

    llm = OpenAICompatibleLLM(LLMSettings(
        base_url="https://api.example.com/v1", api_key="sk-test", model="m",
        api_style="chat_completions", context_window=32768, max_output_tokens=1024,
        supports_json_schema=False, supports_json_object=True, timeout_seconds=10,
    ))
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))

    await llm._send_once([{"role": "user", "content": "hi"}], None, None)

    assert "extra_body" not in captured[0]
