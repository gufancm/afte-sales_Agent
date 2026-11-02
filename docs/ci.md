# CI 与覆盖率（GitHub Actions）

> 工作流：`.github/workflows/ci.yml`；覆盖率徽章：`docs/coverage.svg`（由 `scripts/coverage_badge.py` 生成）。

## 1. 触发条件

- `push` 到 `main` / `master` / `develop`
- 任意 `pull_request`
- 手动 `workflow_dispatch`
- 同一 ref 的新运行会取消旧运行（`concurrency`）

## 2. 四个 Job

| Job | 目的 | 关键步骤 |
| --- | --- | --- |
| `test` | 单元/集成测试 + 覆盖率门槛（无 DB、无 LLM，Fail-Closed） | `pip install -r requirements.txt` → `pytest --cov=app ... --cov-fail-under=70` → 生成徽章 → 写 Job Summary → 上传产物 |
| `eval-offline` | 离线评测集回归（E6 政策矩阵 / E9 证据闭包 / E1 意图 / E28 注入对抗） | `python tests/eval/run_all.py`，无 DB、无 LLM，门槛不达标即失败 |
| `docker-build` | 验证 Dockerfile / compose / requirements 一致 | `cp .env.example .env` → `docker compose build api worker migrate mcp-mock` |
| `docker-e2e` | 真实 Postgres + Worker + MCP Mock 端到端 | `.env` 关闭 LLM 与外部向量依赖 → `up -d postgres redis mcp-mock worker` → `verify_db_saga.py` → HTTP 冒烟（`/health` `/ready` `/metrics` + 建单/发消息/查单）→ 失败输出日志 → `down -v` |

设计取舍：
- **不启动 `embed` / `rerank`**（需下载 BGE 权重，CI 不友好）：`docker-e2e` 用 `RAG_BACKEND=memory` + `RERANK_ENABLED=false`，
  只验证持久化、Saga、Worker、MCP 与 HTTP 入口；向量链路在本地/预发验证。
- **不使用真实 LLM**：`LLM_MODEL=` 为空时进入只读/人工路径（不产生任何写命令），保证确定性与零成本。
- `test` 与 `docker-e2e` 的覆盖率**不合并**（合并需 `coverage combine` + 多来源数据）；当前 `test` 为唯一门槛来源。

## 2.1 发布到 GitHub

仓库已发布：`https://github.com/gufancm/aftersale-agent`（`main` 分支，Actions 四 Job 全绿）。
换机器或重建仓库时，首次推送按三步执行（`.env` 被忽略，`coverage.xml/json` 不入库）：

```powershell
# 1) 登录（浏览器授权，只需一次）
gh auth login --hostname github.com --git-protocol https --web

# 2) 创建仓库并推送（private/public 按需选择）
gh repo create aftersale-agent --public --source=. --remote=origin --push

# 3) 看首次 CI 结果
gh run watch
```

注意：本机仓库目录的属主是 `CodexSandboxOffline`，与登录用户 `wjy` 不同，git 会报 `dubious ownership`。
已执行 `git config --global --add safe.directory E:/agent/aftersale-agent`；换机器/换路径时需要重新执行一次。

## 3. 本地复现

```bash
# 1) 单元测试 + 覆盖率（阈值 70%）
pip install -r requirements.txt
pytest -q --cov=app --cov-report=term-missing:skip-covered \
  --cov-report=xml:coverage.xml --cov-report=json:coverage.json --cov-fail-under=70
python scripts/coverage_badge.py docs/coverage.svg coverage.xml

# 2) 镜像可构建
cp .env.example .env && docker compose build api worker migrate mcp-mock

# 3) DB 端到端（本机 Windows/PowerShell 等价写法见 README 的"验证"小节）
docker compose up -d postgres redis mcp-mock worker
docker compose run --rm \
  -e MCP_MOCK_URL=http://mcp-mock:9100 \
  -e POLICY_APP_DSN=postgresql+asyncpg://aftersale_app:app_pw_change_me@postgres:5432/aftersale \
  -e POLICY_WORKER_DSN=postgresql+asyncpg://aftersale_worker:worker_pw_change_me@postgres:5432/aftersale \
  api python scripts/verify_db_saga.py          # 期望最后一行 DB_SAGA_OK
```

## 4. 覆盖率现状与口径

- `source = ["app"]`；`pyproject.toml` 的 `[tool.coverage.*]` 排除 `pragma: no cover`、`if TYPE_CHECKING:`、`raise NotImplementedError`。
- 当前实测：**314 passed，语句覆盖 75.6%（4798/6346）**，门槛 `--cov-fail-under=70`（数字随每次运行更新，徽章为唯一权威）。
- 新增模块自带测试：`app/mcp/stdio_server.py`（含子进程 stdio 握手）、`app/graph/checkpointer.py`（含模拟重启恢复）、
  `app/graph/retry.py`（重试白名单与真实重试行为）、`app/rag/table_merge.py`（跨页合并与 Markdown 回写）、
  `app/mcp/client.py`（连接级重试：DNS/拒绝连接重试，读超时不重试）。
- 单元测试覆盖薄弱/为零的主要是**只能端到端验证**的模块（由 `docker-e2e` 覆盖，但不计入本门槛）：

  | 模块 | 语句 | 说明 |
  | --- | --- | --- |
  | `app/db/persistence.py` | 267 | SqlPersistence（需真实 Postgres） |
  | `app/rag/pg.py` | 116 | pgvector/BM25 库内检索 |
  | `app/mcp/client.py` | 82 | HTTP 客户端（指向 MCP Mock；连接级重试有单测） |
  | `app/main.py` | 62 | FastAPI 入口/中间件 |
  | `app/saga/commands.py` | 33 | 命令行构造 |
  | `app/policy/db_repository.py` | 33 | DB 规则仓库 |
  | `app/db/engine.py` | 22 | 引擎/事务封装 |
  | `app/api/routes.py` | 154 | 28.6%（HTTP 入口，`docker-e2e` 冒烟覆盖） |

- 提高覆盖率的最省力方向（后续）：给 `saga/commands.py`（纯函数）与 `policy/mdclauses.py`(条款映射) 补单测；
  给 `api/routes.py` 用 `httpx.ASGITransport` 做进程内 API 测试（无需容器），可同时覆盖 `main.py`。

## 5. 徽章

- **覆盖率徽章**（已生效、无需外部服务）：CI 每次运行重新生成 `docs/coverage.svg`，README 引用该文件。
- **CI 状态徽章**（已生效）：README 顶部已引用
  `https://github.com/gufancm/aftersale-agent/actions/workflows/ci.yml/badge.svg`。

- 如需 Codecov/Coveralls 之类的服务徽章，可在 `test` job 增加上传步骤（当前未接入，保持零外部依赖）。

## 6. 备注

- 仓库已推送至 GitHub；最近一次 `main` 运行四个 Job 全部通过（`test` / `eval-offline` / `docker-build` / `docker-e2e`）。
- `.gitignore` 已忽略 `.env` / `.coverage` / `coverage.xml` / `coverage.json`，但 `docs/coverage.svg` 需要提交（徽章）。
- 本地运行覆盖率需 `pytest-cov`（已加入 `requirements.txt`）；离线环境可用镜像源：
  `pip install -i https://pypi.tuna.tsinghua.edu.cn/simple pytest-cov`。
