# 评审修正落地说明（12 核心节点 + 6 项缺陷）

> 适用范围：`docs/ecommerce-aftersale-agent-lld-v3.5.md` 已同步为 12 核心节点结构；本文件记录"评审建议 → 代码修改 → 验证证据"。
> 结论：**评审中的三大合并与 6 项缺陷已全部落地并通过测试/部署验证**；剩余 1 项为数据映射类阻塞项（见第五节）。

## 一、评审依据（本轮输入）
评审要求 15 个逻辑节点收敛为 **12 个核心节点 + 独立 Saga/对账 Worker + 确定性业务服务**，并修正 6 类真实缺陷。三项合并：
1. `PolicyRetrieval` + `EvidenceGate` → 合并（消除 `discover()` 被调用两次）
2. `ClarificationWriter` + `AuditResponder` → 合并为统一回复出口
3. `ReconciliationScheduler` → 移出 Graph（改为独立长跑 Worker）

## 二、图结构：15 → 12 核心节点

| 处置 | 原节点 | 现节点 | 说明 |
|---|---|---|---|
| 合并 | `PolicyRetrieval` + `EvidenceGate` | `policy_evidence` | 一次检索完成"发现→精确补齐→证据校验"；内部保留确定性校验函数 |
| 合并 | `ClarificationWriter` + `AuditResponder` | `response_composer` | 按 `response_type` 分发，所有路径统一回复出口 |
| 改名 | `MCPExecutor` | `command_submitter` | 语义即"仅提交持久化写命令" |
| 移出图 | `ReconciliationScheduler` | 独立对账 Worker | `result_verifier` 幂等建对账任务，Worker 长跑 |

实测编译图节点（12 个）：
`intent_guardrail → context_aggregator → policy_engine → policy_evidence → action_planner → action_preparation → risk_authorization → [hitl_review] → pre_execution_guard → command_submitter → result_verifier → response_composer`

代码：`app/graph/build.py`、`app/graph/nodes.py`；文档：LLD §7.1 / §7.2 / §7.4.4–§7.4.12。

## 三、6 项缺陷修正

| # | 缺陷 | 修法 | 代码 |
|---|---|---|---|
| 1 | 非退款操作默认 `AUTO_APPROVE` | 按动作类型套用确定性风控；**无适用规则/缺事实 → `MANUAL`** | `app/risk_rules.py`、`nodes.risk_authorization` |
| 2 | `PreExecutionGuard` 仅复核带 `quote_id` 的退款 | **所有动作**（退款/拦截/补发/催派）都复核；时间戳解析为带时区 `datetime` 再比较 | `nodes.pre_execution_guard` |
| 3 | 退款行项目误用 `evidence_ids` 当 `order_line_id` | 行项目改用 OMS 校验的订单行 ID（`_quote_items()`） | `nodes.action_preparation` |
| 4 | `freeze_plan()` 未把 `plan_id` 写入 State | `freeze_plan` 返回持久化 `plan_id`，与 `plan_hash` 一并写入 State；Saga 绑定 `plan_id` | `nodes.action_preparation`、`0012_saga_plan_binding.sql` |
| 5 | 外部等待与结果验证未连通 | `ResultVerifier` 按 `command_id` 查 `saga_commands` + 下游权威状态；缺终态**不谎报 `COMPLETED`**，UNKNOWN/未结算幂等建对账任务 | `nodes.result_verifier`、`app/db/persistence.py` |
| 6 | 其他实现问题 | `upsert_intent` 改 version CAS；`policy_engine` 落库 `policy_decisions`；`ActionPlanner` 传入真实事实摘要；`except` 拆分为 SCHEMA/UPSTREAM；`_intent_messages` 保留 tool_call 配对；`ResponseComposer` 用版本化模板（与命中的 `rule_id` 绑定，未命中走兜底） | `nodes.py`、`app/policy/messages.py`、`app/policy/response_templates.json` |

新增文件：`app/risk_rules.py`、`app/rag/evidence.py`、`app/policy/messages.py`、`app/policy/response_templates.json`、`migrations/sql/0012_saga_plan_binding.sql`、`tests/test_review_fixes.py`。
## 四、验证结果（实测）

- 单元测试：`docker run --rm -v E:/agent/aftersale-agent:/app -w /app aftersale-agent-api python -m pytest -q` → **34 passed**（含 `tests/test_review_fixes.py` 13 条针对本轮修正）。
- 图结构：编译图实测 **12 个核心节点**（见第二节）。
- 部署：`docker compose build api worker migrate mcp-mock` + `up -d` → postgres/redis/api/mcp-mock/embed **healthy**，worker Up；`migrate` 应用 `0012_saga_plan_binding.sql`（0011 已先行应用）。
- 冒烟：`scripts/smoke_e2e.py` → **API_SMOKE_OK + REFUND_SAGA_OK**（完整链路：意图→政策 ALLOW→风控 AUTO_APPROVE→Finance 报价→冻结 plan/quote→`WAITING_EXTERNAL`，命令入队 1 条）。
- DB 端到端（`scripts/verify_db_saga.py`，容器网络版）：
  - 图执行 `run_status=COMPLETED`；政策裁决 `ALLOW / FRESH_DAMAGED_WITHIN_WINDOW`（判定层正确）。
  - 证据层 `INSUFFICIENT`（0 条）→ **正确 fail-closed 转 `MANUAL`**，未产生任何写命令。
- 决策矩阵（`scripts/verify_policy_rules.py`，DB 后端）：**18/18 判定与 reason_code 全部正确**；但 17/18 用例的 `required_clause_ids` 在 `policy_chunks` 中**缺失**。

## 五、尚未闭合项（真实落地阻塞项）

**现象**：`policy/rules.json` 的 `required_clause_ids`（如 `CL-FRESH-A`、`CL-APPAREL-A`）指向**合成政策稿** `docs/policy/*.md` 的条款 ID；而当前 RAG 库 `policy_chunks`（205 chunk）已换成**真实平台 PDF**语料（条款 ID 形如 `CL-JD1013329543295012864-A3`）。两套 ID 不相交，故证据门禁取不到必备条款。

**为什么判定仍正确**：Policy Engine 走的是 `policy_rules`（规则表），与 RAG 语料解耦，所以"规则→决策→reason_code"完全正确；缺的只是"决策→依据条款原文"的证据闭包。

**这是设计内的待解析步骤，不是代码缺陷**：`policy_rule_clause_map` 已存在，当前 **33 条全部为 `UNRESOLVED`**（`build_policy_index.py` 明确写入"待人工/发布期解析为 parent_id"）。

**修复路径（二选一）**：
1. **首选（面向真实落地）**：为 16 条规则逐条解析到真实 PDF 条款，把 `policy_rule_clause_map` 从 `UNRESOLVED` 改为 `RESOLVED`，并让 `EvidenceService` 按解析结果取值（可复用 `logical_clause_id` / `parent_id` 做祖父扩展）。这是一项**需要业务语义判断的数据映射工作**。
2. **临时恢复绿道**：`python scripts/ingest_policy.py --all --rules --replace --database-url <APP_DSN>` 重新入库合成政策稿，使 `policy_chunks` 与 `rules.json` 的 `required_clause_ids` 对齐（会覆盖真实 PDF 语料）。

> 建议：确认要"真实平台 PDF 作为唯一证据源"后，按路径 1 建立规则↔条款映射；否则先按路径 2 让证据闭包自洽。

## 六、复现命令

```powershell
# 1) 测试（务必挂载工作区，否则跑的是镜像内旧代码）
docker run --rm -v "E:/agent/aftersale-agent:/app" -w /app aftersale-agent-api python -m pytest -q

# 2) 重建并启动
docker compose build api worker migrate mcp-mock
docker compose up -d
docker compose logs migrate --tail 5

# 3) 冒烟（内存后端，链路自洽）
docker run --rm --env-file .env -v "E:/agent/aftersale-agent:/app" -w /app -e APP_ENV=test aftersale-agent-api python scripts/smoke_e2e.py

# 4) 决策矩阵（DB 后端，需先把 DSN 主机改为 postgres 在容器内运行）
```

## 七、结论

- 评审的**节点合并**与**6 项缺陷**已全部修正，测试、图结构、部署、冒烟、决策矩阵均可复现。
- 唯一未闭合项是"规则 ↔ 真实 PDF 条款"的映射（`policy_rule_clause_map` 待解析），属于数据/语义对齐工作，需要你确认证据源口径后再做。