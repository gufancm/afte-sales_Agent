# 收口节点（7–12）加固落地（v3.6）

本文对应上一轮评审中针对 **RiskAuthorization / HITLReview / PreExecutionGuard / CommandSubmitter /
ResultVerifier / ResponseComposer** 六节点提出的全部优化项，逐条给出**已落地实现**与可核对证据。
设计基线见 `docs/ecommerce-aftersale-agent-lld-v3.5.md`；12 节点顺序不变：

```
intent_guardrail → context_aggregator → policy_engine → policy_evidence → action_planner
→ action_preparation → risk_authorization → [hitl_review] → pre_execution_guard
→ command_submitter → result_verifier → response_composer
```

图外：Saga Dispatcher（`scripts/run_worker.py`）、对账 Worker。**图节点只持有只读端口**，可写端口仅在
`deps.tools`（Worker/SagaDispatcher 持有），`tests/test_context_gateway.py::test_graph_nodes_do_not_reference_write_tools`
守卫该边界。

---

## 1. RiskAuthorization：逐动作 + 多维生效

- **逐动作取规则、取最严结论**：计划内每个外部写动作各评估一次，任一 DENY 即整单 DENY；任一 REVIEW
  即整单进 HITL（`app/risk_rules.py::evaluate_risk`、`nodes.risk_authorization`）。
- **缺少适用规则 → MANUAL，绝不默认放行**：`NO_RISK_RULE_FOR_ACTION` / `NO_ACTION_TO_AUTHORIZE`。
- **退款生效维度**（`evaluate_risk(action, state, params)`，`params` 由节点从配置构造）：
  1. 金额必须已由 Finance 报价（`REFUND_QUOTE_MISSING`）；
  2. 可用余额校验（`BALANCE_INSUFFICIENT`）；
  3. 政策金额上限缺失 → REVIEW（`NO_CAP_DEFINED`），超上限 → DENY（`OVER_POLICY_CAP`）；
  4. **买家退款历史**（`state["risk_facts"]`，来自 OMS `get_buyer_refund_stats`）：缺失或降级 →
     `MANUAL(REFUND_STATS_UNAVAILABLE)`；命中黑名单 → `DENY(BUYER_BLACKLISTED)`；近 30 天频次/累计金额
     越阈 → `REVIEW(REFUND_FREQUENCY_HIGH)`。
- **非退款动作各自独立规则**：拦截（运单/在途/已签收）、催派（运单/未签收）、补发（SKU/数量/库存与地址
  缺权威事实 → REVIEW）。
- 阈值配置：`RISK_MAX_REFUND_COUNT_30D`、`RISK_MAX_REFUND_AMOUNT_30D_MINOR`、`RISK_BLACKLIST_DENY`。

> 说明：单元测试可直接 `evaluate_risk(action, state)`（`params=None`，只跑核心规则）；生产路径由节点始终
> 传入 `params`，启用买家历史维度并 fail-closed。

---

## 2. HITLReview：审批治理（角色门 / 计划一致 / SLA）

- **审批人角色门**：`APPROVAL_ALLOWED_ROLES`（逗号分隔，留空=不启用）非空时，`decide_approval` 校验
  `reviewer_role`，不匹配抛 `PermissionError("REVIEWER_ROLE_NOT_ALLOWED")`；API 映射为 **HTTP 403**。
  落库记录 `reviewer_id / reviewer_role / decision_comment`。
- **恢复时计划一致性**：`hitl_review` 恢复分支校验 DB 审批任务的 `plan_hash == state.plan_hash`，不符 →
  审计 `APPROVAL_PLAN_MISMATCH` 且 `approval_status=REJECTED`（不信任 resume payload）。
- **恢复时过期拦截**：审批已过期 → 审计 `APPROVAL_EXPIRED_ON_RESUME`、`approval_status=EXPIRED`。
- **SLA 升级**：Worker 周期调用 `escalate_expired_approvals()`，把 `PENDING` 且过期任务置 `EXPIRED`
  并写审计 `APPROVAL_SLA_EXPIRED`。
- **幂等**：`ensure_approval_task` 按 `(tenant, plan_hash)` 只新建一次；`interrupt()` 重放不重复写
  `APPROVAL_REQUIRED`（`tests/test_worker_and_hitl.py`）。

---

## 3. PreExecutionGuard：执行前复核（全动作 + 双重 CAS）

- **覆盖全部写动作**（不再只看第一个）：`_guard_common` + 逐动作 `_guard_action`
  （退款/拦截/催派/补发），无法识别动作 → `UNSUPPORTED_ACTION`（转人工）。
- **双重 CAS 防漂移**：
  - 报价时 `quote_order_version` 与当前 `order_snapshot.version` 不一致 → `STATE_DRIFT`；
  - **新增** `plan_frozen_facts_version`（action_preparation 冻结时写入）与当前 `facts_version` 不一致 →
    `STATE_DRIFT`。路由：`STATE_DRIFT → rerisk`。
- 退款：报价/预占未过期、预占金额一致、余额充足、需要审批时审批已通过。
- 拦截：运单归属、在途、未签收；催派：未签收；补发：SKU 在单、数量不超。

---

## 4. CommandSubmitter：命令 + Outbox（优先级 / relay / 取消）

- **优先级**（`app/domain/action_registry.py` 单一元数据源，`priority` 越小越先派发）：资金类 `10` <
  拦截/补发 `20` < 催派 `30`；`enqueue_command` 落库，`claim_next_command` 按 `priority ASC, created_at ASC`。
- **Outbox relay**：命令与 `outbox_events` 同事务写入（`enqueue_command`）；Worker 派发后调用
  `mark_outbox_published(aggregate_id=command_id)` 置 `PUBLISHED`（不再只写不发）。
- **取消入口**：`cancel_command(command_id, tenant_id, reason)` 仅允许 `PREPARED → CANCELLED`；已派发命令
  一律走对账，不撤销。
- **纵深防御**：仅提交“本轮已被风控逐动作评估过”的写动作，否则一条都不写（`COMMAND_SUBMIT_BLOCKED`）。

---

## 5. ResultVerifier：按 command_id 核验 + 权威回查

- **按 command_id 核验**：`list_command_statuses` 汇总；缺失/未终态 → 幂等建对账任务（command_id 级）。
- **业务权威回查**（`_verify_business_authority`）：
  - 退款 → Finance `get_refund_status`（SUCCEEDED/SETTLED=完成，FAILED_CONFIRMED=失败，否则等待）；
  - 拦截 → TMS `get_interception_status`；
  - **补发（新增）** → OMS `get_reshipment_status`（SHIPPED/DELIVERED=完成，FAILED/CANCELLED=失败）；
  - **催派（新增）** → TMS `get_dispatch_status`（ACCEPTED/EXPEDITED/IN_PROGRESS=完成，FAILED/REJECTED=失败）。
- **UNKNOWN / 未结算** → 幂等对账任务；Worker 回查（`command_id` 优先，其次 `refund_request_id`）。
- **人工队列 + 告警**：对账达上限 → `MANUAL_RECONCILE`，写审计 `RECONCILE_NEEDS_MANUAL` 并投递
  `inbox(source="manual_reconcile")`（去重，`severity=HIGH`）。

---

## 6. ResponseComposer：唯一出口（事实注入 / i18n / 多渠道）

- 统一按 `response_type` 分发：`NEED_INFO / DENIED / MANUAL / ACCEPTED / PROCESSING / SUCCEEDED / FAILED / UNKNOWN`。
- **事实注入**：退款金额（`CNY xx.xx`，来自报价，不臆造）+ **到账时效**（`settlement_eta`，仅
  ACCEPTED/PROCESSING）。措辞仍来自版本化政策模板，失败回退通用兜底。
- **多语言 i18n**：`state["locale"]`（默认 `zh-CN`；`en-US` 提供兜底文案）。
- **多渠道分发审计**：`state["channels"]`（默认 `["app"]`）逐渠道写 `MESSAGE_DISPATCHED`。
- 关键资金事件（退款受理/预占/审批）在各自节点即时落审计，不等最终回复节点。

---

## 7. Worker 兜底与运维（`scripts/run_worker.py`）

- 每 ~5s 周期 `_sweep`：
  - `reclaim_expired_commands(max_attempts=WORKER_COMMAND_MAX_ATTEMPTS)`：租约过期 `CLAIMED` → `UNKNOWN`
    （`last_error=LEASE_EXPIRED`）并按 `command_id`+`refund_request_id` 建对账任务，写审计 `COMMAND_LEASE_EXPIRED`；
  - `escalate_expired_approvals()`：审批 SLA 过期 → `EXPIRED`。
- 对账 `_reconcile_once`：优先 `command_id` 回查命令终态，其次 `refund_request_id` 回查 Finance；达上限 →
  `MANUAL_RECONCILE` 入人工队列。
- 原则：**绝不盲重试外部写**；不可确认一律转 UNKNOWN 交由对账收敛（§9.8/§18.10）。

---

## 8. 配置项汇总（`.env`）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `APPROVAL_ALLOWED_ROLES` | 空 | 审批人角色白名单（逗号分隔）；空=不启用 |
| `WORKER_COMMAND_MAX_ATTEMPTS` | 3 | 命令租约回收/对账升级上限 |
| `RISK_MAX_REFUND_COUNT_30D` | 5 | 买家 30 天退款频次阈值（≥ → REVIEW） |
| `RISK_MAX_REFUND_AMOUNT_30D_MINOR` | 500000 | 近 30 天累计退款金额阈值（越阈 → REVIEW） |
| `RISK_BLACKLIST_DENY` | true | 黑名单买家是否直接 DENY（false → REVIEW） |
| `MAX_REQUOTE_CYCLES` | 1 | 报价/预占过期重新报价上限 |

---

## 9. 数据库迁移

- `0018_governance.sql`：`approval_tasks` 增 `reviewer_role`、`decision_comment`；`reconciliation_tasks` 增
  `command_id` + 索引 `idx_recon_command`。
- `0019_saga_priority.sql`：`saga_commands` 增 `priority` + 索引 `idx_commands_claim_priority`。
- 命令租约复用 `saga_commands.lease_until`（0004 已有）。

---

## 10. 验证与证据

- 回归测试：`tests/test_governance_and_risk.py`（9 例）覆盖审批角色门、事实漂移 CAS、生效风控维度、
  命令优先级/取消、补发权威核验、回复 i18n/多渠道/ETA。全量 `pytest -q` = **133 passed**。
- 端到端（Docker，真实 Postgres + LLM）：退货退款工单触发 HITL → 审批恢复 → `WAITING_EXTERNAL`。
- 可核对 SQL：

```sql
SELECT action, status, priority, attempt_count FROM saga_commands ORDER BY created_at DESC LIMIT 5;
SELECT event_type, status FROM outbox_events ORDER BY created_at DESC LIMIT 5;
SELECT status, reviewer_id, reviewer_role FROM approval_tasks ORDER BY created_at DESC LIMIT 3;
```

实测：新退款命令 `priority=10`；对应对账/派发后 outbox 事件为 `PUBLISHED`；`approval_tasks.reviewer_role` 列生效。
