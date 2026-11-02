# -*- coding: utf-8 -*-
"""生成 Task Success v2 人工复核表 + 数据集清单（manifest）。"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import pathlib
from collections import Counter

ROOT = pathlib.Path(r"E:\agent\aftersale-agent")
DATA = ROOT / "tests" / "eval" / "data"
CASES = DATA / "task_success_v2.jsonl"
FIXTURES = DATA / "task_success_fixtures.json"
REVIEW = ROOT / "reports" / "task-success-review-v2.csv"
CHANGES = json.loads((ROOT / "reports" / "task-success-v2-changes.json").read_text(encoding="utf-8"))
CHANGES_V21 = json.loads((ROOT / "reports" / "task-success-v2.1-changes.json").read_text(encoding="utf-8"))

# 已确认需要后续政策 v3 再核的样本（不擅自改金标，只标记）
PENDING_POLICY = {
    "TS-003": "食品无理由：S5#4 以“一次性密封外包装是否破损”为准，不能按“非生鲜”直接判可适用，需补 order.seal_broken 事实",
    "TS-011": "平板无理由：条款以拆封/激活为条件，当前规则按品类直接拒绝，需补 order.activated 事实后复核",
    "TS-012": "冰箱无理由：家电是否属于不适用清单需按商品详情页标签确认，当前按品类拒绝偏宽",
}

rows = [json.loads(x) for x in io.open(CASES, encoding="utf-8") if x.strip()]


def _sha256_lf(path: pathlib.Path) -> str:
    """跨平台稳定指纹：Windows(CRLF) 与 CI(LF) 归一化后一致。"""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _dup_text(dup: dict | None) -> str:
    if not dup:
        return ""
    want = dup.get("expected") or {}
    parts = [f"{k}={v}" for k, v in want.items()]
    return f"重复提交 {dup.get('turns')} 次，断言 " + "、".join(parts)
changed = {c["case_id"]: c for c in CHANGES["changes"]}
changed.update({cid: c for cid, c in CHANGES_V21["cases"].items()})
FIELDS = ["case_id", "group", "title", "user_text", "intent", "requested_action",
          "policy_decision", "reason_code", "rule_id", "policy_actions", "authorized_actions",
          "gate_reason", "response_type", "run_status", "approval_required", "approval_expect",
          "commands", "business_applicable", "business_terminal", "forbidden",
          "duplicate_submission", "basis", "changed_vs_v1", "change_note", "review_status", "review_note"]

out = []
for c in rows:
    e = c["expect_final"]
    ch = changed.get(c["case_id"])
    status = "pending"
    note = ""
    if c["case_id"] in PENDING_POLICY:
        status = "pending_policy_v3"
        note = PENDING_POLICY[c["case_id"]]
    elif ch:
        status = "corrected"
        note = ch["note"]
        if ch.get("gate_reason"):
            note = f"[{ch['gate_reason']}] " + note
    out.append({
        "case_id": c["case_id"], "group": c["group"], "title": c["title"],
        "user_text": c["turns"][-1]["user"], "intent": c["intent"],
        "requested_action": e.get("requested_action") or "",
        "policy_decision": e.get("policy_decision") or "",
        "reason_code": e.get("reason_code") or "",
        "rule_id": e.get("rule_id") or "",
        "policy_actions": "|".join(e.get("policy_actions") or []),
        "authorized_actions": "|".join(e.get("authorized_actions") or []),
        "gate_reason": e.get("gate_reason") or "",
        "response_type": e["response_type"], "run_status": e["run_status"],
        "approval_required": str(bool(e.get("approval_required"))).lower(),
        "approval_expect": e.get("approval_expect") or ("APPROVE" if e.get("approval_required") else ""),
        "commands": "|".join(e.get("commands") or []),
        "duplicate_submission": _dup_text(c.get("duplicate_submission")),
        "business_applicable": str(bool((e.get("business_expect") or {}).get("applicable"))).lower(),
        "business_terminal": (e.get("business_expect") or {}).get("terminal") or "",
        "forbidden": "|".join(c["forbidden"]),
        "basis": c["basis"],
        "changed_vs_v1": "yes" if ch else "no",
        "change_note": ch["note"] if ch else "",
        "review_status": status, "review_note": note,
    })

with io.open(REVIEW, "w", encoding="utf-8-sig", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=FIELDS)
    w.writeheader(); w.writerows(out)

response = dict(Counter(c["expect_final"]["response_type"] for c in rows))
manifest = {
    "dataset": "task_success_v2", "version": "2.0.0", "date": "2026-10-28",
    "cases": len(rows), "turns": sum(len(c["turns"]) for c in rows),
    "multi_turn_cases": sum(1 for c in rows if len(c["turns"]) > 1),
    "groups": dict(Counter(c["group"] for c in rows)),
    "response_types": response,
    "approval_required": sum(1 for c in rows if c["expect_final"].get("approval_required")),
    "business_applicable": sum(1 for c in rows if (c["expect_final"].get("business_expect") or {}).get("applicable")),
    "commands_total": sum(len(c["expect_final"].get("commands") or []) for c in rows),
    "review_status": dict(Counter(r["review_status"] for r in out)),
    "files": {
        "cases": {"path": "tests/eval/data/task_success_v2.jsonl",
                  "sha256": _sha256_lf(CASES)},
        "fixtures": {"path": "tests/eval/data/task_success_fixtures.json",
                     "sha256": _sha256_lf(FIXTURES)},
        "review": {"path": "reports/task-success-review-v2.csv"},
        "changes": {"path": "reports/task-success-v2-changes.json"},
        "hitl": {"path": "tests/eval/data/task_success_hitl_v1.jsonl"},
    },
    "policy_rules": {"path": "policy/rules.json",
                     "sha256": _sha256_lf(ROOT / "policy" / "rules.json")},
    "metric_note": "Workflow Handling Success 与 End-to-End Business Completion 分开统计；离线自检不等于 live 成绩",
}
io.open(DATA / "task_success_manifest.json", "w", encoding="utf-8", newline="\n").write(
    json.dumps(manifest, ensure_ascii=False, indent=2))
print("review rows", len(out), "status", manifest["review_status"])
print("response", response, "approval", manifest["approval_required"], "business", manifest["business_applicable"])
