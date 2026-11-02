# 政策裁决 trace（PolicyEngine 可解释性）

## 1. 为什么要 trace

`policy_engine` 是确定性裁决节点，输出 `ALLOW / DENY / MANUAL / NEED_INFO` 与 `amount_cap` 等资金相关结论。
一旦要回答「为什么这笔能退 / 为什么拒绝 / 为什么转人工 / 为什么命中这条规则而不是那条」，只有 `rule_ids`
是不够的——需要**条件级的判决留痕**。trace 同时服务三件事：

- **审计与申诉**：客户/监管问「依据是什么」，可逐条给出命中条件的实际取值。
- **规则调优**：一眼看出「本该命中的规则差在哪个字段」，是缺事实还是写错了值。
- **回归与治理**：为后续「规则发布管理（沙盒/影子/灰度）」提供可对比的判决快照。

## 2. trace 结构（`PolicyDecision.trace`，engine `policy-engine/2.0`）

| 字段 | 含义 |
| --- | --- |
| `engine` / `ruleset_schema` | 引擎与规则集结构版本 |
| `ruleset_version` / `ruleset_hash` | 规则集版本；**由规范化规则内容计算的指纹**（改条件/金额/条款绑定都会变） |
| `facts_snapshot_id` | 本次裁决使用的规范化事实快照指纹；与 `ruleset_hash` 一起构成“可复现三件套” |
| `as_of` / `evaluated_at` / `time_basis` | 裁决基准时点；`time_basis=claim_at` 表示时效按用户申请时刻判断 |
| `scope` / `intent` | 作用域与意图 |
| `stage_order` | 阶段定义（0 事实守卫 / 10 强制限制 / 20 明确拒绝 / 30 一般允许 / 40 兜底人工） |
| `loaded_rule_count` / `loaded_rule_ids` | 本次载入的适用规则（已按租户/生效期/scope 过滤） |
| `evaluated` | 每条适用规则的判定：`rule_id/version/priority/stage/specificity/decision/reason_code/matched/blocked` |
| `evaluated[].conditions` | **条件级留痕**：`fact / op / expected / actual / ok / missing` |
| `evaluated[].failed_conditions` | 未满足的条件字段（定位「差在哪」） |
| `evaluated[].missing_conditions` | 因事实缺失而无法判定的条件字段 |
| `matched_rule_ids` | 完全命中的规则集合 |
| `blocked_rules` | 「除缺事实外其余条件都成立」的规则 + 缺失的**原始输入字段** |
| `candidates_ranked` | 命中项与 blocked 项统一排序后的前 8 名（stage/priority/specificity/decision） |
| `top_priority` | 最高阶段内的最高优先级 |
| `conflict` | 同阶段同优先级同具体度且结论不同时的冲突信息（否则 `null`），或 `deny_overrides_allow` 兜底触发记录 |
| `missing_facts` | 需要用户/系统补齐的**原始**事实路径（如 `claim.claim_at`，不是派生字段） |
| `winner` | 最终胜出规则（`rule_id/version/stage/priority/specificity/decision/reason_code`）；blocked 胜出时带 `blocked_by_missing_fact=true` |
| `amount_check` | 金额判定：`WITHIN / EXCEEDED / UNKNOWN` + `amount_minor / amount_source / cap_minor / cap_type` |
| `outcome` | 最终 `reason_code` |

`trace.evaluated` 覆盖**全部适用规则**，不只是命中项——这正是「解释为什么被拒」所需。

## 3. 落库与查看

- 落库列：`policy_decisions.trace JSONB`（迁移 `0015_policy_decision_trace.sql`，默认 `{}`）。
- 审计：`POLICY_DECIDED` 事件带 `trace` 摘要 + `ruleset_version / ruleset_hash / facts_snapshot_id /
  time_basis / requires_manual_approval / execution_blocked`。
- 查看脚本：

```bash
docker compose run --rm api python scripts/show_policy_trace.py <ticket_id>
# 不传 ticket_id 时打印最近一条裁决；宿主机可用 POLICY_APP_DSN 指定连接
```

> 直连 `policy_decisions` 时需注入租户上下文（强制 RLS）：脚本内部已 `set_config('app.tenant_id', ...)`，
> 可用环境变量 `POLICY_TENANT` 覆盖（默认 `tenant_demo`）。

## 4. 实测证据（容器实测，2026-10-28）

节点级冒烟（零 LLM / 零 DB，`app/graph/nodes.py::policy_engine`，签收 2026-10-01、申请 2026-10-05）：

```
decision = ALLOW | reason = NO_REASON_WITHIN_7D
time_basis = claim_at
ruleset_hash = 0662d93b206c197d… | facts_snapshot_id = ee4c992fc2c963d8…
winner = {rule_id: R-JD-NR-WINDOW-OK-001, stage: 30, priority: 101, specificity: 3}
audit keys = [as_of, decision, execution_blocked, facts_snapshot_id, policy_decision_id, policy_version,
              reason_code, requires_manual_approval, rule_ids, ruleset_hash, ruleset_version, time_basis, trace]

同一状态把 claim_at 改成 2026-10-10（第 10 天）：
decision = DENY | reason = NO_REASON_WINDOW_EXPIRED
```

说明：同一份事实、同一条规则集，只改申请时点，结论从允许变为拒绝，且两次裁决的 `ruleset_hash` 相同、
`facts_snapshot_id` 不同——可以精确回答“为什么同一订单在不同日期裁决不同”。

## 5. 与旧版的差异（v1 → v2）

| 旧 | 新 |
| --- | --- |
| 只按 `priority` 决出胜者 | 先按 `stage`，再 `priority`、`specificity`；DENY 永远先于 ALLOW |
| 缺事实直接落到“通用允许” | blocked 规则参与竞争，赢了就 `NEED_INFO` / `MANUAL` |
| `ruleset_hash = hash(rule_id@version)` | `hash(规范化规则内容)`，可复现 |
| 金额超限统一 `MANUAL` | 区分 `POLICY_LIMIT`（拒绝）与 `AUTO_EXEC_THRESHOLD`（转人工） |
| `days_since_delivery` 单一整数 | `day_index_signed` / `day_index_next` 双口径 + 原始 `claim_at` |