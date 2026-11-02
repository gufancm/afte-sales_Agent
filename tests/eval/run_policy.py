# -*- coding: utf-8 -*-
"""E6 Policy Engine 120 条确定性金标 runner（离线，不需要 DB / LLM）。

用法：
  python tests/eval/run_policy.py                # 跑 120 条并打印指标
  python tests/eval/run_policy.py --verbose      # 打印每条失败原因
输出：reports/policy-eval-v2-<date>.md / .json
退出码：0=120 条全部通过且无危险放行；1=存在失败。
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.domain.enums import Intent  # noqa: E402
from app.policy.engine import PolicyEngine  # noqa: E402
from app.policy.library import load_rules  # noqa: E402
from app.policy.repository import InMemoryRuleRepository  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
DATA = HERE / "data" / "policy_cases_v2.jsonl"
OUT_DIR = ROOT / "reports"
GATE_DECISION = 1.0          # 确定性模块：冻结规则后必须全通过
GATE_CONDITION = 1.0
DANGEROUS_EXPECT = {"DENY", "MANUAL", "NEED_INFO"}


def load_cases() -> list[dict]:
    return [json.loads(line) for line in DATA.read_text(encoding="utf-8").splitlines() if line.strip()]


def _pct(n: int, d: int) -> float:
    return round(n / d, 4) if d else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    rules = load_rules()
    engine = PolicyEngine(repo=InMemoryRuleRepository(rules))
    cases = load_cases()

    rows: list[dict] = []
    fails: list[dict] = []
    dangerous: list[dict] = []
    over_blocks: list[dict] = []
    n = dict(decision=0, reason=0, clause=0, rule=0, condition=0, version=0, exec_flag=0)

    for c in cases:
        exp = c["expect"]
        d = engine.decide(tenant_id="tenant_demo", intent=Intent(c["intent"]), facts=dict(c["facts"]),
                          as_of=c.get("as_of"))
        d2 = engine.decide(tenant_id="tenant_demo", intent=Intent(c["intent"]), facts=dict(c["facts"]),
                           as_of=c.get("as_of"))
        winner = (d.trace or {}).get("winner") or {}
        problems: list[str] = []
        if d.decision != exp["decision"]:
            problems.append(f"decision {d.decision} != {exp['decision']}")
        else:
            n["decision"] += 1
        if d.reason_code != exp["reason_code"]:
            problems.append(f"reason {d.reason_code} != {exp['reason_code']}")
        else:
            n["reason"] += 1
        if list(d.required_clause_ids) != list(exp["required_clause_ids"]):
            problems.append(f"clauses {d.required_clause_ids} != {exp['required_clause_ids']}")
        else:
            n["clause"] += 1
        if exp.get("rule_id"):
            if winner.get("rule_id") != exp["rule_id"]:
                problems.append(f"rule {winner.get('rule_id')} != {exp['rule_id']}")
            else:
                n["rule"] += 1
            if d.policy_version == "2026.10-jd":
                n["version"] += 1
            else:
                problems.append(f"policy_version {d.policy_version} != 2026.10-jd")
        if bool(d.execution_blocked) != bool(exp.get("execution_blocked", False)):
            problems.append(f"execution_blocked {d.execution_blocked} != {exp.get('execution_blocked')}")
        else:
            n["exec_flag"] += 1

        # 关键条件可判定率：胜出规则的所有条件都必须拿到实际事实值（不允许缺值决策）
        rid = winner.get("rule_id")
        rule = next((r for r in rules if r.rule_id == rid), None)
        cond_ok = True
        if rule is not None and not winner.get("blocked_by_missing_fact"):
            node = next((e for e in d.trace.get("evaluated", []) if e["rule_id"] == rid), {})
            cond_ok = all(not cond.get("missing") for cond in node.get("conditions", []))
        if cond_ok:
            n["condition"] += 1
        else:
            problems.append("winner rule decided with missing fact value")

        deterministic = (d.decision == d2.decision and d.reason_code == d2.reason_code
                         and d.facts_snapshot_id == d2.facts_snapshot_id)
        if not deterministic:
            problems.append("non-deterministic on replay")

        if exp["decision"] in DANGEROUS_EXPECT and d.decision == "ALLOW":
            dangerous.append({"case_id": c["case_id"], "expected": exp["decision"], "actual": d.decision})
        if exp["decision"] == "ALLOW" and d.decision == "DENY":
            over_blocks.append({"case_id": c["case_id"], "reason": d.reason_code})

        rows.append({"case_id": c["case_id"], "group": c["group"], "expected": exp["decision"],
                     "actual": d.decision, "reason": d.reason_code,
                     "expected_reason": exp["reason_code"], "rule": rid,
                     "ruleset_hash": d.ruleset_hash, "loaded_rules": d.trace.get("loaded_rule_count"),
                     "facts_snapshot_id": d.facts_snapshot_id,
                     "problems": problems})
        if problems:
            fails.append(rows[-1])

    total = len(cases)
    decided_total = sum(1 for c in cases if c["expect"].get("rule_id"))
    # 规则集指纹按“实际载入的规则集合”分组校验：不同 as_of 会载入不同规则集（如生效前为空集），
    # 同组必须完全一致；跨组不同是正确行为。
    hashes_by_set: dict[int, set[str]] = {}
    for r in rows:
        hashes_by_set.setdefault(int(r["loaded_rules"] or 0), set()).add(r["ruleset_hash"])
    hash_stable = all(len(v) == 1 for v in hashes_by_set.values())
    metrics = {
        "total": total,
        "decision_accuracy": _pct(n["decision"], total),
        "reason_code_accuracy": _pct(n["reason"], total),
        "required_clause_accuracy": _pct(n["clause"], total),
        "winner_rule_accuracy": _pct(n["rule"], decided_total),
        "key_condition_decidable_rate": _pct(n["condition"], total),
        # 仅在“存在胜出规则”的用例上统计版本一致率（NO_APPLICABLE_POLICY 没有规则可谈版本）
        "policy_version_accuracy": _pct(n["version"], decided_total),
        "execution_flag_accuracy": _pct(n["exec_flag"], total),
        "ruleset_hash_consistency": hash_stable,
        "ruleset_hash_by_size": {str(k): sorted(v) for k, v in hashes_by_set.items()},
        "dangerous_release_count": len(dangerous),
        "over_block_count": len(over_blocks),
        "failed_cases": len(fails),
    }

    by_group: dict[str, dict[str, int]] = {}
    for r in rows:
        g = by_group.setdefault(r["group"], {"total": 0, "pass": 0})
        g["total"] += 1
        g["pass"] += 1 if not r["problems"] else 0

    ok = (len(fails) == 0 and not dangerous and metrics["ruleset_hash_consistency"]
          and metrics["decision_accuracy"] >= GATE_DECISION
          and metrics["key_condition_decidable_rate"] >= GATE_CONDITION)

    print(f"[E6-v2] Policy Engine 金标: {total - len(fails)}/{total} 通过 | decision={metrics['decision_accuracy']:.2%} "
          f"reason={metrics['reason_code_accuracy']:.2%} clause={metrics['required_clause_accuracy']:.2%} "
          f"rule={metrics['winner_rule_accuracy']:.2%} cond={metrics['key_condition_decidable_rate']:.2%} "
          f"dangerous={len(dangerous)}")
    for g, v in by_group.items():
        print(f"  - {g}: {v['pass']}/{v['total']}")
    if args.verbose:
        for f in fails:
            print(f"  FAIL {f['case_id']} ({f['group']}): {f['expected']}/{f['expected_reason']} -> "
                  f"{f['actual']}/{f['reason']} | {'; '.join(f['problems'])}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.date.today().isoformat()
    payload = {"version": "policy-eval-v2", "date": stamp, "rules": len(rules),
               "metrics": metrics, "by_group": by_group, "failed": fails,
               "dangerous_release": dangerous, "over_block": over_blocks}
    (OUT_DIR / f"policy-eval-v2-{stamp}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# Policy Engine 120 条金标评测", "", f"日期：{stamp}　规则数：{len(rules)}", "",
             "| 指标 | 结果 |", "| --- | --- |",
             f"| 用例总数 | {total} |",
             f"| Decision Accuracy | {metrics['decision_accuracy']:.2%} |",
             f"| Reason Code Accuracy | {metrics['reason_code_accuracy']:.2%} |",
             f"| 条款绑定一致率 | {metrics['required_clause_accuracy']:.2%} |",
             f"| 胜出规则一致率 | {metrics['winner_rule_accuracy']:.2%} |",
             f"| 关键条件可判定率 | {metrics['key_condition_decidable_rate']:.2%} |",
             f"| Policy Version Accuracy | {metrics['policy_version_accuracy']:.2%} |",
             f"| 执行阻断标记一致率 | {metrics['execution_flag_accuracy']:.2%} |",
             f"| 危险错误放行次数 | {len(dangerous)} |",
             f"| 规则集指纹一致 | {'是' if metrics['ruleset_hash_consistency'] else '否'} |",
             "", "## 分组通过率", "", "| 分组 | 通过/总数 |", "| --- | --- |"]
    for g, v in by_group.items():
        lines.append(f"| {g} | {v['pass']}/{v['total']} |")
    if fails:
        lines += ["", "## 失败用例", "", "| 用例 | 期望 | 实际 | 原因 |", "| --- | --- | --- | --- |"]
        for f in fails:
            lines.append(f"| {f['case_id']} | {f['expected']}/{f['expected_reason']} | "
                         f"{f['actual']}/{f['reason']} | {'; '.join(f['problems'])} |")
    lines += ["", "## 口径说明", "",
              "- 金标期望值由人工按真实条款逐条写出，未由被测引擎回填；",
              "- 「关键条件可判定率」= 胜出规则的每个条件都拿到了实际事实值，不允许缺值决策；",
              "- 「危险错误放行」= 期望 DENY/MANUAL/NEED_INFO 但实际 ALLOW，必须为 0；",
              "- 规则集指纹由规范化规则内容计算，同一规则集下所有用例必须一致，保证可复现。"]
    (OUT_DIR / f"policy-eval-v2-{stamp}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"报告：{OUT_DIR / f'policy-eval-v2-{stamp}.md'}")
    print("E6_POLICY_OK" if ok else "E6_POLICY_FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())