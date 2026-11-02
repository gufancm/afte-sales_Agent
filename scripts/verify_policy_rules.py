"""真实规则验证：以 **DB 中的 policy_rules + policy_chunks**（pg 后端）跑决策矩阵，
确认「规则 -> 决策 -> 依据条款」在数据库侧闭环、且条款引用无缺失。

前置：
  1) docker compose up -d 已就绪；
  2) python scripts/ingest_policy.py --rules --replace 已执行（写入 policy/rules.json 的规则）；
  3) 真实语料已在 policy_chunks（policy/clauses_jd.json）。

用法：
  # 容器内（主机名 postgres）
  docker run --rm --network aftersale-agent_default --env-file .env -v ... aftersale-agent-api \
      python scripts/verify_policy_rules.py
  # 宿主机（主机名 localhost）
  set POLICY_APP_DSN=postgresql+asyncpg://aftersale_app:app_pw_change_me@localhost:5432/aftersale
  python scripts/verify_policy_rules.py
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db.engine import make_engine, make_sessionmaker, tenant_transaction  # noqa: E402
from app.domain.enums import Intent  # noqa: E402
from app.policy.db_repository import DbRuleRepository  # noqa: E402
from app.policy.engine import PolicyEngine  # noqa: E402
from app.rag.pg import PgClauseStore  # noqa: E402

APP_DSN = os.environ.get("POLICY_APP_DSN",
                         "postgresql+asyncpg://aftersale_app:app_pw_change_me@localhost:5432/aftersale")
TENANT = os.environ.get("POLICY_TENANT", "tenant_demo")

# name, reason_code, category, status, intent, parcel_status, expect_decision, expect_reason_code
CASES = [
    ("生鲜无理由-拒",      "NO_LONGER_NEEDED", "FRESH",  "DELIVERED",       Intent.RETURN_REFUND,  "UNKNOWN",     "DENY",   "NO_REASON_FRESH_EXEMPT"),
    ("定制无理由-拒",      "NO_LONGER_NEEDED", "CUSTOM", "DELIVERED",       Intent.RETURN_REFUND,  "UNKNOWN",     "DENY",   "NO_REASON_CUSTOM_EXEMPT"),
    ("数码无理由-拒",      "SIZE_ISSUE",       "DIGITAL", "DELIVERED",      Intent.RETURN_REFUND,  "UNKNOWN",     "DENY",   "NO_REASON_DIGITAL_EXEMPT"),
    ("图书密封-拒",        "NO_LONGER_NEEDED", "BOOK_MEDIA", "DELIVERED",   Intent.RETURN_REFUND,  "UNKNOWN",     "DENY",   "NO_REASON_SEALED_EXEMPT"),
    ("服饰无理由-退",      "NO_LONGER_NEEDED", "APPAREL", "DELIVERED",      Intent.RETURN_REFUND,  "UNKNOWN",     "ALLOW",  "NO_REASON_WITHIN_7D"),
    ("服饰尺码-退",        "SIZE_ISSUE",       "APPAREL", "DELIVERED",      Intent.RETURN_REFUND,  "UNKNOWN",     "ALLOW",  "NO_REASON_WITHIN_7D"),
    ("服饰质量-退",        "DAMAGED",          "APPAREL", "DELIVERED",      Intent.RETURN_REFUND,  "UNKNOWN",     "ALLOW",  "QUALITY_RETURN_WINDOW"),
    ("数码质量-仅退款",    "QUALITY_DEFECT",   "DIGITAL", "DELIVERED",      Intent.REFUND_ONLY,    "UNKNOWN",     "ALLOW",  "QUALITY_RETURN_WINDOW"),
    ("质量-未签收维修",    "QUALITY_DEFECT",   "APPAREL", "PAID_SHIPPED",   Intent.RETURN_REFUND,  "UNKNOWN",     "ALLOW",  "QUALITY_REPAIR"),
    ("黄金克重补差",       "WEIGHT_DIFF",      "GOLD",    "DELIVERED",      Intent.REFUND_ONLY,    "UNKNOWN",     "ALLOW",  "GOLD_WEIGHT_COMPENSATION"),
    ("黄金质量-退",        "QUALITY_DEFECT",   "JEWELRY", "DELIVERED",      Intent.RETURN_REFUND,  "UNKNOWN",     "ALLOW",  "GOLD_QUALITY_RETURN"),
    ("电动车质量-退",      "QUALITY_DEFECT",   "EBIKE",   "DELIVERED",      Intent.RETURN_REFUND,  "UNKNOWN",     "ALLOW",  "EBIKE_MOTOR_RETURN"),
    ("未出库取消-普通",    "NO_LONGER_NEEDED", "APPAREL", "PAID_UNSHIPPED", Intent.REFUND_ONLY,    "UNKNOWN",     "ALLOW",  "UNSHIPPED_CANCEL_REFUND"),
    ("未出库取消-生鲜",    "NO_LONGER_NEEDED", "FRESH",   "PAID_UNSHIPPED", Intent.REFUND_ONLY,    "UNKNOWN",     "MANUAL", "SPECIAL_CANCEL_REVIEW"),
    ("在途拦截",           "NO_LONGER_NEEDED", "APPAREL", "PAID_SHIPPED",   Intent.INTERCEPT_REFUND, "IN_TRANSIT", "ALLOW", "INTERCEPT_OK"),
    ("签收后拦截-拒",      "NO_LONGER_NEEDED", "APPAREL", "DELIVERED",      Intent.INTERCEPT_REFUND, "DELIVERED", "DENY",  "INTERCEPT_ALREADY_DELIVERED"),
    ("延迟发货-赔付",      "LATE_DELIVERY",    "GROCERY", "PAID_SHIPPED",   Intent.EXPEDITE,       "UNKNOWN",     "ALLOW",  "LATE_DELIVERY_COMPENSATION"),
    ("缺货-赔付",          "OUT_OF_STOCK",     "GROCERY", "PAID_SHIPPED",   Intent.REFUND_ONLY,    "UNKNOWN",     "ALLOW",  "OUT_OF_STOCK_COMPENSATION"),
    ("虚假发货-赔付",      "FALSE_SHIPMENT",   "GROCERY", "PAID_SHIPPED",   Intent.REFUND_ONLY,    "UNKNOWN",     "ALLOW",  "OUT_OF_STOCK_COMPENSATION"),
    ("配送异常-转人工",    "DELIVERY_ABNORMAL", "GROCERY", "PAID_SHIPPED",  Intent.EXPEDITE,       "UNKNOWN",     "MANUAL", "DELIVERY_ABNORMAL_REVIEW"),
    ("安装纠纷-转人工",    "INSTALL_FEE",      "APPLIANCE", "DELIVERED",    Intent.COMPLAINT,      "UNKNOWN",     "MANUAL", "INSTALL_DISPUTE_REVIEW"),
    ("签收异常-转人工",    "MISSING_ITEM",     "APPAREL", "PAID_SHIPPED",   Intent.REFUND_ONLY,    "UNKNOWN",     "MANUAL", "SIGN_ABNORMAL_REVIEW"),
    ("新鲜烘焙-换货",      "DATE_DISSATISFIED", "BAKERY", "DELIVERED",      Intent.RESHIPMENT,     "UNKNOWN",     "ALLOW",  "BAKERY_EXCHANGE_WITHIN_15D"),
    ("无适用政策-转人工",  "NO_LONGER_NEEDED", "APPAREL", "PAID_SHIPPED",   Intent.RETURN_REFUND,  "UNKNOWN",     "MANUAL", "NO_APPLICABLE_POLICY"),
]


async def main() -> None:
    settings = Settings(app_env="development", database_url=APP_DSN, rag_backend="pg")
    sf = make_sessionmaker(make_engine(settings))
    repo = DbRuleRepository(sf)
    n = await repo.refresh(TENANT)
    print(f"从 DB 载入规则 {n} 条（tenant={TENANT}）")
    engine = PolicyEngine(repo=repo)
    store = PgClauseStore(sf)

    ok = 0
    for name, reason, cat, status, intent, parcel, exp_d, exp_r in CASES:
        # 时间事实：签收 2026-10-01、申请 2026-10-05（第 5 天），保证时效类规则可判定
        facts = {"order": {"category": cat, "status": status,
                           "delivered_at": "2026-10-01T10:00:00+08:00"},
                 "logistics": {"parcel_status": parcel}, "finance": {},
                 "claim": {"reason_code": reason, "claim_at": "2026-10-05T10:00:00+08:00"},
                 "intent": str(intent)}
        d = engine.decide(tenant_id=TENANT, intent=intent, facts=facts, scope={"category": cat})
        clauses = await store.fetch_many(TENANT, list(d.required_clause_ids), d.as_of or "")
        found = {x.clause_id for x in clauses}
        missing = [c for c in d.required_clause_ids if c not in found]
        passed = (d.decision == exp_d and d.reason_code == exp_r and not missing)
        ok += 1 if passed else 0
        print(f"[{'PASS' if passed else 'FAIL'}] {name:14s} {d.decision:6s} {d.reason_code:28s} "
              f"clauses={len(clauses)}/{len(d.required_clause_ids)} "
              f"cap={d.amount_cap.cap_minor if d.amount_cap else None} rule={d.rule_ids}")
        if missing:
            print(f"         !! 缺失条款: {missing}")
    print(f"\n决策矩阵：{ok}/{len(CASES)} 通过")

    # ---------------- 留出集样本：用 DB 规则重放独立金标 ----------------
    holdout_path = pathlib.Path(__file__).resolve().parents[1] / "tests" / "eval" / "data" / "policy_holdout_v1.1.jsonl"
    holdout_ids = {"HV-03", "HV-19", "HV-29", "HV-36", "HV-39"}
    holdout_ok = 0
    holdout_total = 0
    if holdout_path.is_file():
        rows = [json.loads(x) for x in holdout_path.read_text(encoding="utf-8").splitlines() if x.strip()]
        picked = [r for r in rows if r["case_id"] in holdout_ids]
        print(f"\n留出集抽检（DB 规则）：{len(picked)} 条")
        for c in picked:
            exp = c["expect"]
            d = engine.decide(tenant_id=TENANT, intent=Intent(c["intent"]), facts=dict(c["facts"]),
                              as_of=c.get("as_of"))
            passed = (d.decision == exp["decision"] and d.reason_code == exp["reason_code"]
                      and list(d.required_clause_ids) == list(exp["required_clause_ids"]))
            holdout_ok += 1 if passed else 0
            holdout_total += 1
            print(f"[{'PASS' if passed else 'FAIL'}] {c['case_id']} "
                  f"{d.decision}/{d.reason_code} 期望 {exp['decision']}/{exp['reason_code']}")
    else:
        print("\n!! 未找到留出集文件，跳过 DB 重放")

    # ---------------- DB 规则 vs rules.json：字段级一致性 ----------------
    from app.policy.facts import parse_ts
    from app.policy.library import load_rules

    file_rules = {r.rule_id: r for r in load_rules()}
    db_rules = {r.rule_id: r for r in repo._cache.get(TENANT, [])}
    diffs: list[str] = []
    only_file = sorted(set(file_rules) - set(db_rules))
    only_db = sorted(set(db_rules) - set(file_rules))
    if only_file:
        diffs.append(f"仅存在于 rules.json：{only_file}")
    if only_db:
        diffs.append(f"仅存在于 DB：{only_db}")
    for rid in sorted(set(file_rules) & set(db_rules)):
        a, b = file_rules[rid].canonical(), db_rules[rid].canonical()
        for key in a:
            if key in ("effective_from", "effective_to"):
                pa, pb = parse_ts(a[key]), parse_ts(b[key])
                if (pa is None) != (pb is None) or (pa and pb and pa != pb):
                    diffs.append(f"{rid}.{key}: file={a[key]} db={b[key]}")
                continue
            if a[key] != b[key]:
                diffs.append(f"{rid}.{key}: file={a[key]!r} db={b[key]!r}")
    print(f"\n规则集一致性：file={len(file_rules)} db={len(db_rules)} 差异={len(diffs)}")
    for x in diffs[:20]:
        print("  !!", x)

    # ---------------- 条款映射覆盖率 ----------------
    async with tenant_transaction(sf, TENANT) as sess:
        rows = (await sess.execute(text(
            "SELECT status, count(*) FROM policy_rule_clause_map WHERE tenant_id=:t GROUP BY status"),
            {"t": TENANT})).all()
        chunk_n = (await sess.execute(text(
            "SELECT count(*) FROM policy_chunks WHERE tenant_id=:t"), {"t": TENANT})).scalar()
    map_counts = {r[0]: int(r[1]) for r in rows}
    total_map = sum(map_counts.values())
    resolved = map_counts.get("RESOLVED", 0)
    print(f"条款映射：{total_map} 条 {map_counts} | policy_chunks={chunk_n}")
    map_ok = total_map > 0 and resolved == total_map

    ok_all = (ok == len(CASES) and holdout_ok == holdout_total and not diffs and map_ok)
    print(f"\n汇总：矩阵 {ok}/{len(CASES)} | 留出抽检 {holdout_ok}/{holdout_total} | "
          f"规则差异 {len(diffs)} | 条款映射 {'OK' if map_ok else 'FAIL'}")
    if not ok_all:
        raise SystemExit(1)
    print("POLICY_RULES_OK")


if __name__ == "__main__":
    asyncio.run(main())