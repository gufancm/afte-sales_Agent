"""DB 版规则仓库：启动/按需把 policy_rules 载入内存缓存，引擎保持纯同步（§4.2）。"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.engine import tenant_transaction
from app.policy.dsl import Condition, Rule
from app.policy.facts import ts_after, ts_before


class DbRuleRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.sf = session_factory
        self._cache: dict[str, list[Rule]] = {}

    async def refresh(self, tenant_id: str) -> int:
        async with tenant_transaction(self.sf, tenant_id) as s:
            rows = (await s.execute(text(
                "SELECT rule_id,version,priority,when_json,decision,reason_code,required_clause_ids,"
                "amount_cap_minor,COALESCE(cap_type,'AUTO_EXEC_THRESHOLD') AS cap_type,stage,"
                "scope,status,effective_from,effective_to "
                "FROM policy_rules WHERE tenant_id=:ten"), {"ten": tenant_id})).mappings().all()
        rules: list[Rule] = []
        for r in rows:
            conds = [Condition(c["fact"], c["op"], c.get("value")) for c in (r["when_json"] or [])]
            rules.append(Rule(rule_id=r["rule_id"], version=r["version"], priority=r["priority"], when=conds,
                              decision=r["decision"], reason_code=r["reason_code"],
                              required_clause_ids=list(r["required_clause_ids"] or []),
                              amount_cap_minor=r["amount_cap_minor"], cap_type=r["cap_type"],
                              stage=r["stage"], scope=dict(r["scope"] or {}),
                              status=r["status"],
                              effective_from=(r["effective_from"].isoformat() if r["effective_from"] else None),
                              effective_to=(r["effective_to"].isoformat() if r["effective_to"] else None)))
        self._cache[tenant_id] = rules
        return len(rules)

    def load_applicable(self, *, tenant_id: str, as_of: str, scope: dict) -> list[Rule]:
        rules = self._cache.get(tenant_id, [])
        out: list[Rule] = []
        for r in rules:
            if r.status == "RETIRED":
                continue
            if r.effective_from and ts_before(as_of, r.effective_from):
                continue
            if r.effective_to and ts_after(as_of, r.effective_to):
                continue
            if any(k in scope and v is not None and scope.get(k) != v for k, v in r.scope.items()):
                continue
            out.append(r)
        return out