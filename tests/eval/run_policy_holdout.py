# -*- coding: utf-8 -*-
"""独立留出集 runner（Policy Engine v1）。

与 E6 主集（policy_cases_v2.jsonl）严格分离：
  - 金标在首次执行**之前**按 17 份 PDF 条款写出（basis 字段），未用引擎输出回填；
  - 选样只挑主集未覆盖的事实组合（novelty 字段）；
  - 失败时先判定"金标错"还是"引擎缺陷"，不允许改金标凑通过。

用法：
  python tests/eval/run_policy_holdout.py
  python tests/eval/run_policy_holdout.py --verbose
退出码：0=全部通过且无危险放行；1=存在失败。
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
DATA = HERE / "data" / "policy_holdout_v1.1.jsonl"
GOLD_V1_0 = HERE / "data" / "policy_holdout_v1.0.jsonl"
OUT_DIR = ROOT / "reports"
DANGEROUS = {"DENY", "MANUAL", "NEED_INFO"}


def load_cases(path: pathlib.Path) -> list[dict]:
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    ids = [r["case_id"] for r in rows]
    assert len(ids) == len(set(ids)), "holdout case_id 重复"
    assert all(r.get("basis") for r in rows), "每条必须有条款依据 basis"
    return rows


def _pct(a: int, b: int) -> float:
    return round(a / b, 4) if b else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--dataset", default=str(DATA), help="留出集 jsonl 路径（默认 v1.1）")
    args = ap.parse_args()

    rules = load_rules()
    by_id = {r.rule_id: r for r in rules}
    engine = PolicyEngine(repo=InMemoryRuleRepository(rules))
    data_path = pathlib.Path(args.dataset)
    cases = load_cases(data_path)

    rows: list[dict] = []
    fails: list[dict] = []
    dangerous: list[dict] = []
    cnt = dict(decision=0, reason=0, clause=0, rule=0, exec_flag=0, snapshot=0)

    for c in cases:
        exp = c["expect"]
        d1 = engine.decide(tenant_id="tenant_demo", intent=Intent(c["intent"]),
                           facts=dict(c["facts"]), as_of=c.get("as_of"))
        d2 = engine.decide(tenant_id="tenant_demo", intent=Intent(c["intent"]),
                           facts=dict(c["facts"]), as_of=c.get("as_of"))
        trace = d1.trace or {}
        win = trace.get("winner") or {}
        problems: list[str] = []

        if d1.decision != exp["decision"]:
            problems.append(f"decision {d1.decision} != {exp['decision']}")
        else:
            cnt["decision"] += 1
        if d1.reason_code != exp["reason_code"]:
            problems.append(f"reason {d1.reason_code} != {exp['reason_code']}")
        else:
            cnt["reason"] += 1
        if (exp.get("check_rule") or "exact") != "blocked_candidate":
            if list(d1.required_clause_ids) != list(exp["required_clause_ids"]):
                problems.append(f"clauses {d1.required_clause_ids} != {exp['required_clause_ids']}")
            else:
                cnt["clause"] += 1
        if exp.get("missing_facts") is not None and (exp.get("check_rule") != "blocked_candidate"):
            if sorted(d1.missing_facts) != sorted(exp["missing_facts"]):
                problems.append(f"missing_facts {d1.missing_facts} != {exp['missing_facts']}")
        # 规则校验三种口径：
        #   exact            -> 必须命中金标写死的 rule_id
        #   blocked_candidate-> 缺失的是分流键时，胜出规则只需是 blocked 候选，且条款与规则自洽
        #   none             -> 该场景不应有任何胜出规则（兜底路径）
        mode = exp.get("check_rule") or ("exact" if exp.get("rule_id") else "none")
        if mode == "exact":
            if win.get("rule_id") != exp["rule_id"]:
                problems.append(f"rule {win.get('rule_id')} != {exp['rule_id']}")
            else:
                cnt["rule"] += 1
            if exp["rule_id"] not in by_id:
                problems.append(f"gold rule {exp['rule_id']} 不存在于规则集（金标写错）")
        elif mode == "blocked_candidate":
            blocked_ids = {b["rule_id"] for b in trace.get("blocked_rules", [])}
            rid = win.get("rule_id")
            if rid not in blocked_ids:
                problems.append(f"rule {rid} 不是 blocked 候选（应为 {sorted(blocked_ids)} 之一）")
            else:
                cnt["rule"] += 1
            expected_missing = exp.get("missing_facts") or []
            if expected_missing and sorted(d1.missing_facts) != sorted(expected_missing):
                problems.append(f"missing_facts {d1.missing_facts} != {expected_missing}")
            # 澄清结论不引用条款（下游 clarify 不取证据），条款经 rule_id 反查
            if list(d1.required_clause_ids):
                problems.append(f"NEED_INFO 不应带条款，实际 {d1.required_clause_ids}")
            else:
                cnt["clause"] += 1
        else:
            if win.get("rule_id") not in (None, ""):
                problems.append(f"rule {win.get('rule_id')} != None")

        if bool(d1.execution_blocked) != bool(exp.get("execution_blocked", False)):
            problems.append(f"execution_blocked {d1.execution_blocked} != {exp.get('execution_blocked')}")
        else:
            cnt["exec_flag"] += 1

        replay_ok = (d1.decision == d2.decision and d1.reason_code == d2.reason_code
                     and d1.facts_snapshot_id == d2.facts_snapshot_id
                     and d1.ruleset_hash == d2.ruleset_hash)
        if replay_ok:
            cnt["snapshot"] += 1
        else:
            problems.append("non-deterministic on replay")

        if exp["decision"] in DANGEROUS and d1.decision == "ALLOW":
            dangerous.append({"case_id": c["case_id"], "expected": exp["decision"]})

        rows.append({
            "case_id": c["case_id"], "group": c["group"], "intent": c["intent"],
            "expected": exp["decision"], "expected_reason": exp["reason_code"],
            "actual": d1.decision, "reason": d1.reason_code,
            "expected_rule": exp.get("rule_id"), "rule": win.get("rule_id"),
            "expected_clauses": exp["required_clause_ids"],
            "clauses": list(d1.required_clause_ids),
            "ruleset_hash": d1.ruleset_hash, "facts_snapshot_id": d1.facts_snapshot_id,
            "basis": c["basis"], "novelty": c.get("novelty", ""),
            "problems": problems,
        })
        if problems:
            fails.append(rows[-1])

    total = len(cases)
    def _mode(e: dict) -> str:
        return e.get("check_rule") or ("exact" if e.get("rule_id") else "none")

    decided = sum(1 for c in cases if _mode(c["expect"]) in ("exact", "blocked_candidate"))
    metrics = {
        "total": total,
        "decision_accuracy": _pct(cnt["decision"], total),
        "reason_code_accuracy": _pct(cnt["reason"], total),
        "required_clause_accuracy": _pct(cnt["clause"], total),
        "winner_rule_accuracy": _pct(cnt["rule"], decided),
        "execution_flag_accuracy": _pct(cnt["exec_flag"], total),
        "replay_determinism": _pct(cnt["snapshot"], total),
        "dangerous_release_count": len(dangerous),
        "failed_cases": len(fails),
    }

    by_group: dict[str, dict[str, int]] = {}
    for r in rows:
        g = by_group.setdefault(r["group"], {"total": 0, "pass": 0})
        g["total"] += 1
        g["pass"] += 0 if r["problems"] else 1

    ok = len(fails) == 0 and not dangerous and metrics["replay_determinism"] == 1.0

    print(f"[HOLDOUT-v1] {total - len(fails)}/{total} 通过 | decision={metrics['decision_accuracy']:.2%} "
          f"reason={metrics['reason_code_accuracy']:.2%} clause={metrics['required_clause_accuracy']:.2%} "
          f"rule={metrics['winner_rule_accuracy']:.2%} replay={metrics['replay_determinism']:.2%} "
          f"dangerous={len(dangerous)}")
    for g, v in by_group.items():
        print(f"  - {g}: {v['pass']}/{v['total']}")
    if args.verbose:
        for f in fails:
            print(f"  FAIL {f['case_id']} ({f['group']}): {f['expected']}/{f['expected_reason']}"
                  f"[{f['expected_rule']}] -> {f['actual']}/{f['reason']}[{f['rule']}]"
                  f" | {'; '.join(f['problems'])}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.date.today().isoformat()
    payload = {"version": "policy-holdout-v1", "date": stamp, "rules": len(rules),
               "dataset": str(data_path.relative_to(ROOT)).replace("\\", "/"),
               "metrics": metrics, "by_group": by_group,
               "failed": fails, "dangerous_release": dangerous, "rows": rows}
    (OUT_DIR / f"policy-holdout-v1-{stamp}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# Policy Engine 独立留出集（40 条）", "",
             f"日期：{stamp}　规则数：{len(rules)}　数据集：`{payload['dataset']}`", "",
             "> 金标在首次执行前按 17 份 PDF 条款写出，未用引擎输出回填；"
             "选样只取主集 120 条未覆盖的事实组合。", "",
             "| 指标 | 结果 |", "| --- | --- |",
             f"| 用例总数 | {total} |",
             f"| Decision Accuracy | {metrics['decision_accuracy']:.2%} |",
             f"| Reason Code Accuracy | {metrics['reason_code_accuracy']:.2%} |",
             f"| 条款绑定一致率 | {metrics['required_clause_accuracy']:.2%} |",
             f"| 胜出规则一致率 | {metrics['winner_rule_accuracy']:.2%} |",
             f"| 执行阻断标记一致率 | {metrics['execution_flag_accuracy']:.2%} |",
             f"| 重放确定性 | {metrics['replay_determinism']:.2%} |",
             f"| 危险错误放行次数 | {len(dangerous)} |", "",
             "## 分组通过率", "", "| 分组 | 通过/总数 |", "| --- | --- |"]
    for g, v in by_group.items():
        lines.append(f"| {g} | {v['pass']}/{v['total']} |")
    lines += ["", "## 逐条明细", "",
              "| 用例 | 分组 | 期望 | 实际 | 期望规则 | 实际规则 | 结论 |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        verdict = "通过" if not r["problems"] else "; ".join(r["problems"])
        lines.append(f"| {r['case_id']} | {r['group']} | {r['expected']}/{r['expected_reason']} | "
                     f"{r['actual']}/{r['reason']} | {r['expected_rule'] or '-'} | {r['rule'] or '-'} | {verdict} |")
    if fails:
        lines += ["", "## 失败分析（金标错 vs 引擎缺陷）", ""]
        for f in fails:
            lines.append(f"- `{f['case_id']}`：{'; '.join(f['problems'])}。"
                         f"依据：{f['basis']}；与主集差异：{f['novelty']}")
    # 修订记录：首跑结果、金标错误、引擎缺陷必须一并公开，不能只报最终通过率
    log_path = HERE / "data" / "policy_holdout_gold_log.json"
    if log_path.is_file():
        lg = json.loads(log_path.read_text(encoding="utf-8"))
        first = lg.get("v1_0_first_run", {})
        lines += ["", "## 首跑结果与修订记录", "",
                  f"- v1.0 首跑（金标未改）：**{first.get('passed')}/{first.get('total')} 通过，"
                  f"危险放行 {first.get('dangerous')} 次**；",
                  f"- 未修改的金标：{lg.get('untouched', '-')}。", ""]
        if lg.get("engine_defects_found"):
            lines += ["| 缺陷 | 现象 | 证据 | 修复 |", "| --- | --- | --- | --- |"]
            for d in lg["engine_defects_found"]:
                lines.append(f"| {d['id']} | {d['symptom']} | {d['evidence']} | {d['fix']} |")
            lines.append("")
        if lg.get("attempted_changes_reverted"):
            lines += ["", "已尝试但回退的改动（避免为通过而改冻结金标）：", ""]
            for a in lg["attempted_changes_reverted"]:
                lines.append(f"- `{a['id']}` {a['change']}：{a['why_reverted']}")
            lines.append("")
        if lg.get("corrections"):
            lines += ["| 用例 | 类型 | 原金标 | 修订后 | 依据 |", "| --- | --- | --- | --- | --- |"]
            for c in lg["corrections"]:
                lines.append(f"| {c['case_id']} | {c['type']} | "
                             f"`{c['from'].get('decision')}/{c['from'].get('reason_code')}` | "
                             f"`{c['to'].get('decision')}/{c['to'].get('reason_code')}` | 见 gold_revision 字段 |")
            lines += ["", "> 修订只针对可证明的金标错误（日期算错、规则不读取该事实），"
                          "没有为了让用例通过而放宽判定；`gold_revision` 字段写明了每条的推导过程。"]
    lines += ["", "## 口径说明", "",
              "- 本集与 E6 主集互斥，仅用于检验规则是否被“背题”，不计入主集指标；",
              "- 期望 `rule_id=None` 表示该场景不应有胜出规则（兜底路径），实际也必须无胜出规则；",
              "- 重放确定性 = 同一输入连续两次裁决的 decision / reason / facts_snapshot_id / ruleset_hash 完全一致；",
              "- 失败用例逐条标注是金标问题还是引擎问题，禁止通过修改金标凑通过率。"]
    (OUT_DIR / f"policy-holdout-v1-{stamp}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"报告：{OUT_DIR / f'policy-holdout-v1-{stamp}.md'}")
    print("POLICY_HOLDOUT_OK" if ok else "POLICY_HOLDOUT_FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())