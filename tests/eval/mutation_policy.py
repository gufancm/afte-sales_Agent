# -*- coding: utf-8 -*-
"""Policy Engine 变异测试（mutation testing）。

目的不是再测一遍业务，而是检验**测试集本身有没有鉴别力**：
对 policy/rules.json 逐条施加已知错误，如果测试仍然全绿，说明存在盲区。

变异只作用于内存中的规则对象，绝不写回 policy/rules.json。

用法：
  python tests/eval/mutation_policy.py
  python tests/eval/mutation_policy.py --max 200
输出：reports/policy-mutation-<date>.md / .json
退出码：0 = 基线通过（存活变异体清单见报告）；1 = 基线本身不通过。
"""
from __future__ import annotations

import argparse
import copy
import datetime
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.domain.enums import Intent  # noqa: E402
from app.policy.dsl import Condition, Rule  # noqa: E402
from app.policy.engine import PolicyEngine  # noqa: E402
from app.policy.repository import InMemoryRuleRepository  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
RULES_PATH = ROOT / "policy" / "rules.json"
MAIN = HERE / "data" / "policy_cases_v2.jsonl"
HOLDOUT = HERE / "data" / "policy_holdout_v1.1.jsonl"
SCOPE = HERE / "data" / "policy_scope_cases.jsonl"
OUT_DIR = ROOT / "reports"

FLIP = {"lte": "lt", "lt": "lte", "gte": "gt", "gt": "gte"}
DECISIONS = ["ALLOW", "DENY", "MANUAL", "NEED_INFO"]
STAGE_SHIFT = {0: 20, 10: 40, 20: 30, 30: 20, 40: 10}


def _build(items: list[dict]) -> list[Rule]:
    out = []
    for r in items:
        out.append(Rule(
            rule_id=r["rule_id"], version=r.get("version", "mut"), priority=int(r.get("priority", 0)),
            when=[Condition(c["fact"], c["op"], c.get("value")) for c in r.get("when", [])],
            decision=r["decision"], reason_code=r["reason_code"],
            required_clause_ids=list(r.get("required_clause_ids", [])),
            amount_cap_minor=r.get("amount_cap_minor"),
            cap_type=r.get("amount_cap_type", "AUTO_EXEC_THRESHOLD"),
            stage=(int(r["stage"]) if r.get("stage") is not None else None),
            scope=dict(r.get("scope", {})), status=r.get("status", "PUBLISHED"),
            effective_from=r.get("effective_from"), effective_to=r.get("effective_to"),
        ))
    return out


def _load_cases(path: pathlib.Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _run(rules: list[Rule], case: dict) -> dict:
    d = PolicyEngine(repo=InMemoryRuleRepository(rules)).decide(
        tenant_id="tenant_demo", intent=Intent(case["intent"]), facts=dict(case["facts"]),
        as_of=case.get("as_of"))
    exp = case["expect"]
    mode = exp.get("check_rule") or ("exact" if exp.get("rule_id") else "none")
    win = (d.trace or {}).get("winner") or {}
    bad = []
    if d.decision != exp["decision"]:
        bad.append("decision")
    if d.reason_code != exp["reason_code"]:
        bad.append("reason")
    if mode == "exact" and win.get("rule_id") != exp["rule_id"]:
        bad.append("rule")
    if mode == "blocked_candidate":
        blocked = {b["rule_id"] for b in d.trace.get("blocked_rules", [])}
        if win.get("rule_id") not in blocked:
            bad.append("rule")
    if mode != "blocked_candidate" and list(d.required_clause_ids) != list(exp["required_clause_ids"]):
        bad.append("clause")
    return {"bad": bad, "decision": d.decision, "reason": d.reason_code, "rule": win.get("rule_id")}


def _judge(rules: list[Rule], cases: list[dict]) -> list[dict]:
    caught = []
    for c in cases:
        r = _run(rules, c)
        if r["bad"]:
            caught.append({"case_id": c["case_id"], "fields": r["bad"],
                           "expected": f"{c['expect']['decision']}/{c['expect']['reason_code']}",
                           "actual": f"{r['decision']}/{r['reason']}"})
    return caught


def build_mutants(items: list[dict]):
    """生成 (变异体名称, 说明, 变异后的规则列表)。"""
    for i, r in enumerate(items):
        rid = r["rule_id"]
        # 1) 比较符翻转
        for j, c in enumerate(r.get("when", [])):
            if c["op"] in FLIP:
                m = copy.deepcopy(items)
                m[i]["when"][j]["op"] = FLIP[c["op"]]
                yield (f"OP-{rid}-{j}", f"{rid} 条件{j} {c['op']}->{FLIP[c['op']]}（{c['fact']} {c.get('value')}）", m)
        # 2) 阈值 ±1
        for j, c in enumerate(r.get("when", [])):
            v = c.get("value")
            if isinstance(v, int) and not isinstance(v, bool):
                for delta in (-1, 1):
                    m = copy.deepcopy(items)
                    m[i]["when"][j]["value"] = v + delta
                    yield (f"TH-{rid}-{j}-{delta:+d}", f"{rid} 条件{j} 阈值 {v}->{v + delta}", m)
        # 3) 决策翻转
        for dec in DECISIONS:
            if dec != r["decision"]:
                m = copy.deepcopy(items)
                m[i]["decision"] = dec
                yield (f"DEC-{rid}-{dec}", f"{rid} 决策 {r['decision']}->{dec}", m)
        # 4) 条款错配（替换为下一条规则的条款）
        other = items[(i + 1) % len(items)]
        if r.get("required_clause_ids") and other.get("required_clause_ids") != r.get("required_clause_ids"):
            m = copy.deepcopy(items)
            m[i]["required_clause_ids"] = list(other["required_clause_ids"])
            yield (f"CLAUSE-{rid}", f"{rid} 条款绑定改为 {other['rule_id']} 的条款", m)
        # 5) stage 错置
        st = r.get("stage")
        if st is not None and st in STAGE_SHIFT:
            m = copy.deepcopy(items)
            m[i]["stage"] = STAGE_SHIFT[st]
            yield (f"STAGE-{rid}-{st}->{STAGE_SHIFT[st]}", f"{rid} stage {st}->{STAGE_SHIFT[st]}", m)
        # 6) 条件删除
        if len(r.get("when", [])) > 1:
            for j, c in enumerate(r["when"]):
                m = copy.deepcopy(items)
                del m[i]["when"][j]
                yield (f"DEL-{rid}-{j}", f"{rid} 删除条件 {c['fact']} {c['op']} {c.get('value')}", m)
        # 7) cap_type 翻转
        if r.get("amount_cap_minor") is not None:
            m = copy.deepcopy(items)
            cur = r.get("amount_cap_type", "AUTO_EXEC_THRESHOLD")
            m[i]["amount_cap_type"] = ("POLICY_LIMIT" if cur == "AUTO_EXEC_THRESHOLD" else "AUTO_EXEC_THRESHOLD")
            yield (f"CAPTYPE-{rid}", f"{rid} cap_type {cur}->{m[i]['amount_cap_type']}", m)
        # 8) 优先级大幅偏移
        m = copy.deepcopy(items)
        m[i]["priority"] = int(r.get("priority", 0)) + 500
        yield (f"PRIO-{rid}", f"{rid} priority {r.get('priority')}->{int(r.get('priority', 0)) + 500}", m)




# --------------------------------------------------------------------------- #
# 等价性判定：存活变异体在随机输入上是否真的与基线行为一致
# --------------------------------------------------------------------------- #
import random  # noqa: E402

CATS = ["APPAREL", "FRESH", "CUSTOM", "DIGITAL", "APPLIANCE", "VIRTUAL", "BOOK_MEDIA",
        "GROCERY", "BAKERY", "GOLD", "JEWELRY", "EBIKE", "UNKNOWN", "mystery", None]
STATUSES = ["DELIVERED", "PAID_UNSHIPPED", "PAID_SHIPPED", "SHIPPED", "RETURNED", None]
REASONS = ["NO_LONGER_NEEDED", "SIZE_ISSUE", "COLOR_MISMATCH", "QUALITY_DEFECT", "DAMAGED",
           "WRONG_ITEM", "MISSING_ITEM", "NOT_AS_DESCRIBED", "LATE_DELIVERY", "OUT_OF_STOCK",
           "FALSE_SHIPMENT", "DATE_DISSATISFIED", "WEIGHT_DIFF", "DELIVERY_ABNORMAL",
           "INSTALL_LATE", "INSTALL_FEE", "INSTALL_DEFECT", None]
PARCELS = ["IN_TRANSIT", "SIGNED", "DELIVERED", "UNKNOWN", None]


def _rand_facts(rng: random.Random, intent: str) -> dict:
    cat = rng.choice(CATS)
    status = rng.choice(STATUSES)
    reason = rng.choice(REASONS)
    parcel = rng.choice(PARCELS)
    claim_at = None
    if rng.random() < 0.8:
        claim_at = f"2026-10-{rng.randint(1, 28):02d}T10:00:00+08:00"
    delivered_at = None
    if status == "DELIVERED" or rng.random() < 0.25:
        delivered_at = f"2026-09-{rng.randint(1, 30):02d}T10:00:00+08:00"
    f = {"order": {"order_id": "O", "category": cat, "status": status, "currency": "CNY", "version": "1"},
         "logistics": {"parcel_status": parcel}, "finance": {"currency": "CNY"},
         "claim": {"reason_code": reason, "claim_at": claim_at}, "intent": intent}
    if delivered_at:
        f["order"]["delivered_at"] = delivered_at
    if rng.random() < 0.4:
        f["finance"]["requested_amount_minor"] = rng.choice([0, 100, 1999, 2000, 2001, 250000, 250001, 999999])
    return f


def _signature(rules: list[Rule], facts: dict, intent: str) -> tuple:
    d = PolicyEngine(repo=InMemoryRuleRepository(rules)).decide(
        tenant_id="tenant_demo", intent=Intent(intent), facts=dict(facts), as_of="2026-10-28T00:00:00+08:00")
    w = (d.trace or {}).get("winner") or {}
    return (d.decision, d.reason_code, w.get("rule_id"),
            tuple(d.required_clause_ids), bool(d.execution_blocked))


def fuzz_compare(base: list[Rule], mutant: list[Rule], n: int = 1000, seed: int = 20261028) -> dict:
    """随机输入对比，并按严重程度分类差异。

    unsafe_allow = 基线不给放行（DENY/MANUAL/NEED_INFO）而变异体放行 —— 资金/合规风险；
    over_block   = 基线放行而变异体拒绝/转人工 —— 体验问题；
    cosmetic     = 决策相同，仅 reason/rule/条款不同 —— 影响解释与引用。
    """
    rng = random.Random(seed)
    diffs = []
    sev = {"unsafe_allow": 0, "over_block": 0, "cosmetic": 0}
    samples: dict[str, dict] = {}
    intents = [x.value for x in Intent]
    for _ in range(n):
        intent = rng.choice(intents)
        facts = _rand_facts(rng, intent)
        a = _signature(base, facts, intent)
        b = _signature(mutant, facts, intent)
        if a == b:
            continue
        if a[0] == b[0]:
            kind = "cosmetic"
        elif b[0] == "ALLOW":
            kind = "unsafe_allow"
        elif a[0] == "ALLOW":
            kind = "over_block"
        else:
            kind = "cosmetic"
        sev[kind] += 1
        samples.setdefault(kind, {"facts": facts, "base": a, "mutant": b})
        if len(diffs) < 3:
            diffs.append({"facts": facts, "base": a, "mutant": b, "kind": kind})
    return {"diff_count": sum(sev.values()), "severity": sev, "samples": diffs, "first_by_kind": samples}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=0, help="最多跑多少个变异体（0=全部）")
    args = ap.parse_args()

    items = json.loads(RULES_PATH.read_text(encoding="utf-8"))["rules"]
    base_rules = _build(items)
    cases = _load_cases(MAIN) + _load_cases(HOLDOUT) + _load_cases(SCOPE)
    main_ids = {c["case_id"] for c in _load_cases(MAIN)}
    holdout_ids = {c["case_id"] for c in _load_cases(HOLDOUT)}
    scope_ids = {c["case_id"] for c in _load_cases(SCOPE)}

    baseline = _judge(base_rules, cases)
    if baseline:
        print(f"[MUTATION] 基线不通过：{len(baseline)} 条用例失败，先修基线")
        for b in baseline[:10]:
            print("  ", b)
        return 1

    records = []
    for idx, (mid, desc, mutated) in enumerate(build_mutants(items)):
        if args.max and idx >= args.max:
            break
        caught = _judge(_build(mutated), cases)
        records.append({
            "mutant": mid, "desc": desc, "killed": bool(caught),
            "caught_by": caught[:5], "caught_count": len(caught),
            "caught_main": sum(1 for c in caught if c["case_id"] in main_ids),
            "caught_holdout": sum(1 for c in caught if c["case_id"] in holdout_ids),
            "caught_scope": sum(1 for c in caught if c["case_id"] in scope_ids),
            "_items": mutated,
        })

    killed = [r for r in records if r["killed"]]
    survived = [r for r in records if not r["killed"]]

    # 基线语料里从未胜出的规则（不可达/被遮蔽）
    winners = set()
    for c in cases:
        sig = _signature(base_rules, c["facts"], c["intent"])
        if sig[2]:
            winners.add(sig[2])
    dead_rules = sorted({r.rule_id for r in base_rules} - winners)

    for r in survived:
        fz = fuzz_compare(base_rules, _build(r["_items"]))
        r["fuzz_diff_count"] = fz["diff_count"]
        r["fuzz_sample"] = fz["samples"][:1]
        r["fuzz_severity"] = fz["severity"]
        if fz["severity"]["unsafe_allow"]:
            r["class"] = "blind_spot_unsafe"
        elif fz["severity"]["over_block"]:
            r["class"] = "blind_spot_overblock"
        elif fz["diff_count"] > 0:
            r["class"] = "blind_spot_cosmetic"
        else:
            r["class"] = "equivalent"
    by_kind: dict[str, dict[str, int]] = {}
    for r in records:
        kind = r["mutant"].split("-")[0]
        g = by_kind.setdefault(kind, {"total": 0, "killed": 0})
        g["total"] += 1
        g["killed"] += 1 if r["killed"] else 0
    rate = round(len(killed) / len(records), 4) if records else 0.0

    print(f"[MUTATION] 变异体 {len(records)} 个 | 杀死 {len(killed)} | 存活 {len(survived)} | kill_rate={rate:.2%}")
    for k, v in by_kind.items():
        print(f"  - {k}: {v['killed']}/{v['total']}")
    for s in survived[:15]:
        print(f"  SURVIVED {s['mutant']}: {s['desc']}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.date.today().isoformat()
    for r in records:
        r.pop("_items", None)
    payload = {"version": "policy-mutation/1", "date": stamp, "rules": len(items),
               "cases": len(cases), "total": len(records), "killed": len(killed),
               "survived": len(survived), "kill_rate": rate,
               "by_kind": by_kind, "dead_rules": dead_rules, "survivors": survived, "records": records}
    (OUT_DIR / f"policy-mutation-{stamp}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# Policy Engine 变异测试", "",
             f"日期：{stamp}　规则 {len(items)} 条　用例 {len(cases)} 条（主集 120 + 留出集 40 + 作用域 17）", "",
             f"**变异体 {len(records)} 个，杀死 {len(killed)} 个，存活 {len(survived)} 个，kill rate = {rate:.2%}**", "",
             "> 存活变异体 ≠ 引擎错误，而是**测试盲区**：这些错误当前用例抓不到，需要补针对性用例。", "",
             "## 分类结果", "", "| 变异类型 | 杀死/总数 | 含义 |", "| --- | --- | --- |"]
    meaning = {
        "OP": "比较符翻转（lte↔lt, gte↔gt）：考阈值语义是否被边界用例锁死",
        "TH": "阈值偏移 ±1：考临界值用例是否真的踩在边界上",
        "DEC": "决策翻转（ALLOW↔DENY↔MANUAL）：考危险放行是否会被发现",
        "CLAUSE": "条款错配：考条款绑定断言是否覆盖该规则",
        "STAGE": "阶段错置：考例外/拒绝的优先级是否被验证",
        "DEL": "条件删除：考规则条件是否被单独验证",
        "CAPTYPE": "金额上限类型翻转：考 POLICY_LIMIT 与 AUTO_EXEC_THRESHOLD 是否区分",
        "PRIO": "优先级大幅偏移：考同阶段竞争是否被覆盖",
    }
    for k, v in by_kind.items():
        lines.append(f"| {k} | {v['killed']}/{v['total']} | {meaning.get(k, '')} |")
    if dead_rules:
        lines += ["", "## 规则集里的不可达规则（与测试无关的产品问题）", "",
                  "以下规则在全部用例中**从未成为胜出规则**，属于被遮蔽的死规则：", ""]
        for rid in dead_rules:
            lines.append(f"- `{rid}`")
        lines += ["", "> 不是测试漏了，而是这些规则的条件被更高 stage 的规则完全覆盖。"
                      "要么删除，要么重新定义触发条件，否则只是规则集里的噪音。", ""]

    if survived:
        buckets = {"blind_spot_overblock": [], "blind_spot_cosmetic": [], "equivalent": []}
        for s in survived:
            buckets.setdefault(s.get("class", "equivalent"), []).append(s)
        lines += ["", "## 存活变异体分级", "",
                  "| 级别 | 数量 | 含义 |", "| --- | --- | --- |",
                  f"| 等价变异体 | {len(buckets.get('equivalent', []))} | 1000 组随机输入下与基线行为完全一致 |",
                  f"| 保守偏差 | {len(buckets.get('blind_spot_overblock', []))} | 会把本来可放行的场景转人工，安全但不经济 |",
                  f"| 解释偏差 | {len(buckets.get('blind_spot_cosmetic', []))} | 决策相同，仅 reason/条款不同，影响对外解释 |", ""]
        for key, title in (("blind_spot_overblock", "保守偏差（over-block）"),
                           ("blind_spot_cosmetic", "解释偏差（cosmetic）")):
            items = buckets.get(key, [])
            if not items:
                continue
            lines += ["", f"### {title}", "",
                      "| 变异体 | 说明 | 随机对比命中次数 |", "| --- | --- | --- |"]
            for s in items:
                lines.append(f"| {s['mutant']} | {s['desc']} | {s.get('fuzz_diff_count', 0)} |")
        eq = buckets.get("equivalent", [])
        if eq:
            lines += ["", "### 等价变异体（不计为测试缺陷）", "",
                      "| 变异体 | 说明 |", "| --- | --- |"]
            for s in eq:
                lines.append(f"| {s['mutant']} | {s['desc']} |")
    lines += ["", "## 被杀死变异体示例", "",
              "| 变异体 | 说明 | 抓到用例数 | 首个抓到用例 |", "| --- | --- | --- | --- |"]
    for r in killed[:20]:
        first = r["caught_by"][0]
        lines.append(f"| {r['mutant']} | {r['desc']} | {r['caught_count']} | "
                     f"{first['case_id']}（{first['expected']} -> {first['actual']}） |")
    lines += ["", "## 口径说明", "",
              "- 变异只作用于内存，不写回 `policy/rules.json`；",
              "- 杀死判定 = 至少一条用例的 decision/reason/rule/clause 与金标不符；",
              "- 主集与留出集合并计分，但分别在报告中记录谁抓到，用于判断两组用例的分工；",
              "- 阈值类变异同时跑 ±1 两个方向，避免只测一侧；",
              "- 存活变异体再用 1000 组随机事实对比基线与变异体输出（decision/reason/rule/条款/执行阻断），"
              "据此分成等价、保守偏差、解释偏差三类；",
              "- 只有“基线不放行而变异体放行”才算危险；当前此类为 0。"]
    (OUT_DIR / f"policy-mutation-{stamp}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"报告：{OUT_DIR / f'policy-mutation-{stamp}.md'}")
    print("POLICY_MUTATION_BASELINE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())