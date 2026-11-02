# 电商售后异常订单智能处置 Agent — 实施基线（代码落地 + Docker）

对应设计文档：`docs/ecommerce-aftersale-agent-lld-v3.5.md`（LLD v3.5）。
本仓库是该 LLD 的**可运行、可 Docker 部署**的实现：意图识别、确定性 Policy Engine、RAG 证据闭包、
LangGraph 工作流、Saga/Outbox + Worker、MCP 端口与 Mock、API/SSE、数据库迁移与测试。

> 免责：**实施基线**，非生产验证系统。接入真实资金系统前必须完成迁移执行、故障恢复与财务对账验证；
> 默认 `FINANCE_REAL_WRITES_ENABLED=false`（外部资金写禁用）。

详细部署与实测见 **`docs/DEPLOYMENT.md`**。


![python](https://img.shields.io/badge/python-3.12-blue)
![tests](https://img.shields.io/badge/tests-314%20passed-brightgreen)
![docker](https://img.shields.io/badge/docker-compose-2496ED)
![observability](https://img.shields.io/badge/metrics-prometheus-orange)
![coverage](docs/coverage.svg)
![ci](https://github.com/gufancm/aftersale-agent/actions/workflows/ci.yml/badge.svg)

## 架构总览

```mermaid
flowchart TB
  A[用户消息 / 工单] --> B[intent_guardrail 意图与安全]
  B --> C[context_aggregator 事实聚合]
  C --> D[policy_engine 确定性裁决]
  D --> E[policy_evidence RAG 证据闭包]
  E --> F[action_planner 计划]
  F --> G[action_preparation 报价预占]
  G --> H{risk_authorization 逐动作风控}
  H -->|REVIEW| I[hitl_review 人工审批] --> J[pre_execution_guard 执行前复核]
  H -->|放行| J
  J --> K[command_submitter 命令+Outbox]
  K --> L[result_verifier 结果核验]
  L --> M[response_composer 唯一出口]
  K -. 持久化命令 .-> DB[(PostgreSQL / pgvector)]
  DB -. 领取并派发 .-> W[Saga Worker / 对账 Worker]
  W --> MCP[MCP 工具 OMS·TMS·Finance]
  MCP -. 回执 / 对账 .-> DB
  E --- RAG[(BM25 + 向量 + BGE 重排)]
  MEM[(记忆系统 短期跨轮 / 情景 / 语义)]
  MEM -. 非权威注入理解与措辞 .-> B
  L -. 终态归档 / 画像更新 .-> MEM
  M --- MET[/metrics Prometheus/]
```

## 技术栈与亮点

- **确定性优先**：政策裁决、风控、执行前复核均为确定性服务；LLM 只负责理解与措辞，绝不替代资金/权限判断。
- **Fail-Closed**：缺事实、缺规则、缺审批一律转人工，绝不默认放行；真实资金写默认关闭。
- **12 节点 LangGraph + 图外 Saga/Outbox Worker**：HITL `interrupt()` 挂起与恢复、命令优先级、租约回收、对账收敛。
- **MCP 标准协议**：官方 SDK 的 stdio Server（JSON-RPC 2.0），15 个售后工具（10 只读 / 5 写）带 `readOnlyHint` 与必填幂等键，
  任意 MCP Host 可直接接入；业务逻辑仍由既有网关/处理器单点实现，不重复实现规则。
- **可恢复 HITL**：Postgres Checkpointer 持久化 `interrupt()` 暂停点（`CHECKPOINT_BACKEND=memory|postgres`），
  失败降级可见（日志 + `/ready.checkpointer_durable`），`CHECKPOINT_STRICT=true` 可拒绝「假持久化」启动。
- **节点级重试**：LangGraph `RetryPolicy` 只挂在可重入节点（意图/事实聚合/检索/规划/措辞），
  只对超时与连接类瞬时故障生效；资金写、审批暂停、执行前复核、结果回查**永不重试**（由 Saga 幂等承担）。
- **父子分块 RAG**：Docling 解析 + HybridChunker，BM25 词法 + pgvector 向量 + BGE 重排 + 自适应查询改写/HyDE。
- **跨页表格合并**：Docling `TableItem` 按「相邻页 + 同列数 + 表头一致/末行截断/版面贴边&同版心」确定性合并，
  改写 Markdown 表格块后再入库，避免续页半张表丢失表头。
- **三级意图路由**：规则快路径 / 小模型 / 大模型；多意图、注入与滥用旗标参与路由。
- **可观测性**：Prometheus `/metrics`（HTTP/LLM/工具/命令/审批/对账），审计表支撑指标与追溯。

## 目录结构

```
aftersale-agent/
  app/
    config.py                # 环境变量与启动校验（§2.2）
    main.py                  # FastAPI 入口 /health /ready（含 HITL Checkpointer 装配）
    bootstrap.py             # 依赖装配（内存 or 真实；RAG memory/pg 可选）
    domain/                  # enums（意图/来源/安全旗标/状态+白名单）、schemas（strict）、canonical、money
    llm/adapter.py           # OpenAI 兼容适配器（chat_completions/responses，§17.2），prompts/
    policy/                  # dsl engine repository seed db_repository（确定性政策，§4）
    rag/                     # 检索（BM25 词法+pgvector 向量+RRF+BGE Reranker+查询变换/证据闭包，§5）
    graph/                   # state deps nodes（12 核心节点） build（含 checkpointer，§7）
    memory/                  # 记忆系统（短期跨轮 / 情景 / 语义；非权威，仅注入理解/措辞）
    saga/                    # commands dispatcher（唯一外部写执行者，§9/§18.10）
    mcp/                     # ports client mocks/{inmemory,server}（OMS/TMS/Finance）
    db/                      # engine（tenant_transaction/system_transaction） persistence memory
    api/                     # security（JWT/租户上下文） routes（REST/SSE/审批恢复）
  migrations/sql/            # 0001..0020（…→父子分块→HNSW→Saga 计划绑定→工具审计→裁决 trace→BM25→记忆系统）
  scripts/
    migrate.py               # 幂等迁移（schema_migrations 记录 + 旧库 bootstrap）
    run_worker.py            # Saga Worker：命令派发 + 对账回查（§18.10/§19.1.6）
    smoke_e2e.py             # 本地无 DB 冒烟（API + 退款 Saga）
    verify_db_saga.py        # DB 版端到端（真实 Postgres + pg RAG + Worker）
    rebuild_lexical_index.py # 回填/重建 policy_chunks 的 BM25 索引（tokens/doc_len）
    check_retrieval.py       # 检索链路自检（查询变换+BM25+向量+RRF+Reranker）
    serve_reranker.py        # 本地 BGE Reranker 服务（cross-encoder，/rerank）
    check_memory.py          # 记忆系统自检（回放多轮历史 + 滚动摘要 + 情景/语义召回）
  tests/                     # canonical/money/intent_schema/policy_engine/graph_smoke/worker_and_hitl
  docs/                      # LLD v3.5 + DEPLOYMENT.md + 设计实测（intent/context/planner/rag/治理/记忆/CI/评测清单 eval-plan）
  .github/workflows/ci.yml   # CI：测试与覆盖率门槛 / Docker 镜像构建 / DB 端到端
  docs/coverage.svg          # 覆盖率徽章（CI 每次运行由 scripts/coverage_badge.py 生成）
  docker-compose.yml  Dockerfile  requirements.txt  requirements-ingest.txt  .env.example  .dockerignore  .gitignore
```

## 快速开始（本地，无 DB）

```bash
python -m venv .venv && . .venv/Scripts/activate      # Windows
pip install -r requirements.txt                        # 直连 PyPI 超时可用 -i 清华镜像
set APP_ENV=test
python -m uvicorn app.main:app --port 8000
```

无 DB/无 LLM 时：意图识别进入只读/人工路径，**不会产生任何写命令**（Fail-Closed）。

## 快速开始（Docker：Postgres + Redis + Mock + Worker）

```bash
cp .env.example .env      # 如需接 LLM 再填 LLM_*（模型名可留空）
docker compose build
docker compose up -d
docker compose ps         # postgres/redis/api/mcp-mock healthy；worker Up；migrate Exited(0)
```

组件、迁移机制、角色与 RLS、验证清单、故障排查见 `docs/DEPLOYMENT.md`。

## 接入 LLM（OpenAI 兼容）

只改环境变量、不改代码：`.env` 填 `LLM_BASE_URL`（含 `/v1`）、`LLM_API_KEY`、`LLM_MODEL`，
改完 `docker compose up -d api worker` 即生效（`GET /ready` 的 `llm_enabled` 变为 true，
`startup_violations` 返回阻断项供探活）。默认 Chat Completions + `response_format=json_schema`(strict)，
也支持 `LLM_API_STYLE=responses`；`LLM_MODEL` 留空 -> `LLM_DISABLED`（只读/转人工，不自动处置）。

启动校验会阻断：`LLM_CONTEXT_WINDOW < 32768`、`LLM_MAX_OUTPUT_TOKENS >= LLM_CONTEXT_WINDOW`、
端点既不支持 `json_schema` 也不支持 `json_object`。适配器做严格 Schema 校验，
不通过则降级人工，不产生写命令。

无密钥/无外网自检（本地假端点，验证「结构化输出 -> 意图 -> 政策 -> 证据 -> HITL -> Saga」整条链路）：

```bash
docker run -d --name fake-llm --network aftersale-agent_default -v E:/agent/aftersale-agent:/app -w /app \
  aftersale-agent-api python -m uvicorn --app-dir scripts --host 0.0.0.0 --port 8899 fake_openai_server:fake
docker compose run --rm api python scripts/smoke_llm_e2e.py    # -> LLM_E2E_OK
docker rm -f fake-llm
```

真实端点自检（会真实调用你配置的 LLM；`check_llm.py` 只打一次意图请求，`llm_live_probe.py` 跑完整图）：

```bash
docker compose run --rm api python scripts/check_llm.py        # -> LLM_OK
docker compose run --rm api python scripts/llm_live_probe.py   # 意图->政策->证据->HITL 审批->Saga 命令
```

接真实模型的两个常见坑：
- 端点只支持 `response_format=json_object`（如部分 DashScope 模型会返回 `This response_format type is unavailable now`）
  时，必须置 `LLM_SUPPORTS_JSON_SCHEMA=false`；适配器会把 strict schema 作为文本注入提示，
  否则模型会自造字段名/枚举值导致结构化输出校验失败。
- 推理型模型（会产出大量 reasoning tokens）需放大 `LLM_MAX_OUTPUT_TOKENS`（建议 >=8192）与
  `LLM_TIMEOUT_MS`（建议 >=120000），否则会 `MODEL_TRUNCATED` 或上游超时转人工。

## 意图识别（规则快路径 + 两级模型路由 + 多意图）

`intent_guardrail` 按“工业界三层”工作，全部可配置、可观测（审计 `INTENT_FASTPATH` / `INTENT_CLASSIFIED`）：

1. **规则快路径**：`STATUS_QUERY` / `EXPEDITE` 这类高频、低风险、表述固定的意图由
   `app/graph/intent_fastpath.py` 的规则直出（`confidence=1.0`），**不调用 LLM**，零 token 成本、低延迟。
   快路径原则是“宁可不中”：出现多诉求标记、多个订单号、写操作动词、金额、安全/注入词即放弃，回退 LLM。
2. **复杂度分级**：`estimate_complexity()` 给当前用户消息打分，低于 `LLM_INTENT_COMPLEXITY_THRESHOLD` 走小模型，
   否则走大模型；未配置 `LLM_MODEL_SMALL` 时一律走主模型（单级，行为与之前一致）。
3. **兜底**：复杂 / 不确定的请求交给 LLM 做严格 Schema 的结构化判定，失败降级人工。

**多意图**：`secondary_intents` 含写操作意图（`REFUND_ONLY/RETURN_REFUND/INTERCEPT_REFUND/RESHIPMENT/EXPEDITE`）
时不再静默丢弃，而是路由到 `clarify`，回复“请确认本次希望优先处理哪一项”；只读次要意图（如 `STATUS_QUERY`）
不阻塞主流程，但会完整记录在审计里。

**safety_flag 参与路由**（不再只记录）：`PROMPT_INJECTION` → deny；`TOOL_OR_PERMISSION_REQUEST` /
`PII_OR_CREDENTIAL` → deny；`LEGAL_OR_REGULATORY_THREAT` / `SELF_HARM_OR_SAFETY` / `ABUSE` → 人工；
`OFF_TOPIC` → 澄清。路由与回复文案由纯函数 `nodes.decide_intent_route()` 同源决定。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LLM_INTENT_FASTPATH_ENABLED` | `true` | 关闭则所有意图都走 LLM |
| `LLM_MODEL_SMALL` | 空 | 留空=单级；填写便宜的小模型后启用两级路由 |
| `LLM_SMALL_BASE_URL` / `LLM_SMALL_API_KEY` | 空 | 仅当小模型在别的端点时才需要 |
| `LLM_SMALL_MAX_OUTPUT_TOKENS` | `0` | 0=沿用主模型输出预算 |
| `LLM_INTENT_SMALL_MODEL_ENABLED` | `true` | 单独关闭“简单→小模型” |
| `LLM_INTENT_COMPLEXITY_THRESHOLD` | `2` | 复杂度分低于它才用（小模型） |

当前 `.env` 已启用方案 A：`LLM_MODEL=deepseek-v4-pro`（复杂/多意图）+ `LLM_MODEL_SMALL=deepseek-flash`（简单单意图），
已实测分级生效（`tier=small/large` + `llm_calls.model` 可核对）。小模型自检：`scripts/check_llm.py --small`。
⚠️ 小模型对个别输入会误判（已实测 1 例），是分级方案的质量上限——如需提升可改为“小模型低置信 → 升级大模型重判”。
设计与实测证据（含 ticket_id）见 `docs/intent-routing.md`。


## ContextAggregator / 工具网关

事实采集节点的权限模型（读写隔离）、只读工具网关（超时/重试/熔断/限流/审计/校验）、
关键与次要事实分级降级、`facts_version`：见 `docs/context-aggregation.md`。
只读调用审计落 `tool_calls` 表（迁移 `0014_tool_calls.sql`）。

只读自助答复（混合式：事实用模板、措辞用 LLM、失败回退模板）见 `docs/context-aggregation.md` §10，
开关 `READONLY_LLM_REPHRASE_ENABLED`（默认 `true`）。

## 调用示例

```bash
# 建单（order_id 允许为空 → NEED_INFO）
curl -X POST http://localhost:8000/api/v1/tickets \
  -H "X-Tenant-Id: tenant_demo" -H "X-User-Id: u_demo" \
  -H "Idempotency-Key: 7f0f..." -H "Content-Type: application/json" \
  -d '{"order_id":"o_1001","intent":"REFUND_ONLY","initial_message":"生鲜坏了，想退款"}'

# 追加消息并触发一次 run
curl -X POST http://localhost:8000/api/v1/tickets/<ticket_id>/messages \
  -H "X-Tenant-Id: tenant_demo" -H "X-User-Id: u_demo" \
  -H "Content-Type: application/json" -d '{"text":"我已经上传照片了"}'

# 审批（HITL：interrupt 后 Command(resume=...) 恢复）
curl -X POST http://localhost:8000/api/v1/tickets/<ticket_id>/approval \
  -H "X-Tenant-Id: tenant_demo" -H "X-User-Id: reviewer_1" \
  -H "Content-Type: application/json" \
  -d '{"approval_id":"<approval_id>","decision":"APPROVED"}'

# SSE（断线用 Last-Event-ID 续传）
curl -N http://localhost:8000/api/v1/tickets/<ticket_id>/events \
  -H "X-Tenant-Id: tenant_demo" -H "X-User-Id: u_demo" -H "Last-Event-ID: 0"
```

## 安全边界（不可违反）

- `LLM 输出 != 业务授权 != 工具执行结果`：三层分别校验。
- 意图识别节点**零工具调用**；输出不构成资金授权。
- 政策裁决只由确定性 Policy Engine 产生；RAG 只提供证据。
- `CommandSubmitter`（原 `MCPExecutor`）只写 `saga_commands + outbox_events`；**唯一外部写执行者是 Worker（Saga Dispatcher）**。
- 可退余额由 Finance 唯一管理；Agent 不建第二套账本。
- `timeout != failure`：写超时进 `UNKNOWN`，先回查/对账，绝不盲重试。
- 应用数据库角色**非超级用户、无 BYPASSRLS**，RLS 对业务真正生效。

## 政策库（真实平台规则）

**单一事实源**：`policy/clauses_jd.json`（17 份京东开放平台官方 PDF 解析出的 205 条条款）与
`policy/rules.json`（20 条消费者裁决规则 + 11 条非裁决清单 catalog）。内存后端（`RAG_BACKEND=memory` /
无 DB）与数据库读的都是这一套内容；生成规则时强校验「每条规则的依据条款必须真实存在」。

```bash
# 生成规则（强校验条款链接，缺失即失败）
python scripts/gen_rules_from_corpus.py
# 规则->条款 断链检查（离线；--source db 校验已入库条款）
python scripts/check_rule_clause_links.py
# 入库规则（清空该租户后写入 policy_rules；容器内主机名 postgres）
docker compose run --rm api python scripts/ingest_policy.py --rules --replace
# 真语料重建条款（可选，写入含 parent_id / logical_clause_id 的 child chunk）
docker compose run --rm api python scripts/ingest_policy.py --from-clauses-jd --replace --embed
# 决策矩阵验证（DB 规则 + DB 条款，24/24）
python scripts/verify_policy_rules.py      # -> POLICY_RULES_OK
# 查看某工单的政策裁决 trace（为何放行/拒绝/转人工、每条规则各条件取值）
docker compose run --rm api python scripts/show_policy_trace.py <ticket_id>
```

**决策 trace（可解释性）**：每次裁决在 `PolicyDecision.trace` 产出结构化留痕——载入了哪些规则、
每条适用规则各条件的实际取值与判定结果、命中集合、优先级/具体度消解、冲突与缺失事实、胜出规则与结论，
落库到 `policy_decisions.trace`（迁移 `0015_policy_decision_trace.sql`）并写入审计。详见 `docs/policy-decision-trace.md`。

- 索引与依据：`docs/policy/README.md`；真实语料映射复核表：`docs/real-corpus-rule-mapping.md`。
- 决策只由确定性 Policy Engine 产生；命中取最高优先级，同优先级看条件更具体；无命中→ `NEED_INFO`(追问) 或 `MANUAL`(转人工)。
- 真实语料未覆盖的诉求（生鲜破损赔付、服饰/图书质量细则、预售、价保）**不设规则**，一律转人工，绝不由 LLM 编造政策。
- 退款金额上限未在真实条款中给出 → 风控对退款默认 `REVIEW`（人工审批），不做无依据的自动放行。
- 政策判定字段：订单品类/状态、`claim.reason_code`、物流状态、意图等**可信事实**（LLM 仅提案）。

## ActionPlanner（确定性快路径 + 计划审查 + 有界重规划）

- 只提案、不授权：动作在意图白名单内由 `validate_plan_against_intent` 守护，金额上限/证据要求/条款全部来自 PolicyDecision 与 Evidence。
- **快路径**：意图明确 + 单动作 + 政策 `ALLOW` + 证据 `OK` + 必需槽位齐（退款/补发还需无歧义 SKU）→ 直出计划、跳过 LLM（审计 `PLAN_FASTPATH`）。
- **计划审查**（确定性 critic 始终执行）：`FATAL`→转人工（政策/证据矛盾、SKU 不在订单、数量超订单、重复动作、订单号不一致、非法依赖）；`REPLAN`→事实已有但计划漏填，有界重规划补回；`CLARIFY`→事实也没有，需用户补充；`WARN`→仅记录。可选 LLM 复核默认关闭（`PLANNER_LLM_CRITIC_ENABLED`）。
- **有界重规划**：`REPLAN` 反馈回 `action_planner`，最多 `PLANNER_MAX_REPLANS`（默认 1）轮，防死循环。
- **依赖 DAG**：`ActionProposal.depends_on`（0<=dep<自身下标）声明先后依赖，`command_submitter` 拓扑序提交。
- **单一元数据源**：`app/domain/action_registry.py` 定义每个动作的 `kind/risk_class/required_slots/idempotency_template/preconditions`；外部写集合与命令幂等键都从这里取值。
- **plan_id 前移**：planner 即落 `DRAFT` 计划，`action_preparation` 原地冻结为**同一 plan_id**（`VALIDATED`），风控/HITL/命令全链路引用同一 id。
- **报价预占（hold）**：`action_preparation` 报价即冻结一笔可用余额预占（`reservation_reference`，落 `refund_quotes`，迁移 `0017`），
  额度不足则 fail-closed（`BALANCE_INSUFFICIENT`）；执行前复核校验 hold，执行时消费 hold（`authorize/hold → capture`）。
- **requote 上限**：报价/预占过期 → `reprepare` 重报价，超过 `MAX_REQUOTE_CYCLES` → 转人工（防死循环）。
- 开关：`PLANNER_FASTPATH_ENABLED=true`、`PLANNER_LLM_CRITIC_ENABLED=false`、`PLANNER_MAX_REPLANS=1`。
- 原理、审查分级与实测证据见 `docs/action-planner.md`。

## 收口节点（7–12）加固

RiskAuthorization / HITLReview / PreExecutionGuard / CommandSubmitter / ResultVerifier / ResponseComposer
六节点的评审优化项已全部落地：

- **风控**：逐动作取最严；退款新增买家历史维度（频次/30 天累计/黑名单），**缺事实 → MANUAL 不默认放行**。
- **审批治理**：审批人角色门（`APPROVAL_ALLOWED_ROLES`，403）、恢复时 `plan_hash` 一致性与过期拦截、SLA 过期升级。
- **执行前复核**：覆盖全部写动作；`quote_order_version` + `plan_frozen_facts_version` 双重 CAS 防漂移。
- **命令/Outbox**：派发优先级（资金 < 物流）、outbox relay 置 `PUBLISHED`、命令取消入口。
- **结果核验**：按 `command_id` 核验；补发回查 OMS `get_reshipment_status`、催派回查 TMS `get_dispatch_status`；
  UNKNOWN → 幂等对账 → `MANUAL_RECONCILE` 入人工队列。
- **回复出口**：事实（金额/到账时效）注入、多语言 i18n、多渠道分发审计。

原理、配置项、迁移（`0018`/`0019`）与可核对 SQL 见 **`docs/nodes-7-12-hardening.md`**。

## MCP Server（stdio / JSON-RPC 2.0）

`app/mcp/stdio_server.py` 用官方 `mcp>=2.0` SDK 把售后工具暴露为标准 MCP Server；
任何 MCP Host（Claude Desktop、Codex、IDE Agent）可直接接入，不需要另写适配层。

```bash
python -m app.mcp.stdio_server                        # stdio（默认，JSON-RPC over stdio）
python -m app.mcp.stdio_server --transport streamable-http   # 远程部署备选
python -m pytest -q tests/test_mcp_stdio_server.py    # 含子进程 stdio 真实握手
```

| 域 | 工具（15 个） | 类型 |
| --- | --- | --- |
| OMS | `get_order_detail` `get_reshipment_status` `get_buyer_refund_stats` | 只读 |
| OMS | `hold_fulfillment` `create_reshipment` | 写（必填 `idempotency_key`） |
| TMS | `get_tracking_info` `get_dispatch_status` `get_interception_status` | 只读 |
| TMS | `intercept_parcel` `expedite_delivery` | 写（必填 `idempotency_key`） |
| Finance | `get_refundable_balance` `calculate_refund_amount` `get_refund_status` `get_reconciliation_result` | 只读 |
| Finance | `execute_refund` | 写（必填 `idempotency_key` + 审批授权） |

Host 侧配置（`MCP_STDIO_BACKEND=asgi` 为进程内直连 mock，`http` 则转发到 `MCP_MOCK_URL`）：

```json
{"mcpServers": {"aftersale": {"command": "python", "args": ["-m", "app.mcp.stdio_server"],
  "env": {"MCP_STDIO_BACKEND": "asgi", "PYTHONIOENCODING": "utf-8"}}}}
```

错误语义：订单不存在/租户越权等**可预期失败**以 `isError=true` + 业务错误码返回（`ORDER_NOT_FOUND`、`TENANT_FORBIDDEN`），
不是被折叠成 “Error executing tool”。写工具缺 `idempotency_key` 在参数校验阶段即被拒绝，不会到下游。

## Checkpointer 与节点重试

- **HITL 暂停点持久化**：`CHECKPOINT_BACKEND=postgres` 时使用 `AsyncPostgresSaver`（表由 `CHECKPOINT_SETUP_ON_START` 自动建），
  `interrupt()` 暂停 → 进程重启 → `POST /tickets/{id}/approval` 仍能 `Command(resume=...)` 续跑。
  `memory` 仅用于测试；连接失败会降级并输出 ERROR，`/ready` 暴露 `checkpointer` 与 `checkpointer_durable`。
  回归用例 `tests/test_checkpointer.py::test_interrupt_survives_simulated_restart` 用**两个独立 saver + 两个图对象**模拟重启。
- **节点级重试**：`app/graph/retry.py` 定义白名单 `RETRY_SAFE_NODES`（意图/事实聚合/检索/规划/措辞）与
  `NO_RETRY_NODES`（政策裁决/报价准备/风控/审批/执行前复核/命令提交/结果回查）。`retry_on` 复用网关同一套
  瞬时故障判定（超时、连接重置、上游 5xx），业务错误与授权错误立即失败。
  `tests/test_graph_retry_policy.py` 断言编译后图上写节点 `retry_policy is None`，并验证「超时重试 3 次成功 / ValueError 不重试」。

## Docling 跨页表格合并

`app/rag/table_merge.py` 对 `doc.tables` 做确定性后处理，判据按强度递减：

1. 表头归一化后相同 → 拼接正文并去掉续页重复表头；
2. 上一页末行被切断（末尾空单元格）→ 同一行跨页，直接拼接；
3. 版面证据：上表结束于页底（≥78%）、下表起于页顶（≤18%）、左右边界一致（同版心）→ 续页未重复表头，按数据行拼接。

页间距、列数、版心任一不符即不合并（宁可少并，不可错并）。`scripts/parse_platform_pdfs.py` 会额外产出：
`*.tables.merged.md`（合并后的表格附录）、`*.docling.merged.md`（**已把 Markdown 表格块替换为合并版本**，可直接回灌切块），
并在 `_parse_report.csv` 记录 `docling_tables / tables_merged / table_merge_pairs / md_tables_replaced`。

实测（17 份京东政策 PDF，Docling 2.x）：解析 **17/17 成功**；识别表格 **49 张 → 合并后 27 张**（跨页/同页拆分共 **22 处**），
其中 6 份文档的 Markdown 表格块被改写（**34 个块**）。例：7 天无理由退货规则 9→4、取消订单规则 10→4、售后管理规则 10→6。
已知残留：个别商品表格因 Docling 单元格对齐失败被识别成 2 列表头块（与 3 列表体列数不一致），按「不合并」处理，需在条款映射阶段人工兜底。

## 测试

```bash
docker compose run --rm api python -m pytest -q                     # 全部通过（数量见下方 CI 徽章）
# 测试通过 tests/conftest.py 屏蔽 LLM_*/EMBEDDING_*/RAG_* 环境变量并禁用 .env，
# 因此开发机或容器里配了 LLM_MODEL_SMALL 也不会让测试误调真实端点。
# 无外部依赖（内存后端，不调真实 LLM）
docker compose run --rm api python scripts/smoke_e2e.py             # REFUND_SAGA_OK
docker compose run --rm api python scripts/smoke_llm_e2e.py         # LLM_E2E_OK（假端点）

# 需要 DB：容器内主机名是 postgres/mcp-mock，脚本默认 localhost（宿主机运行），二选一
# a) 宿主机运行（postgres 端口已发布到 localhost:5432）
#     set POLICY_APP_DSN=postgresql+asyncpg://aftersale_app:app_pw_change_me@localhost:5432/aftersale
#     python scripts/verify_policy_rules.py
# b) 容器内运行（显式指定主机名）
docker compose run --rm api \
  -e POLICY_APP_DSN=postgresql+asyncpg://aftersale_app:app_pw_change_me@postgres:5432/aftersale \
  -e POLICY_WORKER_DSN=postgresql+asyncpg://aftersale_worker:worker_pw_change_me@postgres:5432/aftersale \
  -e MCP_MOCK_URL=http://mcp-mock:9100 \
  python scripts/verify_policy_rules.py                             # POLICY_RULES_OK
docker compose run --rm api \
  -e POLICY_APP_DSN=postgresql+asyncpg://aftersale_app:app_pw_change_me@postgres:5432/aftersale \
  -e POLICY_WORKER_DSN=postgresql+asyncpg://aftersale_worker:worker_pw_change_me@postgres:5432/aftersale \
  -e MCP_MOCK_URL=http://mcp-mock:9100 \
  python scripts/verify_db_saga.py                                  # DB_SAGA_OK

# 真实 LLM 链路
docker compose run --rm api python scripts/check_llm.py             # LLM_OK
docker compose run --rm api python scripts/demo_ticket.py           # 建单->发消息->审批->终态
```

## 记忆系统（短期跨轮 / 情景 / 语义）

三类记忆，均为"非权威"，**只影响理解（意图提示）与措辞，绝不进入政策/风控/金额/审批**：

- **短期跨轮**：进图前从 `ticket_messages` 按 `message_seq` 回放最近多轮历史（`_load_memory`），超过预算的更早内容
  由 LLM 折叠为**滚动摘要**（`conversation_summaries`）；`response_composer` 把最终回复写回消息历史，形成下一轮上下文。
  修复了此前 `thread_key` 每轮新建、历史未回填导致意图"只看一条消息"的断链。
- **情景记忆**：终态工单归档为摘要 + BGE-M3 向量（`episodic_memories`，HNSW cosine），按租户/用户作用域召回，
  在意图提示中作为"历史工单记忆（非权威，仅供参考）"注入。
- **语义记忆**：用户画像（`last_intent/last_category/locale`，`user_profiles`），非权威。

非权威边界有源码级回归测试守护（确定性节点不得引用 `memory_context`/`deps.memory`）；记忆失败一律降级，不影响主流程。
原理、配置项与可核对实测证据（含 ticket_id / message_seq / score）见 **`docs/memory-design.md`**；
自检命令：`docker compose exec api python scripts/check_memory.py <ticket_id> tenant_demo u_demo`。

## CI 与覆盖率

- **CI（GitHub Actions）**：`.github/workflows/ci.yml`，四个 Job ——
  `test`（`pip install -r requirements.txt` → `pytest --cov=app ... --cov-fail-under=70` → 生成覆盖率徽章 → Job Summary → 上传产物）、
  `eval-offline`（E6/E9/E1/E28 离线评测，门槛不达标即失败）、
  `docker-build`（`docker compose build api worker migrate mcp-mock`）、
  `docker-e2e`（真实 Postgres + Worker + MCP Mock：`verify_db_saga.py` + HTTP 冒烟；LLM 关闭、RAG 走内存条款）。
- **覆盖率**：`source = app`，当前 **75.6%（4798/6346 语句）**，门槛 70%；徽章 `docs/coverage.svg` 由
  `scripts/coverage_badge.py` 从 `coverage.xml` 生成（零外部依赖、无需 Codecov）。薄弱模块均为只能端到端验证的
  `app/db/persistence.py`、`app/rag/pg.py`、`app/mcp/client.py`、`app/main.py`、`app/api/routes.py`（已由 `docker-e2e` 覆盖）。
- Job 细节、本地复现命令、如何启用 CI 状态徽章：见 **`docs/ci.md`**。
- 本地跑覆盖率：`pytest -q --cov=app --cov-report=term-missing --cov-fail-under=70`（需 `pytest-cov`，已入 `requirements.txt`）。

## 已知边界 / 下一步

- 评测集：**MVP 五集已建**——E1 意图金标（410 条，冻结 **v1.0**）、E6 政策决策矩阵（120）、E9 证据闭包（63）、
  E28 注入对抗（45）、E35 UAT 场景（17）；完整 39 集清单见 **`docs/eval-plan.md`**。
- **E1 v1.0 正式指标（live）**：主意图 accuracy **0.9805**（402/410）、macro-F1 **0.9811**、次意图 micro-F1 0.9048、
  安全旗标 micro/macro-F1 **0.9637/0.9829**（8 类旗标 recall 全 = 1.000）、路由准确率 **0.9805**、
  **危险请求错误放行率 0.0000（0/37，deny 类 0/27）**。复现：`python tests/eval/run_intent_metrics.py --gate`
  （结果缓存 `tests/eval/cache/intent_live_cache.jsonl`，回放零成本）；已知偏差登记 `tests/eval/e1_known_deviations.json`。
- ActionPlanner 已落地（2026-10-25）：确定性快路径 + 确定性计划审查（+可选 LLM 复核）+ 有界重规划 +
  依赖 DAG + plan_id 前移（同一 plan_id 贯穿审批/风控/命令）；动作元数据统一到 `action_registry`。
  规划质量金标评测集本轮明确不建（收尾统一测评）。原理与实测见 `docs/action-planner.md`。
- 只读自助答复已落地（2026-10-25）：`STATUS_QUERY/COMPLAINT` 不再返回占位文案，改为进
  `context_aggregator` 的**只读自助分支**——用确定性**事实模板**渲染真实订单/物流状态，再让 LLM **仅做措辞润色**，
  输出经**后置校验**（不得引入模板外新事实/数字/承诺、必须保留关键实体），不合格即**回退纯模板**。
  原理与实测证据见 `docs/context-aggregation.md` §10、`docs/intent-routing.md` §7。
- `reason_code` 词表：提示词已移除 `COUNTERFEIT` / `NOT_RECEIVED`（引擎无对应规则）；如需支持需先补规则。
- 多动作授权（已修复）：风控与执行前复核改为**逐动作**覆盖 `_write_actions()`，逐动作取最严结论
  （任一动作被拒/转人工则整体不执行）；`command_submitter` 增加纵深防御——写动作若未被本轮风控
  逐动作覆盖则 fail-closed、一条命令都不写（审计 `COMMAND_SUBMIT_BLOCKED`）。回归测试见
  `tests/test_multi_action_authorization.py`。当前每个意图仍最多 1 个外部写动作（不变量有测试守护）。
- HITL 重复审计（已修复）：`ensure_approval_task` 改为返回 `created` 标志（SQL 用 `ON CONFLICT DO NOTHING`
  + 回查），`hitl_review` 仅在首次新建时落 `APPROVAL_REQUIRED`；`interrupt()` 恢复重放不再产生重复事件。

- 检索链路（工业界标准）：真实 **BM25**（jieba 分词 + idf/tf 饱和 + 长度归一化，索引列 `tokens/doc_len`，迁移 `0016`）
  + **pgvector Dense** → **RRF** 融合 → **BGE Reranker**（cross-encoder）精排；查询侧默认 **adaptive**：首轮 BM25+Dense 先跑，只有**硬信号**（无候选/候选不足/Top1 过低/必备条款未覆盖）或**两条以上软信号**（短查询、间隔小、含指代）才触发**多查询改写 + HyDE**；指代优先用会话上下文做零成本扩展，TTL 缓存 + 日预算 + 2.5s 超时降级，`off/always/adaptive` 可配。
  `RAG_BACKEND=pg` 且未配置 `EMBEDDING_MODEL` 时**拒绝启动**（不再静默退化为空）；确需仅 BM25 请显式
  `RAG_ALLOW_LEXICAL_ONLY=true`。检索只负责**发现**，授权证据仍由 `required_clause_ids` 精确补齐 + 确定性校验
  （条款覆盖/版本/冲突/例外闭包，见 `docs/rag-retrieval.md`、`docs/context-aggregation.md`）。
- 退款报价预占已落地（2026-10-25）：报价与可用余额 hold 原子产生，`refund_quotes.reservation_reference` 落库（迁移 `0017_refund_reservation.sql`），
  执行前复核校验 hold、执行时消费 hold；requote 有 `MAX_REQUOTE_CYCLES` 上限。实测见 `docs/action-planner.md` §7.5。
- 记忆系统已落地（2026-10-25）：短期跨轮（历史回放 + 滚动摘要 + 回复回写）、情景记忆（终态工单摘要 → BGE-M3 向量召回）、
  语义记忆（用户画像，非权威）；**程序性记忆明确不做**。记忆绝不进入政策/风控/金额/审批（有源码级回归测试守护）。
  迁移 `0020_memory.sql`；原理与实测见 `docs/memory-design.md`。
- CI 与覆盖率已落地（2026-10-26，2026-11-02 四 Job 全绿）：GitHub Actions 四 Job（测试+覆盖率门槛 / 离线评测 /
  镜像构建 / DB 端到端 + HTTP 冒烟）；覆盖率徽章零外部依赖自动生成。
  仓库已发布：`https://github.com/gufancm/aftersale-agent`，CI 见 Actions 页。详见 `docs/ci.md`。
- 检索离线评测（recall@k/MRR/nDCG）与金标集：本轮明确暂缓，项目收尾统一测评。
- HITL 检查点当前为进程内 `InMemorySaver` → API 单副本；多副本需 PostgresSaver/RedisSaver。
- Outbox Relay 已落地（Worker `mark_outbox_published`）；评测金标集、SLO 校准、许可证见 LLD v3.5 第 14/17 章。
- 审批检查点仍为进程内 `InMemorySaver`（多副本需 PostgresSaver/RedisSaver）；多渠道分发目前写审计，接入真实短信/邮件网关需实现 `Notifier` 端口。
- `/metrics` 为进程内指标注册表（单副本抓取）；多副本应改集中式（Pushgateway / OTel Collector）。
