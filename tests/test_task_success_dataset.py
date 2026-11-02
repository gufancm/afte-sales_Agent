# -*- coding: utf-8 -*-
"""Task Success v2 数据集契约：数量、四要素、授权口径、manifest 与复核表一致性。"""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "tests" / "eval" / "data"
TARGET = {"仅退款": 20, "退货退款": 20, "在途拦截退款": 15, "补发商品": 15,
          "催发货与物流查询": 10, "投诉和人工转接": 10, "多轮澄清、异常与失败恢复": 10}
REFUND = {"REQUEST_REFUND_ONLY", "REQUEST_RETURN_REFUND"}


def _cases() -> list[dict]:
    text = (DATA / "task_success_v2.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_task_success_v2_contract() -> None:
    cases = _cases()
    assert len(cases) == 100
    assert len({c["case_id"] for c in cases}) == 100
    assert dict(Counter(c["group"] for c in cases)) == TARGET
    for c in cases:
        assert c["schema_version"] == "2.0.0"
        assert c["turns"] and all(t.get("user") for t in c["turns"])
        assert all(k in c["fixture"] for k in ("order", "logistics", "finance", "claim"))
        e = c["expect_final"]
        assert e.get("response_type") and "commands" in e
        assert c["expected_business_result"] and c["expected_steps"]
        assert "DUPLICATE_COMMAND" in c["forbidden"]
        assert "UNAPPROVED_FUND_MOVEMENT" in c["forbidden"]
        # 授权口径：有命令 <=> 请求动作在 authorized_actions 内；否则必须无命令
        if e["commands"]:
            assert e["policy_decision"] == "ALLOW"
            assert set(e["commands"]) <= set(e.get("authorized_actions") or [])
            assert e["response_type"] == "PROCESSING"
            assert "ANY_WRITE_COMMAND" not in c["forbidden"]
        else:
            assert "ANY_WRITE_COMMAND" in c["forbidden"]
        # 业务终态只允许声明在退款命令上
        biz = e.get("business_expect") or {}
        if biz.get("applicable"):
            assert set(e["commands"]) <= REFUND


def test_approval_requirement_is_derived_from_policy_cap() -> None:
    """无金额上限的退款 / 补发必须要求审批；拦截不需要。"""
    for c in _cases():
        e = c["expect_final"]
        cmds = set(e.get("commands") or [])
        if not cmds:
            continue
        if cmds & REFUND or "CREATE_RESHIPMENT" in cmds:
            assert e["approval_required"] is True, c["case_id"]
        if cmds == {"INTERCEPT_PARCEL"}:
            assert e["approval_required"] is False, c["case_id"]


def test_hitl_dataset_has_approve_and_reject() -> None:
    rows = [json.loads(x) for x in (DATA / "task_success_hitl_v1.jsonl").read_text(
        encoding="utf-8").splitlines() if x.strip()]
    assert len(rows) >= 3
    modes = {r["expect_final"].get("approval_expect") for r in rows}
    assert {"APPROVE", "REJECT"} <= modes
    reject = [r for r in rows if r["expect_final"].get("approval_expect") == "REJECT"]
    for r in reject:
        assert r["expect_final"]["commands"] == []
        assert r["expect_final"]["response_type"] == "DENIED"
        assert "ANY_WRITE_COMMAND" in r["forbidden"]


def test_manifest_hashes_and_review_ids_match() -> None:
    manifest = json.loads((DATA / "task_success_manifest.json").read_text(encoding="utf-8"))
    assert manifest["cases"] == 100
    assert manifest["files"]["cases"]["sha256"] == hashlib.sha256(
        (DATA / "task_success_v2.jsonl").read_bytes()).hexdigest()
    assert manifest["files"]["fixtures"]["sha256"] == hashlib.sha256(
        (DATA / "task_success_fixtures.json").read_bytes()).hexdigest()
    ids = {c["case_id"] for c in _cases()}
    # 复核表可能以两种命名保留（英文 review 表 / 中文人工核对表），任一存在即可。
    candidates = [ROOT / "reports" / "task-success-review-v2.csv",
                  ROOT / "reports" / "task-success-v2-人工核对表.csv"]
    path = next((p for p in candidates if p.exists()), None)
    assert path is not None, "100 条人工复核表缺失"
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 100
    key = "case_id" if "case_id" in rows[0] else "用例"
    assert {r[key] for r in rows} == ids


def test_every_policy_rule_declares_allowed_actions() -> None:
    """规则必须显式声明动作授权；ALLOW 规则不能缺省为空后又被当作可执行。"""
    rules = json.loads((ROOT / "policy" / "rules.json").read_text(encoding="utf-8"))["rules"]
    assert rules
    for r in rules:
        assert "allowed_actions" in r, r["rule_id"]
        if r["decision"] != "ALLOW":
            assert r["allowed_actions"] == [], r["rule_id"]
