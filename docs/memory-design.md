# 记忆系统设计与落地（短期跨轮 / 情景 / 语义）

> 本文对应代码：`app/memory/`、`app/api/routes.py`、`app/graph/nodes.py`、`app/db/{memory,persistence}.py`、
> 迁移 `migrations/sql/0020_memory.sql`；测试 `tests/test_memory.py`；自检脚本 `scripts/check_memory.py`。
> 程序性记忆（procedural memory）本轮**明确不做**。

## 1. 三类记忆与边界

| 类型 | 作用 | 存储 | 权威性 | 注入点 |
| --- | --- | --- | --- | --- |
| 短期跨轮 | 同一工单多轮对话的连续理解（"那如果到货了发现破损"仍能指代上一轮的订单） | `ticket_messages` + `conversation_summaries` | 参考（原始消息即事实来源） | 意图提示 `_intent_messages` |
| 情景记忆 | 该用户历史工单的处置摘要 → 向量召回，供同类问题参考 | `episodic_memories`（pgvector 1024 + HNSW cosine） | **非权威** | 意图提示（`render_memory_note`） |
| 语义记忆 | 用户画像/偏好（当前意图、品类、语言） | `user_profiles` | **非权威** | 意图提示（措辞/理解） |

**硬约束**：记忆只用于「理解（意图提示）」与「措辞（回复）」，**绝不**作为
`PolicyEngine`、`RiskAuthorization`、金额、审批等确定性路径的输入。缺依赖/异常一律降级为空，
绝不影响主流程（`MemoryService._safe` 与各调用点的 `try/except`）。

## 2. 数据模型（迁移 0020，幂等）

- `ticket_messages.message_seq BIGINT`：消息严格插入顺序（`ticket_message_seq` 序列 + 唯一索引），
  历史数据按 `created_at, message_id` 回填；多轮历史按 `message_seq` 顺序回放。
- `conversation_summaries(tenant_id, ticket_id, summary, up_to_message_count, version)`：单工单滚动摘要。
- `episodic_memories(memory_id, tenant_id, user_id, ticket_id, intent, outcome_type, summary, embedding vector(1024), metadata)`：
  情景记忆 + 向量，`idx_episodic_hnsw`（`vector_cosine_ops`）。
- `user_profiles(tenant_id, user_id, profile jsonb, version)`：非权威画像。
- 三张新表启用并强制 RLS（`tenant_guard_*` 策略，与 `0007/0010` 同口径），并授权 `aftersale_app/aftersale_worker`。

## 3. 短期跨轮记忆（修复"断链"）

### 3.1 原缺陷
- `routes.py` 用 `thread_key = f"{ticket_id}:{run_id}"`，而每条消息都新建 `run_id` → 每轮都是新线程；
- 进图前只塞入**当前一条**消息，且没有从 `ticket_messages` 回填历史 → `_intent_messages` 实际只看到 1 条；
- 没有任何节点把回复写回消息历史；`conversation_summary` 是占位字段，无人读写。

### 3.2 实现
1. **回填历史**：`routes.append_message` 在 `graph.ainvoke` 之前调用 `_load_memory()`，经
   `MemoryService.load_conversation()` 读取最近 `MEMORY_MAX_TURNS*2+2` 条消息，按 `message_seq` 升序
   写入 `state["messages"]`（保留 `thread_key` 的 run 级隔离，避免跨轮 state 串味）。
2. **滚动摘要**：更早于最近 `MEMORY_RECENT_TURNS*2` 条的内容，在
   `estimate_tokens(older) > MEMORY_SUMMARY_TOKENS` 且 LLM 可用时，用 `memory_summary` 提示压缩成摘要，
   落库 `conversation_summaries`（记录 `up_to_message_count`，覆盖已够则不重复调用 LLM）；
   摘要以 `state["conversation_summary"]` 注入。
3. **回复回写**：`response_composer` 通过 `_remember_turn()` 把最终回复以 `role=assistant` 写回
   `ticket_messages`（幂等键 `<run_id>:assistant`），成为下一轮历史的来源。

### 3.3 配置
`MEMORY_RECENT_TURNS=8`（逐字窗口）、`MEMORY_MAX_TURNS=12`（装载上限）、`MEMORY_SUMMARY_TOKENS=1500`
（摘要触发/预算）、`MEMORY_SUMMARY_ENABLED=true`（关则仅窗口截断、不生成摘要）。

## 4. 情景记忆

- **归档时机**：`response_composer` 收口时归档，仅 **终态** 回复类型参与
  （`SUCCEEDED / FAILED / ACCEPTED / PROCESSING / MANUAL / DENIED`）；澄清/未知等中间态不归档。
- **摘要来源**：优先 `memory_episode` 提示 + LLM 归纳；LLM 不可用或失败时回退**确定性模板**
  （意图/订单/品类/政策裁决/理由码/动作/金额 + 结果摘要），保证可审计、可离线。
- **向量**：BGE-M3（`EMBEDDING_MODEL`，1024 维）对摘要编码，写 `episodic_memories.embedding`。
- **召回**：按 `tenant_id`（RLS + 显式条件）与 `user_id` 作用域，`embedding <=> query` cosine 近邻，
  距离阈值 `MEMORY_EPISODIC_MIN_SCORE=0.35`，返回 `MEMORY_EPISODIC_TOP_K=3` 条。
- **注入**：`render_memory_note()` 渲染为**显式标注"非权威、不得据此决策"**的提示段落。

## 5. 语义记忆（非权威，可选）

- 字段：`last_intent` / `last_category` / `locale`（`MemoryService.profile_patch_from_state()`）；
  空值不入库；合并写入（`ON CONFLICT DO UPDATE` 递增 `version`）。
- 读取：`build_context()` 与情景记忆一起注入意图提示。
- 关闭开关：`MEMORY_SEMANTIC_ENABLED=false` 时读写均不生效。

## 6. 非权威边界（有测试守护）

- `render_memory_note()` 文段自带「非权威…不得作为政策依据/不得据此决策」标注。
- 回归测试 `test_deterministic_nodes_do_not_read_memory_context`：对
  `policy_engine / risk_authorization / action_preparation / pre_execution_guard / command_submitter /
  context_aggregator / hitl_review / _guard_*` 断言其函数体**不含** `memory_context`、`render_memory_note`、`deps.memory`。
- 风控的事实来源仍是 OMS 实时只读工具（退款频次/黑名单等），**不使用**记忆。

## 7. 配置项一览

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `MEMORY_RECENT_TURNS` | 8 | 逐字保留的最近轮次（×2 = 消息数） |
| `MEMORY_MAX_TURNS` | 12 | 单次装载的历史上限 |
| `MEMORY_SUMMARY_TOKENS` | 1500 | 摘要触发阈值与预算 |
| `MEMORY_SUMMARY_ENABLED` | true | 关闭则仅窗口截断 |
| `MEMORY_EPISODIC_ENABLED` | true | 情景记忆总开关 |
| `MEMORY_EPISODIC_TOP_K` | 3 | 召回条数 |
| `MEMORY_EPISODIC_MIN_SCORE` | 0.35 | cosine 相似度阈值 |
| `MEMORY_EPISODIC_LLM_SUMMARY` | true | 关则用确定性模板摘要 |
| `MEMORY_SEMANTIC_ENABLED` | true | 语义记忆总开关 |

## 8. 实测证据（可核对，Docker + 真实 DeepSeek + BGE-M3）

- 短期跨轮：工单 `e4ae5154-54df-4a18-ba14-2a684bab8005`
  `ticket_messages.message_seq = 47(user) / 48(assistant) / 49(user) / 50(assistant)`；
  第 2 轮仅说"那如果到货了发现破损，我想退款"（未复述订单号），仍正确解析到上一轮的 `o_1002` 并进入退款流程。
  `scripts/check_memory.py` 回放：`回放消息数=3`，内容为 user→assistant→user 顺序。
- 滚动摘要：同一工单以 `MEMORY_RECENT_TURNS=1, MEMORY_MAX_TURNS=2, MEMORY_SUMMARY_TOKENS=5` 运行后，
  `conversation_summaries` 落库 1 行（`up_to_message_count=2, version=1`），
  摘要="用户查询订单 o_1002 的物流进度；客服反馈该订单状态为已送达，但物流信息暂未查到。未决事项：物流信息缺失，待进一步核实。"
  （默认配置重跑仍复用该摘要，不再调用 LLM）。
- 情景记忆：工单 `e4ae5154…` 审批通过收口后 `episodic_memories` 落库 1 行
  （`intent=REFUND_ONLY, outcome_type=PROCESSING, has_vec=t`）；
  新工单 `386bdfac-8588-4db0-9d55-1949236afa47` 召回该记忆，`score=0.6944`（阈值 0.35）。
- 语义记忆：`user_profiles(tenant_demo, u_demo)` = `{"last_intent":"REFUND_ONLY","last_category":"APPAREL"}`。
- 迁移：`docker compose run --rm migrate` → `applied 0020_memory.sql`；`message_seq` 46 条历史消息 0 条为空，3 条 RLS 策略生效。

## 9. 测试（`tests/test_memory.py`，15 例）

多轮回放、意图提示多轮可见、滚动摘要生成/落库/预算与复用、无 LLM 时跳过摘要、
情景归档与向量召回、租户/用户隔离、分数阈值与开关、仅终态归档、画像读写与补丁与开关、
`build_context` 聚合、`response_composer` 回写历史/归档/画像、`_load_memory` 装载、非权威边界源码守护、
记忆提示非权威标注、记忆指标（`aftersale_memory_*`）暴露。

## 10. 与工业界做法的差距 / 后续可做

- **未做**：程序性记忆（明确不做）；跨工单的用户级偏好**LLM 抽取**（当前为确定性字段）；
  摘要**增量合并**（当前覆盖窗口外内容整体重算）；记忆 TTL/遗忘/衰减与容量治理；
  记忆命中/采纳率离线评测（与检索评测一并收尾）。
- **可演进**：把 `user_profiles` 升级为经 LLM 抽取 + 人工确认的偏好档案；情景记忆引入
  "成功/失败"权重与去重（`dedup_key`）；摘要改为 PostgresSaver 等持久检查点时与状态机统一。