# -*- coding: utf-8 -*-
"""CI 断链检查：每条规则（rules + catalog）引用的 clause_id 必须真实存在。

来源二选一：
  --source corpus（默认）  校验 policy/clauses_jd.json（离线，不连库）
  --source db              校验 DB 的 policy_chunks（入库后闭环验证）

命中判定：clause_id 或 logical_clause_id 任一匹配即视为存在。

用法：
  python scripts/check_rule_clause_links.py
  python scripts/check_rule_clause_links.py --source db \
      --database-url postgresql+asyncpg://aftersale_app:app_pw_change_me@localhost:5432/aftersale
退出码：0 = 全部命中；1 = 存在断链。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RULES = ROOT / "policy" / "rules.json"
CORPUS = ROOT / "policy" / "clauses_jd.json"
TENANT = "tenant_demo"


def load_rule_links() -> list[tuple[str, list[str]]]:
    data = json.loads(RULES.read_text(encoding="utf-8"))
    out = [(r["rule_id"], list(r.get("required_clause_ids") or [])) for r in data.get("rules", [])]
    out += [(c.get("item_id", "?"), list(c.get("required_clause_ids") or [])) for c in data.get("catalog", [])]
    return out


def corpus_ids() -> set[str]:
    data = json.loads(CORPUS.read_text(encoding="utf-8"))
    ids = {c["clause_id"] for c in data["clauses"]}
    ids |= {c["logical_clause_id"] for c in data["clauses"] if c.get("logical_clause_id")}
    return ids


async def db_ids(dsn: str, tenant: str) -> set[str]:
    import asyncpg
    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://", 1))
    try:
        rows = await conn.fetch(
            "SELECT clause_id, logical_clause_id FROM policy_chunks WHERE tenant_id=$1", tenant)
    finally:
        await conn.close()
    ids = {r["clause_id"] for r in rows}
    ids |= {r["logical_clause_id"] for r in rows if r["logical_clause_id"]}
    return ids


def main() -> int:
    ap = argparse.ArgumentParser(description="规则→条款 断链检查")
    ap.add_argument("--source", choices=["corpus", "db"], default="corpus")
    ap.add_argument("--database-url", default=None)
    ap.add_argument("--tenant", default=TENANT)
    a = ap.parse_args()

    links = load_rule_links()
    if a.source == "db":
        from app.config import get_settings
        dsn = a.database_url or get_settings().effective_worker_database_url
        known = asyncio.run(db_ids(dsn, a.tenant))
        where = f"policy_chunks(tenant={a.tenant})"
    else:
        known = corpus_ids()
        where = CORPUS.name

    total = sum(len(c) for _, c in links)
    broken: list[tuple[str, str]] = []
    for owner, clause_ids in links:
        for cid in clause_ids:
            if cid not in known:
                broken.append((owner, cid))

    print(f"来源：{where}；规则/条目 {len(links)} 个，引用条款 {total} 处，已知条款 {len(known)} 个")
    if broken:
        print(f"[FAIL] 断链 {len(broken)} 处：")
        for owner, cid in sorted(broken):
            print(f"   - {owner} -> {cid}")
        return 1
    print("[OK] 全部引用条款均存在（rule/catalog -> clause 闭包完整）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())