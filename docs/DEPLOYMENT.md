# 部署与运行指南（aftersale-agent / LLD v3.5 实施基线）

本文给出**可直接落地**的部署步骤与**已验证**的运行结果。设计依据见 LLD v3.5（本仓库 `docs/`）。

> 交付边界：这是实施基线，不是已通过生产验证的系统。接入真实资金系统前，必须完成迁移执行、
> 故障恢复演练与财务对账验证；默认 `FINANCE_REAL_WRITES_ENABLED=false`（外部资金写禁用）。

---

## 1. 组件与拓扑

| 组件 | 说明 | 端口 |
| --- | --- | --- |
| `postgres` | `pgvector/pgvector:pg16`，承载工单/政策/资金/Saga/事件/审计 | 5432 |
| `redis` | 预留（缓存/分布式锁），可与 RAG/限流结合 | 6379 |
| `migrate` | 一次性任务：应用 `migrations/sql/*`（幂等，含记录表） | - |
| `api` | FastAPI（REST + SSE），以**受 RLS 约束**的 `aftersale_app` 连接 | 8000 |
| `worker` | Saga Dispatcher + 对账回查，以 `aftersale_worker`（BYPASSRLS）连接 | - |
| `mcp-mock` | OMS/TMS/Finance 联调 Mock（同一进程，按路径区分） | 9100 |

数据流：`API → LangGraph(12 核心节点 + 独立对账 Worker) → 写 saga_commands + outbox（同事务）→ Worker 领取 → MCP 写工具 → 回写状态`。
**唯一外部写执行者是 Worker（Saga Dispatcher）**；节点本身不发起外部写。

---

## 2. 前置条件

- Docker Desktop / Docker Engine（本基线在 `Docker Engine 29.x` 验证通过）。
- 可用网络拉取镜像（`python:3.12-slim`、`pgvector/pgvector:pg16`、`redis:7-alpine`）。
- 构建期 pip 默认走清华镜像（`Dockerfile` 内 `ARG PIP_INDEX_URL`），可 `--build-arg` 覆盖。

---

## 3. 一键部署

```bash
cd aftersale-agent
cp .env.example .env          # 首次；如需接 LLM 再填 LLM_*（模型名可留空）
docker compose build
docker compose up -d
docker compose ps             # 期望：postgres/redis/api/mcp-mock healthy；worker Up；migrate Exited(0)
```

查看迁移结果：

```bash
docker compose logs migrate | tail -n 12
# applied 0001_extensions.sql ... applied 0009_roles.sql
# migrations complete
```

无 LLM（`LLM_MODEL=<YOUR_MODEL>` 或空）时，意图识别进入**只读/人工**分支，**不会产生任何写命令**（Fail-Closed）。

---

## 4. 接入 LLM（OpenAI 兼容，改环境变量即可，不改代码）

在 `.env` 中设置：

```
LLM_BASE_URL=https://api.openai.com/v1     # 或任意 OpenAI 兼容端点
LLM_API_KEY=sk-...
LLM_MODEL=<你的模型名>
LLM_API_STYLE=chat_completions             # 或 responses
LLM_CONTEXT_WINDOW=32768                   # 必须 >= 32768，否则启动校验拒绝自动处置
LLM_MAX_OUTPUT_TOKENS=4096
LLM_SUPPORTS_JSON_SCHEMA=true              # 端点能力；否则置 false 并启用 json_object
LLM_SUPPORTS_JSON_OBJECT=true
```

改完 `docker compose up -d api` 重启即可。适配器对结构化作严格 JSON Schema 校验；不通过则降级为人工，
不产生写命令。`GET /ready` 会返回 `startup_violations`，非空表示被降级。

---

## 5. 记忆系统（短期跨轮 / 情景 / 语义，默认开启）

记忆是**非权威**上下文，只注入意图理解与措辞，**不进入政策/风控/金额/审批**；缺依赖/异常一律降级。

```
MEMORY_RECENT_TURNS=8          # 逐字保留的最近轮次
MEMORY_MAX_TURNS=12            # 单次装载历史上限
MEMORY_SUMMARY_TOKENS=1500     # 滚动摘要触发阈值/预算（需 LLM）
MEMORY_SUMMARY_ENABLED=true
MEMORY_EPISODIC_ENABLED=true   # 情景记忆（终态工单摘要 + BGE-M3 向量召回）
MEMORY_EPISODIC_TOP_K=3
MEMORY_EPISODIC_MIN_SCORE=0.35
MEMORY_EPISODIC_LLM_SUMMARY=true
MEMORY_SEMANTIC_ENABLED=true   # 语义记忆（用户画像，非权威）
```

- 迁移 `0020_memory.sql` 新建 `conversation_summaries / episodic_memories / user_profiles`（RLS 强制）并给
  `ticket_messages` 增加 `message_seq`（严格回放顺序）。
- 自检：`docker compose exec api python scripts/check_memory.py <ticket_id> tenant_demo u_demo`
  打印将回放的多轮历史、滚动摘要、情景/语义召回与注入片段。
- 原理与实测证据见 `docs/memory-design.md`。

---

## 6. 迁移机制与角色

- `scripts/migrate.py` 使用 asyncpg simple-query 执行整文件多语句（支持 `DO $$ ... $$`）。
- 迁移幂等：以 `schema_migrations` 记录已应用文件；重复 `up` 会输出 `skip ...`。
- 旧库（已存在 schema 但无迁移记录）首次运行会**自动 bootstrap** 记录，不做重复 DDL。
- 顺序：`0001 扩展 → 0002 基础表 → 0003 政策/RAG → 0004 资金/Saga → 0005 事件/审计 → 0006 组合FK → 0007 RLS → 0008 种子 → 0009 角色`。每条约束只定义一次（规避 v3.5 中重复 policy/FK 的问题）。
- 后续迁移：`0010 父子分块 → 0011 HNSW → 0012 Saga 计划绑定 → 0013 政策版本对齐 → 0014 工具审计 → 0015 裁决 trace → 0016 BM25 词法索引 → 0017 退款预占 → 0018 审批治理 → 0019 命令优先级 → 0020 记忆系统`。

角色（`0009_roles.sql`，LLD §19.2）：

| 角色 | 属性 | 用途 |
| --- | --- | --- |
| `agent_user`（迁移用） | 超级用户（镜像默认） | 仅执行 DDL/种子 |
| `aftersale_app` | NOSUPERUSER / NOBYPASSRLS | API 业务读写，RLS 生效 |
| `aftersale_worker` | NOSUPERUSER / BYPASSRLS | 跨租户领取命令/对账（系统事务） |

> **口令仅为本地基线**，生产必须改为密钥管理注入并轮换。
> 应用角色不是超级用户 → RLS 会真正生效（见 §7.4 实测）。

---

## 7. 验证清单（实测命令与输出）

### 7.1 单元/集成测试（本地，无 DB）

```bash
python -m pytest -q          # 34 passed
python scripts/smoke_e2e.py  # API_SMOKE_OK + REFUND_SAGA_OK
```

### 7.2 服务健康

```bash
curl -s localhost:8000/ready
# {"ready":true,"startup_violations":[],"llm_enabled":false,"finance_real_writes_enabled":false}
```

### 7.3 HTTP 工单流（无 LLM → 人工，零写命令）

```bash
curl -s -X POST localhost:8000/api/v1/tickets -H "X-Tenant-Id: tenant_demo" -H "X-User-Id: u_demo" \
  -H "Content-Type: application/json" \
  -d '{"order_id":"o_1001","intent":"UNKNOWN","initial_message":"你好"}'
# {"ticket_id":"...","status":"NEW","version":0}
```

### 7.4 RLS 强制生效（app 角色）

```bash
# 无租户上下文 -> 0 行
docker compose exec -T -e PGPASSWORD=app_pw_change_me postgres \
  psql -U aftersale_app -d aftersale -c "begin; select count(*) from aftersale_tickets; commit;"
# 带租户上下文 -> 可见行
docker compose exec -T -e PGPASSWORD=app_pw_change_me postgres \
  psql -U aftersale_app -d aftersale -c "begin; select set_config('app.tenant_id','tenant_demo',true); select count(*) from aftersale_tickets; commit;"
```

### 7.5 退款 Saga 全链路（DB + Worker + MCP）

`scripts/verify_db_saga.py` 用真实 Postgres（app/worker 角色）+ pg RAG + stub LLM 打通：
① 图运行 → 写 `saga_commands`；② Worker 领取并派发 → Finance 返回 SUCCEEDED；③ 命令回写 CONFIRMED。

```bash
python scripts/verify_db_saga.py
# graph: {'intent':'REFUND_ONLY','policy':'ALLOW','risk':'AUTO_APPROVE','status':'WAITING_EXTERNAL',
#         'evidence_status':'OK','evidence_items':2,'refund_request_id':'...'}
# claimed: {'action':'REQUEST_REFUND_ONLY','refund_request_id':'...','quote_id':'...','quote_hash':True}
# dispatch: {'mcp_result':'SUCCEEDED','command_status':'CONFIRMED','finance_status':'SUCCEEDED'}
# DB_SAGA_OK
```

容器内 Worker 实测日志：

```
worker-1 | httpx HTTP Request: POST http://mcp-mock:9100/finance/execute_refund "HTTP/1.1 200 OK"
worker-1 | command=... action=REQUEST_REFUND_ONLY -> CONFIRMED
worker-1 | httpx HTTP Request: POST http://mcp-mock:9100/finance/get_reconciliation_result "HTTP/1.1 200 OK"
worker-1 | reconcile task=... -> DONE
```

### 7.6 SSE（断线续传）

```bash
curl -N localhost:8000/api/v1/tickets/<ticket_id>/events -H "X-Tenant-Id: tenant_demo" -H "X-User-Id: u_demo" -H "Last-Event-ID: 0"
# id: 1
# event: message
# data: {"text":"您的申请已收到，正在核实。","run_status":"COMPLETED"}
```

---

## 8. 关键控制点（与 LLD 一致）

- **意图识别零工具调用**：`IntentResult` 固定 Schema（含 `SlotSources`），低于置信度阈值→UNKNOWN→澄清；安全旗标命中→拒绝/人工。
- **政策确定性**：Policy Engine 不调用 LLM；RAG 只提供引用与例外证据（`required_clause_ids` 精确闭包）。
- **金额与授权**：Finance 唯一签发 `quote_id/quote_hash/expires_at`；Risk 决定 AUTO_APPROVE/REVIEW；预执行再校验。
- **HITL**：`interrupt()` + `Command(resume=...)`（同一 `thread_id`）；恢复时**从 DB 读取审批结果**，不信任 resume payload。
- **对外等待**：事件驱动新执行片段；`WAITING_EXTERNAL` 不用 `Command(resume=...)`。
- **timeout != failure**：写超时记 `UNKNOWN` → 生成对账任务 → Worker 回查 Finance 权威状态（绝不盲重试）。

---

## 9. 运维

- 日志：`docker compose logs -f api worker`
- 重启：`docker compose restart api`（代码变更需 `docker compose build` 后 `up -d`）
- 扩缩容：**当前 HITL 检查点使用进程内 `InMemorySaver`，API 需单副本**；多副本/重启恢复需替换为
  `langgraph-checkpoint-postgres`（PostgresSaver）或 RedisSaver。Worker 可多副本（命令领取用 `FOR UPDATE SKIP LOCKED`）。
- 清理重来：`docker compose down -v`（会删除 pgdata 卷）。

---

## 10. 故障排查（本环境实测踩坑与修复）

| 现象 | 根因 | 修复 |
| --- | --- | --- |
| 构建期 pip 超时 | 直连 PyPI 受限 | `Dockerfile` 默认清华镜像 `PIP_INDEX_URL` |
| `cannot insert multiple commands into a prepared statement` | migrate 用 SQLAlchemy 预处理执行多语句 | 改用 asyncpg simple-query 整文件执行 |
| 重复 `up` 时 `ADD CONSTRAINT ... already exists` | 迁移非幂等 | `schema_migrations` 记录 + bootstrap |
| Worker 派发报 `Object of type UUID is not JSON serializable` | asyncpg 返回 `uuid.UUID`，httpx 无法序列化 | 领取 SQL 对 UUID 列 `::text` 投影 |
| `invalid input ... expected datetime ... got 'str'` | `timestamptz` 绑定 ISO 字符串 | 绑定前 `datetime.fromisoformat`（`_ts`） |
| Worker 领不到命令 | `saga_commands` 缺 `payload/quote_id/quote_hash/...` 列 | 补齐 DDL 与 `enqueue_command` 字段 |
| 9101/9102/9103 连不上 | mock 单进程只监听 9100 | 三个 URL 统一指向 `http://mcp-mock:9100` |

---

## 11. 已知边界 / 下一步

- RAG 的 Dense 需配置 OpenAI 兼容 embeddings 才生效；未配置时降级为词法检索（当前默认 `RAG_BACKEND=memory`，
  可切 `pg` 从 `policy_chunks` 读取）。BM25 建议接 `pg_search`(ParadeDB) 或独立服务。
- HITL 检查点需持久化后端以支持多副本（见 §9）。
- Outbox 发布/投递暂由 Inbox 去重承担；生产建议独立 Outbox Relay。
- 评测金标集、SLO 校准、许可证（如 `pg_search` AGPL）见 LLD v3.5 第 14/17 章。

---

## 12. 政策库（真实规则）：事实源、入库与验证

政策库有**两个事实源**，内容一致：
- `policy/rules.json`：规则集（16 条）与文档清单（8 篇）——规则的唯一来源；
- `docs/policy/*.md`：条款正文（标题形如 `## [CL-FRESH-A] 标题`，方括号内为 clause_id）。

代码侧：`app/policy/library.py` 在内存后端（`RAG_BACKEND=memory` / 无 DB）加载**同一套**规则与条款；
`app/policy/mdclauses.py` 提供切分逻辑（入库脚本与内存库共用，避免重复）。

### 12.1 查看现有数据

```bash
# 方式 A：脚本（推荐，同时列出 documents/rules/chunks）
python scripts/ingest_policy.py --list \
  --database-url postgresql+asyncpg://aftersale_app:app_pw_change_me@localhost:5432/aftersale

# 方式 B：psql
psql "postgresql://aftersale_app:app_pw_change_me@localhost:5432/aftersale" \
  -c "SELECT doc_id,title,version FROM policy_documents ORDER BY doc_id;" \
  -c "SELECT rule_id,decision,reason_code,required_clause_ids FROM policy_rules ORDER BY priority DESC;"
```

### 12.2 预览切分（不连库）

```bash
python scripts/ingest_policy.py --all --rules --dry-run
```

### 12.3 全量入库（规则 + 条款，幂等）

```bash
# 宿主机执行：主机名用 localhost
python scripts/ingest_policy.py --all --rules --replace \
  --database-url postgresql+asyncpg://aftersale_app:app_pw_change_me@localhost:5432/aftersale
# 容器内执行：主机名用 postgres
# python scripts/ingest_policy.py --all --rules --replace \
#   --database-url postgresql+asyncpg://aftersale_app:app_pw_change_me@postgres:5432/aftersale
```

- `--rules`：把 `policy/rules.json` 写入 `policy_rules`（`--replace` 先清空该租户规则）；
- `--all`：按清单把 8 篇文档写入 `policy_documents` / `policy_chunks`（`--replace` 先清该 doc+version）；
- `--embed`：配置 `EMBEDDING_*` 后回填 `policy_chunks.embedding`（维度自动对齐 `vector(N)`）。

入库后自检（缺条款引用应为空）：

```sql
SELECT r.rule_id, mc.cid AS missing_clause_id
FROM policy_rules r
CROSS JOIN LATERAL unnest(r.required_clause_ids) AS mc(cid)
LEFT JOIN policy_chunks c ON c.clause_id = mc.cid AND c.tenant_id = r.tenant_id
WHERE c.clause_id IS NULL AND r.tenant_id='tenant_demo';
```

### 12.4 验证规则（DB 规则 + DB 条款决策矩阵）

```bash
python scripts/verify_policy_rules.py
# 从 DB 载入规则 16 条（tenant=tenant_demo）
# [PASS] 生鲜破损-仅退款  ALLOW FRESH_DAMAGED_WITHIN_WINDOW  clauses=4/4 cap=20000
# ...
# 结果：18/18 通过
# POLICY_RULES_OK
```

### 12.5 让检索真正读库里的条款

```bash
# .env
RAG_BACKEND=pg
EMBEDDING_BASE_URL=https://api.openai.com/v1      # 可选，用于语义检索
EMBEDDING_API_KEY=sk-...
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIM=1024                                 # 与模型/表列一致；未配 embedder 时仅词法检索
```

```bash
docker compose up -d api
```

切换后 `policy_evidence`（原 `policy_retrieval` / `evidence_gate`）从 `policy_chunks` 取条款（词法 = ILIKE/ts_rank；
Dense = pgvector，需 embedding）。注意：**规则**仍由 `policy/rules.json`（`app/policy/library.py`）提供，
与库内 `policy_rules` 内容一致；如需完全由库驱动，可用
`app/policy/db_repository.py: DbRuleRepository.refresh(tenant_id)`（`scripts/verify_policy_rules.py` 已示范）。

> 政策内容为**参考口径**（含法定底线与平台实践），非法律意见；正式上线前请由法务/业务复核。
