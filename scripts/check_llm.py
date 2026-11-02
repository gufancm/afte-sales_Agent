"""LLM 连通性与结构化输出自检（真实端点，不需要 DB）。

用法：
    docker compose run --rm api python scripts/check_llm.py
    docker compose run --rm api python scripts/check_llm.py "衣服有质量问题想退款"
    docker compose run --rm api python scripts/check_llm.py --small "快递到哪了"   # 自检小模型

退出码：0=OK；2=LLM_DISABLED；3=认证失败；4=模型/请求被拒；5=网络超时；6=结构化输出校验失败。
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from pydantic import ValidationError

from app.bootstrap import load_prompts
from app.config import get_settings
from app.domain.schemas import IntentResult
from app.graph.nodes import PROMPT_VERSION_INTENT
from app.llm.adapter import build_llm, build_llm_small

DEFAULT_TEXT = "我买的那件衣服有质量问题，想退款"


def _mask(s: str) -> str:
    if not s:
        return "(empty)"
    return (s[:6] + "..." + s[-4:]) if len(s) > 12 else "***"


async def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    use_small = "--small" in sys.argv
    text = args[0] if args else DEFAULT_TEXT
    s = get_settings()
    tier = "small" if use_small else "large"
    model = s.llm_model_small if use_small else s.llm_model
    print(f"APP_ENV={s.app_env} api_style={s.llm_api_style} tier={tier} model={model}")
    print(f"base_url={s.llm_base_url} api_key={_mask(s.llm_api_key)}")
    print(f"context_window={s.llm_context_window} max_output={s.llm_max_output_tokens} "
          f"json_schema={s.llm_supports_json_schema} json_object={s.llm_supports_json_object}")
    viol = s.startup_violations()
    print("startup_violations:", viol)

    llm = build_llm_small(s) if use_small else build_llm(s)
    if llm is None:
        print("LLM_DISABLED: 模型名为空或占位符"
              + ("（小模型未配置 LLM_MODEL_SMALL）" if use_small else ""))
        return 2

    prompts = load_prompts()
    messages = [
        {"role": "system", "content": prompts.get("intent_guardrail", "")},
        {"role": "user", "content": text},
    ]
    print(f"\n--- 发送意图识别请求 ---\nuser: {text}")
    t0 = time.perf_counter()
    try:
        parsed, audit = await llm.structured(
            messages=messages, schema_model=IntentResult,
            prompt_version=PROMPT_VERSION_INTENT, temperature=0.0,
        )
    except ValidationError as exc:
        print("SCHEMA_ERROR(二次校验仍失败):", str(exc)[:400])
        return 6
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        msg = str(exc)
        low = msg.lower()
        print(f"UPSTREAM_ERROR type={name}: {msg[:400]}")
        if "401" in msg or "authentication" in low or "invalid api key" in low or "unauthorized" in low:
            print("=> 认证失败: 检查 LLM_API_KEY")
            return 3
        if "404" in msg or ("model" in low and ("not found" in low or "does not exist" in low or "invalid" in low)):
            print("=> 模型不存在: 检查 LLM_MODEL 是否为该平台支持的模型名")
            return 4
        if "json_schema" in low or "response_format" in low or " 400" in msg or msg.startswith("400"):
            print("=> 端点可能不支持 response_format=json_schema (试 LLM_SUPPORTS_JSON_SCHEMA=false)")
            return 4
        if "timeout" in low or "connection" in low:
            print("=> 网络/超时: 检查容器出网与 base_url")
            return 5
        return 5

    dt = (time.perf_counter() - t0) * 1000
    print(f"\n--- 结果 ({dt:.0f} ms) ---")
    print(json.dumps(parsed.model_dump(mode="json"), ensure_ascii=False, indent=2))
    print("audit:", json.dumps(audit, ensure_ascii=False))
    print("\nLLM_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))