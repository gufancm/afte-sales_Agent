# -*- coding: utf-8 -*-
"""生成人工复核表：把 review=pending 的样本汇总成 markdown，供人工确认金标。

用法：python tests/eval/make_review_table.py
输出：reports/eval-review-<date>.md
"""
from __future__ import annotations

import datetime
import json
import pathlib

HERE = pathlib.Path(__file__).parent
DATA = HERE / "data"
OUT_DIR = HERE.parents[1] / "reports"


def read_jsonl(name: str) -> list[dict]:
    p = DATA / name
    if not p.is_file():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / ("eval-review-%s.md" % datetime.date.today().isoformat())
    lines = ["# 评测集人工复核表", "",
             "> 用法：逐行核对「期望」是否符合真实业务/政策，在「复核结论」列填 `确认` / `改:<正确的期望>`。",
             "> 确认后把对应数据文件里的 `review` 改为 `confirmed`，即可计入门槛与 CI。", ""]
    intent = []
    for f in ("intent_golden.jsonl", "intent_multi.jsonl", "intent_safety.jsonl"):
        intent += [c for c in read_jsonl(f) if c.get("review") == "pending"]
    lines += ["## E1 意图金标（待复核 %d 条）" % len(intent), "",
              "| # | 文本 | 期望主意图 | 次意图 | 安全旗标 | 期望tier | 复核结论 |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for i, c in enumerate(intent, 1):
        e = c["expect"]
        lines.append("| %d | %s | %s | %s | %s | %s |  |" % (
            i, c["text"], e["intent"], "/".join(e["secondary_intents"]) or "-",
            "/".join(e["safety_flags"]) or "-", e["expect_tier"]))
    pol = [c for c in read_jsonl("policy_cases_v2.jsonl") if c.get("review") == "pending"]
    lines += ["", "## E6 政策决策矩阵（待复核 %d 条）" % len(pol), "",
              "> 含条款原文的完整复核表见 `tests/eval/POLICY-REVIEW.md` / `policy-review-v2.csv`。", "",
              "| ID | 分组 | 说明 | 期望裁决 | 期望 reason | 复核结论 |", "| --- | --- | --- | --- | --- | --- |"]
    for c in pol:
        lines.append("| %s | %s | %s | %s | %s |  |" % (
            c["case_id"], c["group"], c.get("note", ""), c["expect"]["decision"], c["expect"]["reason_code"]))
    adv = [c for c in read_jsonl("adversarial.jsonl") if c.get("review") == "pending"]
    lines += ["", "## E28 注入对抗（待复核 %d 条）" % len(adv), "",
              "| ID | 攻击类型 | 文本 | 预期 | 复核结论 |", "| --- | --- | --- | --- | --- |"]
    for c in adv:
        lines.append("| %s | %s | %s | 零改变/拒绝 |  |" % (c["id"], c["attack_type"], c["text"]))
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("已生成复核表：", out)


if __name__ == "__main__":
    main()