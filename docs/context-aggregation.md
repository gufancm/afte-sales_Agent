# ContextAggregator 与工具网关（Tool Gateway）

本文件说明第 2 个 LangGraph 节点 `context_aggregator` 的职责、权限模型与工程化完善，
以及配套的 MCP 工具网关（`app/mcp/gateway.py`）。目标：把「节点直接 httpx 调 MCP」升级为
「只读门面 + 统一网关」，达到大厂内部工具调用层的基线标准。

## 1. 节点职责

`context_aggregator`（`app/graph/nodes.py`）是**事实采集 + 归属校验**节点：

- 输入：上游意图识别产出的槽位（`slots.order_id`、`slots.waybill_no`）与认证上下文（tenant/user）。
- 动作：并发只读拉取三份事实（OMS 订单 / TMS 物流 / Finance 可退余额），校验订单归属，产出
  `order_snapshot` / `logistics_snapshot` / `finance_snapshot` / `facts_version` / `context_meta`。
- 不做：不裁决（PolicyEngine 负责）、不调 LLM、不发写操作（SagaDispatcher 负责）。

## 2. 权限模型（读写隔离）

| 层 | 对象 | 谁持有 | 能力 |
| --- | --- | --- | --- |
| 原始端口 | `WriteTools`（`Deps.tools`） | SagaDispatcher / Worker / 联调脚本 | 读 + 写 |
| 只读门面 | `ReadOnlyOrderTools` / `ReadOnlyLogisticsTools` / `ReadOnlyFinanceTools`（`Deps.orders/logistics/finance`） | 图节点 | 仅只读 |
| 协议 | `ReadOnly*Port` / `*Port`（`app/mcp/ports.py`） | 类型约束 | 只读 / 读写分层 |

- 图节点只能拿到只读门面；门面上**不存在** `execute_refund` / `intercept_parcel` / `create_reshipment` 等写方法。
- 写能力只存在于 `Deps.tools`，且被约定为「图节点不得引用」；测试 `test_graph_nodes_do_not_reference_write_tools` 静态守护。
- 网关强制 `tenant_id` 非空；缺失即 `ToolAuthorizationError`（fail-closed）。

## 3. 统一工具网关能力

`app/mcp/gateway.py` 对每次只读调用集中提供：

- **超时**：`MCP_TOOL_TIMEOUT_MS`（默认 10000），每次尝试独立计时。
- **重试 + 指数退避**：仅对瞬时错误（超时/连接/5xx）重试 `MCP_TOOL_MAX_RETRIES` 次；
  4xx、授权错误、Schema 校验错误**绝不重试**。
- **熔断**：连续失败 `MCP_TOOL_CIRCUIT_FAILURE_THRESHOLD`（默认 5）后打开，冷却
  `MCP_TOOL_CIRCUIT_COOLDOWN_MS`（默认 30000）内快速失败，冷却后半开放行一次试探。
- **限流**：按工具令牌桶（`MCP_TOOL_RATE_LIMIT_PER_S`）。
- **并发上限**：全局信号量（`MCP_TOOL_MAX_CONCURRENCY`）。
- **Schema 校验**：上游返回先经 Pydantic 校验，缺字段/类型错 -> `ToolSchemaError`，不把半残数据交给下游。
- **调用审计**：每次调用写 `tool_calls`（tenant/ticket/run/trace/node/tool/status/attempts/latency/critical）。

异常体系：`ToolTimeout` / `ToolUnavailable` / `ToolCircuitOpen` / `ToolRateLimited` /
`ToolSchemaError` / `ToolAuthorizationError` / `ToolNotFound` / `ToolUpstreamError`。

## 4. 关键/次要事实分级降级

`context_aggregator` 按意图决定事实的关键性，避免「三个读同生共死」：

- 订单事实：**始终关键**，读失败 -> fail-closed 转人工，绝不带残缺事实继续。
- 资金事实：退款族意图（`REFUND_ONLY` / `RETURN_REFUND` / `INTERCEPT_REFUND`）下关键；否则读失败降级，
  标记 `finance_snapshot.degraded=true`、`refundable_balance_minor=null`。
- 物流事实：`INTERCEPT_REFUND` / `EXPEDITE` 下关键；否则读失败降级，标记 `parcel_status=UNKNOWN`、`degraded=true`。

降级信息写入 `context_meta.degraded` 与 `CONTEXT_AGGREGATED` 审计；下游 PolicyEngine 缺事实时按
`NEED_INFO` / `MANUAL` 处置，仍然安全。

## 5. 事实版本

`facts_version = "<order.version>:<logistics.version>:<finance.version>"`（缺失版本记 `unknown`）。
读-写之间用该版本做乐观并发/漂移检测；本版已把 Finance 版本纳入（此前遗漏）。

## 6. 租户与归属边界（被调方强制）

除调用方校验外，Mock MCP（`app/mcp/mocks/server.py`）作为「真实系统应在网关侧强制」的替身：

- 每个调用必须携带合法 `tenant_id`；未知/缺失 -> `403 TENANT_FORBIDDEN`。
- 订单不存在 -> `404 ORDER_NOT_FOUND`（不再是 500）。
- 传入 `authenticated_user_id` 时做归属校验，非本人订单 -> `403 NOT_OWNER`。

> 真实系统必须在被调方（网关/DB RLS）强制这些边界，不能只依赖调用方自觉。

## 7. 审计与可观测

- `tool_calls`：只读工具调用（谁、哪个工单/节点、耗时、重试次数、状态、是否关键）。
- `agent_audit_events.event_type='CONTEXT_AGGREGATED'`：一次采集的事实版本与降级情况。
- 网关内存指标：`ToolGateway.calls`（进程内，便于本地观测）。

## 8. 验证证据（容器实测）

- 单元/回归：容器内 `pytest -q` -> `93 passed`（新增 `tests/test_context_gateway.py` 13 项）。
- 迁移：`0014_tool_calls.sql` 已应用，`tool_calls` 表存在且启用 RLS。
- 端到端（真实 API + 真实 LLM）：退款工单 `RETURN_REFUND`，`tool_calls` 落 6 行：
  `oms.get_order_detail` / `tms.get_tracking_info` / `finance.get_refundable_balance`（node=context_aggregator）、
  `finance.calculate_refund_amount`（action_preparation）、`finance.get_refundable_balance`（pre_execution_guard）、
  `finance.get_reconciliation_result`。
- `CONTEXT_AGGREGATED` 审计：`{"facts_version":"v1:v0:b0","degraded":{},"finance_critical":true,"logistics_critical":false}`。
- Mock 租户强制实测：未知/缺失租户 -> 403，订单不存在 -> 404，合法租户 -> 200。

## 9. 相关配置（`.env`）

```
MCP_TOOL_TIMEOUT_MS=10000
MCP_TOOL_MAX_RETRIES=2
MCP_TOOL_BACKOFF_BASE_MS=150
MCP_TOOL_BACKOFF_MAX_MS=1000
MCP_TOOL_CIRCUIT_FAILURE_THRESHOLD=5
MCP_TOOL_CIRCUIT_COOLDOWN_MS=30000
MCP_TOOL_RATE_LIMIT_PER_S=50
MCP_TOOL_MAX_CONCURRENCY=8
MCP_MOCK_TENANTS=tenant_demo
MCP_MOCK_REQUIRE_TENANT=true
READONLY_LLM_REPHRASE_ENABLED=true   # 只读自助答复是否用 LLM 润色（false=纯模板）
```

## 10. 只读自助答复（混合式：事实模板 + LLM 润色）

`STATUS_QUERY` / `COMPLAINT` 命中 `readonly` 后进入本节点的**只读自助分支**，用真实事实即时答复（无写命令）。
检索/答复均不经过 Planner/风控链，事实仍全部来自本节点的只读工具网关调用。

**混合式设计（行业常见做法：事实用模板，措辞用 LLM）**

| 环节 | 实现 | 目的 |
| --- | --- | --- |
| 事实模板 | `app/graph/readonly_answer.py::render_fact_template()` | 唯一事实来源；状态中文化；可审计、可复现 |
| 措辞润色 | `app/llm/prompts/readonly_rephrase.txt` + `adapter.text()` | LLM 只改语气/礼貌度，不改事实 |
| 后置校验 | `verify_grounded()` | 拒绝越界输出：新增数字/承诺词/丢失关键实体/超长 |
| 失败回退 | `compose_readonly_answer()` | 校验不过或调用异常 → 回退纯模板，回复不中断 |

- **唯一事实来源是模板**：LLM 输入即模板文本，输出必须在模板事实范围内，否则拒绝。
- **后置校验规则**：不得新增输入中不存在的数字/日期/承诺（`保证/一定/100%/赔付/赔偿/秒退/立即到账` 等）；
  模板中的订单号/运单号（`o_*` / `wb_*`）必须在输出中保留；长度不超过 `max(200, 2×模板长度)`。
- **降级可观测**：审计事件 `READONLY_ANSWER` 记录 `mode`（`llm`/`template`）、`verified`、`reason`
  （`new_number:*` / `promise:*` / `missing_entity:*` / `too_long` / `llm_error` / `no_prompt` / `llm_disabled`）。
- **只读边界不变**：投诉不取业务事实；查询缺订单号走澄清；物流不可用时降级但仍回订单事实。

**验证证据（容器实测，2026-10-25）**

- 单元/回归：容器内 `pytest -q` -> `101 passed`（新增 `tests/test_readonly_answer.py` 8 项）。
- `订单 o_1001 我的快递到哪了` → ticket `67c51a98-836d-4011-b184-5b0d480003fd`；
  回复「您好，您的订单 o_1001 目前已经付款，正在等待发货，暂时还没有查到物流信息哦。」；
  审计 `READONLY_ANSWER mode=llm verified=true`；`tool_calls` 2 行（oms/tms，`node=context_aggregator`）；
  `llm_calls` 1 行（`node=readonly_answer model=deepseek-v4-pro`）。
- `我要投诉你们客服态度太差了` → ticket `6c321665-fc28-4d3b-99eb-4893d8d812dc`；
  回复「收到您的反馈啦，我们已经记录下来了，会尽快核实；如果需要人工介入，我们也会安排专员跟进。」；
  `READONLY_ANSWER mode=llm verified=true`；`tool_calls` 0 行。