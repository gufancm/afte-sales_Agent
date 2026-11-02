# 本地 BGE-M3 向量服务：部署与运维记录

> 本文记录 embedding 落地的实际状态、命令与取值来源，便于复现与交接。

## 1. 选型结论

| 项 | 取值 | 说明 |
|---|---|---|
| Embedding 模型 | `BAAI/bge-m3` | 1024 维、最长 8192 token、多语种；本地部署，数据不出境 |
| 服务形态 | OpenAI 兼容 `/v1/embeddings` | 由 `scripts/serve_embedding.py` 提供，可跑在宿主机或容器 |
| 权重目录 | `E:\agent\models\bge-m3`（约 2.19 GB） | 只读挂载进容器，运行期不联网 |
| 索引 | `policy_chunks.embedding` + HNSW(cosine) | 见 `migrations/sql/0011_embedding_hnsw.sql` |
| Reranker（后续） | `BAAI/bge-reranker-v2-m3` | 同一 BGE 生态，尚未部署 |

**BGE-M3 是开源权重，不需要 API Key**。`.env` 里的 `EMBEDDING_API_KEY=local-dev-key` 只是本地占位，
服务端用 `--api-key` 校验同一个值，避免裸奔。

## 2. 权重从哪里来

国内直连 `huggingface.co` 不通，走镜像：

- 模型卡（官方）：https://huggingface.co/BAAI/bge-m3
- 国内镜像（本机实际使用）：https://hf-mirror.com/BAAI/bge-m3
- ModelScope 备选：https://modelscope.cn/models/BAAI/bge-m3

下载命令（`HF_ENDPOINT` 自动切镜像）：

```powershell
$env:HF_ENDPOINT="https://hf-mirror.com"
$env:HF_HUB_DISABLE_XET="1"   # 必须：镜像站不支持 Xet，否则报 401 Unauthorized
& "E:\agent\.venv-ingest\Scripts\python.exe" scripts\download_embedding_model.py `
    --repo BAAI/bge-m3 --dest "E:\agent\models\bge-m3"
```

下载 `BAAI/bge-reranker-v2-m3` 时把 `--repo/--dest` 换掉即可。

## 3. 服务如何运行

### 3.1 容器方式（推荐，`docker compose`）

```powershell
docker compose build embed
docker compose up -d embed
```

- 服务名 `embed`，端口 `7997`，健康检查 `/health`
- `Dockerfile.embed`：`python:3.12-slim` + CPU 版 torch（约 196 MB）
- 权重通过 `BGE_M3_HOST_DIR=E:/agent/models/bge-m3` 只读挂载到 `/models/bge-m3`，
  容器内 `HF_HUB_OFFLINE=1`，不访问外网

### 3.2 宿主机方式（调试用）

```powershell
& "E:\agent\.venv-ingest\Scripts\python.exe" scripts\serve_embedding.py `
    --model-path "E:\agent\models\bge-m3" --host 127.0.0.1 --port 7997 --device cpu
```

## 4. 配置项（`.env`）

| 变量 | 当前值 | 说明 |
|---|---|---|
| `EMBEDDING_BASE_URL` | `http://embed:7997/v1` | 容器内地址；宿主机脚本改用 `http://localhost:7997/v1` |
| `EMBEDDING_API_KEY` | `local-dev-key` | 本地占位值，需与服务端 `--api-key` 一致 |
| `EMBEDDING_MODEL` | `BAAI/bge-m3` | 同时写入 `policy_chunks.embedding_model`，检索必须带同一值 |
| `EMBEDDING_DIM` | `1024` | 与 BGE-M3 输出维度一致 |
| `BGE_M3_HOST_DIR` | `E:/agent/models/bge-m3` | 宿主机权重目录 |
| `RAG_BACKEND` | `pg` | 切到 pg 后才真正走 DB + 向量检索 |

## 5. 常用命令

```powershell
# 健康检查
Invoke-RestMethod http://localhost:7997/health

# 看还有多少 chunk 没向量
python scripts/embed_policy_chunks.py --dry-run `
  --database-url postgresql://aftersale_app:app_pw_change_me@localhost:5432/aftersale

# 回填向量（宿主机跑，地址要指向本机）
$env:EMBEDDING_BASE_URL="http://localhost:7997/v1"
python scripts/embed_policy_chunks.py --tenant tenant_demo `
  --database-url postgresql://aftersale_app:app_pw_change_me@localhost:5432/aftersale

# 向量检索冒烟
python scripts/check_dense_retrieval.py --query "七天无理由退货的运费由谁承担" `
  --database-url postgresql://aftersale_app:app_pw_change_me@localhost:5432/aftersale

# 库内统计
docker exec aftersale-agent-postgres-1 psql -U aftersale_worker -d aftersale -t -A -c `
  "SELECT count(*) total, count(embedding) embedded, min(embedding_model) FROM policy_chunks;"
```

## 6. 换模型怎么做

- 换 OpenAI 官方：`EMBEDDING_BASE_URL=https://api.openai.com/v1`、
  `EMBEDDING_MODEL=text-embedding-3-small`、`EMBEDDING_DIM=1536`；
  回填脚本会自动 `ALTER COLUMN embedding TYPE vector(1536)`。
- 换其他本地模型：把 `--model-path` 指向新权重目录即可，维度变化由脚本自动处理。

**注意（LLD §5.7）**：换模型后必须新建索引/分区，并按 `embedding_model + revision` 隔离，
不能把不同模型的向量混在同一索引里；切换期间双读比对后再切。

## 7. 踩过的坑

| 现象 | 原因 | 处理 |
|---|---|---|
| `401 Unauthorized ... cas-server.xethub.hf.co` | 镜像站不支持 Xet 传输 | `HF_HUB_DISABLE_XET=1` |
| `ConnectTimeoutError files.pythonhosted.org` | 容器内装依赖被墙 | 用清华源：`--index-url .../tuna.tsinghua.edu.cn/simple`，torch 主源仍用 `download.pytorch.org/whl/cpu` |
| 拉到 1 GB+ 的 CUDA 版 torch | 把 PyPI 当主源、pytorch 当附加源 | 顺序不能反：`--index-url TORCH_INDEX --extra-index-url PIP_INDEX` |
| `must be owner of table policy_chunks` | 应用角色无 DDL 权限 | 索引由 `migrations/sql/0011` 以 owner 身份建立 |
| 宿主机脚本连不上库 | `.env` 里主机名是 `postgres` | 宿主机跑脚本时 `--database-url` 用 `localhost` |

## 8. 后续要接入 LLM 时，Key 从哪里拿

`.env` 里 `LLM_BASE_URL / LLM_API_KEY / LLM_MODEL` 留空即为不启用 LLM。常见获取地址：

- OpenAI：https://platform.openai.com/api-keys
- DeepSeek：https://platform.deepseek.com/api_keys
- 阿里百炼（通义）：https://bailian.console.aliyun.com/
- 硅基流动（多家开源模型聚合）：https://cloud.siliconflow.cn/account/ak
- 智谱 GLM：https://open.bigmodel.cn/
- Moonshot Kimi：https://platform.moonshot.cn/console/api-keys
- 火山方舟（豆包）：https://console.volcengine.com/ark

填好后必须兼容 OpenAI 的 `/v1/chat/completions`；若走兼容网关，`LLM_BASE_URL` 指向其 `/v1` 即可。