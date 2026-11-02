# ActionPlanner：确定性快路径 + 计划审查 + 有界重规划

> 节点：`action_planner`（第 5 个节点）。职责：把「意图 + 政策裁决 + 证据 + 可信事实」变成一份
> **只提案、不授权**的 `ActionPlan`。金额上限 / 证据要求 / 条款由 `PolicyDecision`、`EvidenceBundle`
> 决定，planner **不得修改**。

## 1. 一次规划里发生什么

```
action_planner(state, deps):
  1) 确定性快路径 try_fastplan()         # 命中 -> 跳过 LLM
  2) 否则 LLM 规划（带上一轮 replan 反馈）  # 未命中 / 需重规划
  3) 确定性计划审查 deterministic_critique()  # 始终执行
  4) 可选 LLM 复核 llm_critique()          # 默认关闭（省一次调用）
  5) 计划草稿落库 create_plan_draft()      # plan_id 前移（审批/风控/命令引用同一 id）
  6) 审计 PLAN_FASTPATH / PLAN_PROPOSED
-> route_after_plan: prepare | clarify | replan | manual
```

**只提案不授权**的边界：planner 输出 `Action` 与结构化参数；是否允许退款、金额上限、证据要求
全部来自上游确定性结论，planner 越界（动作不在意图白名单）会被 `validate_plan_against_intent` 拦下。

## 2. 动作注册表（单一元数据源）

`app/domain/action_registry.py`：每个动作一份 `ActionSpec`（`kind` / `risk_class` / `required_slots` /
`idempotency_template` / `preconditions`）。planner 选动作、critic 校验槽位、`command_submitter`
生成幂等键、风控与执行前复核都从这一份元数据取值，避免“动作语义散落在 prompt/风控/节点里各写一份”。

- 外部写动作集合 `EXTERNAL_WRITE_ACTIONS` 直接来自注册表（`external_write_actions()`）。
- 幂等键按模板生成：`refund:{ticket_id}:{order_id}` / `intercept:{ticket_id}:{waybill_no}` / `reship:{ticket_id}:{order_id}:{sku_id}` / `expedite:{ticket_id}:{waybill_no}`。

## 3. 确定性快路径（省一次 LLM，消除规划抖动）

`app/graph/planner_fastpath.py`。命中条件**全部**满足才直出，任一不满足即交 LLM：

- 意图命中映射（`INTENT_PRIMARY_ACTION`）：`REFUND_ONLY / RETURN_REFUND / INTERCEPT_REFUND / RESHIPMENT / EXPEDITE`；
- 政策裁决 `ALLOW` 且证据 `OK`；
- 必需槽位齐（按注册表 `required_slots`）；退款/补发还需**无歧义 SKU**（用户已指明，或订单仅一行）；
- 输出 `ActionPlan.source="fastpath"`，并审计 `PLAN_FASTPATH`。

复杂/多意图诉求（未命中映射、事实不全、SKU 有歧义）自然回落 LLM 规划。开关：`PLANNER_FASTPATH_ENABLED`（默认 true）。

## 4. 计划审查（Plan Critic）

`app/graph/plan_critic.py`。**确定性审查始终执行**，结论分四级：

| 级别 | 含义 | 处置 |
| --- | --- | --- |
| `FATAL` | 与政策/证据/事实矛盾，不可执行（如 `POLICY_NOT_ALLOW`、`EVIDENCE_NOT_OK`、`SKU_NOT_IN_ORDER`、`QUANTITY_EXCEEDS_ORDER`、`DUPLICATE_ACTION`、`ORDER_ID_MISMATCH`、非法依赖） | 转人工（`manual`） |
| `REPLAN` | **事实里已有该槽位、计划漏填**（如 `reason_code`）——planner 可自行补回 | 有界重规划 |
| `CLARIFY` | 事实里也没有该槽位（需用户补充，如多行订单未指明 SKU） | 澄清 |
| `WARN` | 仅记录（如 `requested_amount_minor` 超可退余额，交由风控/报价判定） | 继续 |

**可选 LLM 复核**（`LLM_CRITIC`）：默认关闭（`PLANNER_LLM_CRITIC_ENABLED=false`，避免每次规划 +1 次调用）。
开启后仅在确定性审查通过、且 LLM 给出 `HIGH` 严重且 `ok=false` 时才 `FATAL` 转人工；复核失败不影响主流程。
提示词：`app/llm/prompts/plan_critic.txt`（`plan-critic-v1`）。

## 5. 有界重规划（REPLAN 闭环）

`REPLAN` 时把 `critique.feedback()` 写入 `replan_feedback` 并回到 `action_planner`：

- 仅在「本轮不是重规划轮」且 `replanning_count < PLANNER_MAX_REPLANS`（默认 1）时发出新一轮反馈并计数；
- 下一轮把反馈拼进 LLM 输入（`上一轮计划被审查驳回，请据此修正：...`），**并强制跳过快路径**；
- 达到上限后不再回 planner（防死循环）。审计 `PLAN_PROPOSED.replan / replanning_count`。

`route_after_plan` 出口：`replan`（需重规划且反馈就绪）→ `action_planner`；`prepare` / `clarify` / `manual`。

## 6. 依赖 DAG

`ActionProposal.depends_on: list[int]` 标注动作先后依赖（如先拦截成功再退款）。schema 强约束
`0 <= dep < 自身下标`（既保证**无环**，又给出确定顺序）。`command_submitter` 用
`_topo_external_writes()` 做拓扑序展开后再提交，保证被依赖动作先入队。

## 7. plan_id 前移（同一 id 贯穿全链路）

1. planner 规划后即 `create_plan_draft()` 落 `action_plans`（`status=DRAFT`），返回 `plan_id` 写入 State；
2. `action_preparation` 报价后 `freeze_plan(..., plan_id=state["plan_id"])` **原地 UPDATE**（保持同一 id，`status=VALIDATED`）；
3. 风控 / HITL / 执行前复核 / Saga 命令全部绑定该 `plan_id`（`approval:{approval_id}` 或 `plan:{plan_id}`）。

## 7.5 报价预占（hold）与 requote 上限

**报价即预占（quote + hold 原子）**：Finance 只读报价同时冻结一笔**可用余额预占**，返回
`reservation_reference / reservation_expires_at / available_balance_minor`（可用额度 = 可退余额 − 已预占）。
- `action_preparation` 把凭证写入 State 与 `refund_quotes.reservation_reference`（迁移 `0017_refund_reservation.sql`），
  落审计 `REFUND_RESERVED`，并随 `plan_hash` 一起冻结（`PLAN_FROZEN` 携带 `reservation_ref`）。
- **额度不足则不预占**（`reservation_reference=None`）→ 节点 **fail-closed** 返回 `BALANCE_INSUFFICIENT`
  （审计 `REFUND_RESERVATION_FAILED`），绝不「先报价后放行」。
- 执行前复核校验 hold：`RESERVATION_MISSING`/`RESERVATION_MISMATCH` → 转人工；`RESERVATION_EXPIRED` → 重报价。
- 执行消费 hold：`execute_refund` 校验 `quote_hash` 一致后实扣可退余额并释放 reserved（幂等键去重）。

**requote 有上限**：报价/预占过期 → `reprepare` 重新报价；`requote_count` 达到 `max_requote_cycles`
后返回 `REQUOTE_EXCEEDED` → 转人工（防死循环）。

> 与业界对齐：这一步就是支付里的 `authorize/hold → capture` 两阶段；真实 Finance 侧需以
> `reservation_reference` 做并发互斥，避免同一订单被多笔并发退款超额占用。

**实测证据**（ticket `a785f245-b21c-443b-a509-59f3b9419133`，`o_1004`，¥88）：
- `refund_quotes`：`amount_minor=1000`、`reservation_reference` 非空、`reservation_expires_at` 有值。
- 审计链：`REFUND_RESERVED{res=145dbbf0…}` → `PLAN_FROZEN{同 res}` → `RISK_DECIDED` → `COMMANDS_SUBMITTED` → `RESPONDED`。
- 命令 `CONFIRMED`；mock 余额 `refundable 8800 → 7800`、`reserved 0`（hold 已消费释放）。

## 8. 配置开关

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PLANNER_FASTPATH_ENABLED` | `true` | 确定性快路径总开关 |
| `PLANNER_LLM_CRITIC_ENABLED` | `false` | 是否加一次 LLM 计划复核 |
| `PLANNER_MAX_REPLANS` | `1` | 有界重规划最大轮数 |
| `MAX_REQUOTE_CYCLES` | `1` | 报价/预占过期后重报价的最大轮数（超出转人工） |

## 9. 实测证据（2026-10-25）

工单 `cf066e40-cb05-4fd0-9cd6-38128b2404a9`（`订单 o_1002 收到的衣服有破洞…退货退款…`，意图 `RETURN_REFUND`）：

- `agent_audit_events`：`PLAN_FASTPATH` → `PLAN_PROPOSED{source=fastpath, actions=[REQUEST_RETURN_REFUND], replan=false}` → `PLAN_FROZEN`；后续 `RISK_DECIDED / APPROVAL_REQUIRED / COMMANDS_SUBMITTED / RESPONDED`。
- `action_plans`：仅 1 行，`plan_id=e079e2c0-9f46-4f9f-800c-0d586d835cfe`，`status=VALIDATED`，`quote_id` 非空 —— 证明 DRAFT→原地冻结为**同一 plan_id**。
- `llm_calls`：仅 `intent_guardrail` 与 `policy_retrieval`，**无 `action_planner` 行** —— 证明快路径确实跳过了 LLM。
- 证据链未受影响：`EVIDENCE_BUILT.status=OK`（`lexical=pg-bm25, reranker=http, dense_enabled=true, variant_count=5`）。

回归测试：`tests/test_action_planner.py`（14 例）覆盖快路径/开关/审查分级/有界重规划/依赖 DAG/幂等键/plan_id 复用；
全量 `pytest -q` = **115 passed**。

## 10. 与业界做法的差异与取舍

- **确定性优先**：意图明确、单动作、政策/证据确定的场景直出，复杂多意图才交给 LLM——比“强制所有请求都过 LLM”更稳、更省。
- **确定性闸门 + 可选 LLM 复核**：把“可解释、可回归”的硬校验放在确定性 critic，LLM 只做**降级闸门**且默认关闭。
- **只提案不授权**：planner 与 Policy/Risk/Guard 职责分离，避免 LLM 直接触碰资金授权。
- **评测暂缓**：规划质量金标评测集本轮明确不建（项目收尾统一测评）。