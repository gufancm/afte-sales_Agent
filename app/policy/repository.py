"""规则仓库（LLD §4.2）。内存实现用于测试与本地；生产用 DB 版（policy_rules/policy_documents）。"""
from __future__ import annotations

from app.policy.dsl import Rule
from app.policy.facts import ts_after, ts_before


class InMemoryRuleRepository:
    def __init__(self, rules: list[Rule] | None = None) -> None:
        self._rules = list(rules or [])

    def add(self, rule: Rule) -> None:
        self._rules.append(rule)

    def load_applicable(self, *, tenant_id: str, as_of: str, scope: dict) -> list[Rule]:
        out: list[Rule] = []
        for r in self._rules:
            if r.status == "RETIRED":
                continue
            # 有效期一律解析为带时区 datetime 比较（字符串比较会在不同时区偏移下出错）
            if r.effective_from and ts_before(as_of, r.effective_from):
                continue
            if r.effective_to and ts_after(as_of, r.effective_to):
                continue
            # 简单 scope 过滤：规则 scope 的键值必须与请求 scope 一致（若提供了该键）
            if any(k in scope and v is not None and scope.get(k) != v for k, v in r.scope.items()):
                continue
            out.append(r)
        return out