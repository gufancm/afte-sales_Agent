# -*- coding: utf-8 -*-
"""E9 证据闭包完整性 runner（离线，不需要 DB / LLM）。

断言：每条用例「必备条款集合」能被条款库精确补齐；缺失/版本漂移/空集的行为符合预期，且零编造。
用法：python tests/eval/run_closure.py；退出码 0=全过（CI 硬门槛）。
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.policy.library import load_clauses  # noqa: E402
from app.rag.evidence import validate_coverage  # noqa: E402
from app.rag.memory import InMemoryClauseStore  # noqa: E402

DATA = pathlib.Path(__file__).parent / "data" / "closure_cases.jsonl"


def load_cases() -> list[dict]:
    return [json.loads(line) for line in DATA.read_text(encoding="utf-8").splitlines() if line.strip()]


async def main() -> int:
    store = InMemoryClauseStore(load_clauses())
    cases = load_cases()
    ok = 0
    fails: list[tuple[str, str, str]] = []
    for c in cases:
        required = list(c["required_clause_ids"])
        fetched = {}
        for ch in await store.fetch_many("tenant_demo", required, c.get("policy_version") or ""):
            fetched[ch.clause_id] = ch
        v = validate_coverage(required_clause_ids=required, fetched=fetched,
                              policy_version=c.get("policy_version"))
        problems = []
        if v.status.value != c["expect_status"]:
            problems.append(f"status {v.status.value} != {c['expect_status']}")
        if len(fetched) != c["expect_item_count"]:
            problems.append(f"items {len(fetched)} != {c['expect_item_count']}")
        if problems:
            fails.append((c["id"], c["desc"], "; ".join(problems)))
        else:
            ok += 1
    total = len(cases)
    print(f"[E9] 证据闭包完整性: {ok}/{total} 通过 (clauses={len(store.all())})")
    for cid, desc, why in fails:
        print(f"  FAIL {cid} {desc}: {why}")
    print("E9_CLOSURE_OK" if ok == total else "E9_CLOSURE_FAIL")
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))