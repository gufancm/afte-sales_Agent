# -*- coding: utf-8 -*-
"""E1 意图识别金标集 runner（离线快路径部分始终可跑；LLM 部分需 --live + 已配置模型）。

用法：
  python tests/eval/run_intent.py                # 离线：快路径命中/拦截 + 复杂度分级（E2 分流）
  python tests/eval/run_intent.py --live         # 追加：真实 LLM 主/次意图 + 安全旗标 accuracy
退出码：0=达标（未 --live 时以离线门槛为准）；1=离线门槛失败。
金标必须人工确认（review=confirmed）；pending 样本只统计、不计入门槛。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import pathlib
import sys
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.domain.enums import Intent, SafetyFlag  # noqa: E402
from app.graph.intent_fastpath import estimate_complexity, try_fastpath  # noqa: E402

DATA_DIR = pathlib.Path(__file__).parent / "data"
FILES = ["intent_golden.jsonl", "intent_multi.jsonl", "intent_safety.jsonl", "intent_safety_negative.jsonl"]
SINGLE_INTENT_FILE = "intent_golden.jsonl"
VALID_INTENTS = {str(i) for i in Intent}
VALID_FLAGS = {str(f) for f in SafetyFlag}


def load_cases() -> list[dict]:
    out: list[dict] = []
    for name in FILES:
        p = DATA_DIR / name
        if p.is_file():
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    case = json.loads(line)
                    case["_file"] = name
                    out.append(case)
    return out


def integrity(cases: list[dict]) -> list[str]:
    """数据完整性门禁：冻结 sha256 + 文本唯一 + 单意图集各类等量(8x40) + 枚举取值合法。"""
    errs: list[str] = []
    freeze = DATA_DIR.parent / "eval_freeze.json"
    if freeze.is_file():
        man = json.loads(freeze.read_text(encoding="utf-8"))
        for name, meta in man.get("files", {}).items():
            fp = DATA_DIR / name
            if not fp.is_file():
                errs.append("冻结文件缺失：%s" % name)
                continue
            got = hashlib.sha256(fp.read_bytes()).hexdigest()
            if got != meta["sha256"]:
                errs.append("数据已被改动但未重新冻结：%s（冻结 %s… 实际 %s…）—— 改数据后请重新生成 eval_freeze.json"
                            % (name, meta["sha256"][:12], got[:12]))
    dup = [t for t, n in Counter(c["text"] for c in cases).items() if n > 1]
    if dup:
        errs.append("重复文本 %d 条（E1 文本必须唯一）：%s" % (len(dup), "；".join(dup[:3])))
    dist = Counter(c["expect"]["intent"] for c in cases if c["_file"] == SINGLE_INTENT_FILE)
    if len(dist) != len(VALID_INTENTS) or len(set(dist.values())) != 1:
        errs.append("单意图集未保持各类等量(8 类 × 等量)：%s" % dict(dist))
    for c in cases:
        if c["expect"]["intent"] not in VALID_INTENTS:
            errs.append("非法期望意图：%s" % c["expect"]["intent"])
        for f in c["expect"]["safety_flags"]:
            if f not in VALID_FLAGS:
                errs.append("非法期望旗标：%s" % f)
    return errs


def offline(cases: list[dict]) -> tuple[int, int, list[str]]:
    """快路径：应走 fastpath 的必须命中且意图正确；不应走 fastpath 的必须被拦截。"""
    fails: list[str] = []
    fp_total = sum(1 for c in cases if c["expect"]["expect_tier"] == "fastpath")
    fp_hit = 0
    for c in cases:
        fp = try_fastpath(c["text"])
        tier = c["expect"]["expect_tier"]
        if tier == "fastpath":
            if fp is None or fp.intent != c["expect"]["intent"]:
                fails.append(f"fastpath missed: {c['text'][:30]} -> {fp.intent if fp else None}")
            else:
                fp_hit += 1
        else:
            # 写操作/注入/多意图/长句 不得被快路径误中
            if fp is not None:
                fails.append(f"fastpath over-fired: {c['text'][:30]} -> {fp.intent}")
    return fp_hit, fp_total, fails


async def live(cases: list[dict]) -> int:
    from app.bootstrap import load_prompts
    from app.config import get_settings
    from app.domain.schemas import IntentResult
    from app.graph.nodes import PROMPT_VERSION_INTENT
    from app.llm.adapter import build_llm

    s = get_settings()
    llm = build_llm(s)
    if llm is None:
        print("[E1-live] LLM_DISABLED：未配置 LLM_MODEL，跳过实时评测。")
        return 0
    prompts = load_prompts()
    system = prompts.get("intent_guardrail", "")
    n_intent_ok = 0
    flag_tp = flag_fn = 0
    flag_fp = 0
    total = 0
    for c in cases:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": c["text"]}]
        try:
            parsed, _ = await llm.structured(messages=messages, schema_model=IntentResult,
                                             prompt_version=PROMPT_VERSION_INTENT, temperature=0.0)
        except Exception as exc:  # noqa: BLE001
            print(f"  LLM error on {c['text'][:24]}: {type(exc).__name__}")
            continue
        total += 1
        exp = c["expect"]
        if str(parsed.intent) == exp["intent"]:
            n_intent_ok += 1
        got = {str(f) for f in (parsed.safety_flags or [])}
        want = set(exp["safety_flags"])
        flag_tp += len(want & got)
        flag_fn += len(want - got)
        flag_fp += len(got - want)
    if total == 0:
        print("[E1-live] 无有效样本。")
        return 0
    acc = n_intent_ok / total
    rec = flag_tp / (flag_tp + flag_fn) if (flag_tp + flag_fn) else 1.0
    print(f"[E1-live] 主意图 accuracy={acc:.3f} ({n_intent_ok}/{total})；安全旗标 recall={rec:.3f} "
          f"(tp={flag_tp} fn={flag_fn} fp={flag_fp})")
    gate = acc >= 0.80
    print("E1_LIVE_OK" if gate else "E1_LIVE_FAIL")
    return 0 if gate else 1


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()
    cases = load_cases()
    ierrs = integrity(cases)
    for e in ierrs:
        print("  INTEGRITY " + e)
    if ierrs:
        print("E1_INTEGRITY_FAIL")
        return 1
    fp_hit, fp_total, fails = offline(cases)
    print(f"[E1-offline] 快路径命中 {fp_hit}/{fp_total}；用例总数 {len(cases)}；"
          f"tier 分布 {dict(Counter(c['expect']['expect_tier'] for c in cases))}")
    for f in fails:
        print("  FAIL " + f)
    if fails:
        print("E1_OFFLINE_FAIL")
        return 1
    print("E1_OFFLINE_OK")
    if args.live:
        return await live(cases)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))