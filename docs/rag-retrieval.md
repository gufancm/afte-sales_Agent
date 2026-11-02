# 检索增强（BM25 / 向量 / RRF / Reranker / 查询变换）

`policy_evidence` 节点（`app/rag/`）的检索链路按工业界标准实现。定位不变：**检索只负责"发现"**，
最终授权证据由 `policy_engine` 的 `required_clause_ids` 精确补齐 + 确定性校验（覆盖/版本/冲突/例外闭包）。

## 1. 链路总览

```
query
  │  ① 查询变换（LLM，可关，失败自动降级）
  │     ├─ 多查询改写：N 条同义/补全表述（rag-multi-query-v1）
  │     └─ HyDE：生成"假设政策条款原文"（rag-hyde-v1）
  ▼
每个变体 × { BM25 词法召回 , pgvector Dense 召回 }     （多路）
  │  ② RRF 融合（Reciprocal Rank Fusion, k=60）
  ▼
top-N 候选
  │  ③ BGE Reranker（cross-encoder）精排
  ▼
top-K 候选 -> EvidenceService（发现 + 精确补齐 + 确定性校验）
```

## 2. 组件与实现

| 环节 | 实现 | 说明 |
| --- | --- | --- |
| 分词 | `app/rag/tokenize.py` | jieba `cut_for_search`（英文小写、去停用词）；**无 jieba 时退化为英文按词 + 中文二元组** |
| BM25 词法 | `app/rag/pg.py::PgBM25` | **真实 BM25**：`idf = ln(1+(N-df+0.5)/(df+0.5))`，tf 饱和 + 文档长度归一化（k1=1.2, b=0.75）。索引列 `policy_chunks.tokens`（`{term:tf}`）+ `doc_len`，`GIN(jsonb_path_ops)` 加速。 |
| 向量 Dense | `app/rag/pg.py::PgDense` | pgvector 近邻（`embedding <=> query`），BGE-M3 1024 维（见 `docs/embedding-bge-m3-deploy.md`） |
| 融合 | `app/rag/retrieval.py::rrf_fuse` | 多路（多变体 × {BM25,Dense}）统一 RRF |
| 重排 | `app/rag/pg.py::HttpReranker` + `scripts/serve_reranker.py` | BGE Reranker（cross-encoder）HTTP 服务，`/rerank`；**失败降级为原序** |
| 查询变换 | `app/rag/query_transform.py` | LLM 多查询 + HyDE；**默认 adaptive**（首轮低置信才触发）；TTL/LRU 缓存；输出清洗；超时/失败降级 |
| 内存后端 | `app/rag/memory.py` | 与 PgBM25 同一公式的内存 BM25 + 空 Dense（测试/无 DB），保证行为一致 |

## 3. 配置（`.env`）

```
RAG_BM25_K1=1.2
RAG_BM25_B=0.75
RAG_TOKENIZER=jieba
RAG_RERANK_ENABLED=true
RERANK_BASE_URL=http://rerank:7998
RERANK_MODEL=BAAI/bge-reranker-v2-m3
RERANK_API_KEY=local-dev-key
RERANK_TIMEOUT_MS=60000

# 查询变换：off / always / adaptive（生产默认 adaptive）
RAG_TRANSFORM_POLICY=adaptive
RAG_QUERY_REWRITE_ENABLED=true
RAG_MULTI_QUERY_N=3
RAG_HYDE_ENABLED=true

# adaptive 门控：硬信号单条即触发；软信号需同时满足 SOFT_SIGNALS_REQUIRED 条
RAG_ADAPTIVE_MIN_DENSE_SIM=0.60
RAG_ADAPTIVE_MIN_MARGIN=0.015
RAG_ADAPTIVE_SHORT_QUERY_CHARS=8
RAG_ADAPTIVE_MIN_CANDIDATES=5
RAG_ADAPTIVE_SOFT_SIGNALS_REQUIRED=2
RAG_ADAPTIVE_CONTEXT_CHARS=240
RAG_ADAPTIVE_CONTEXT_MESSAGES=4
RAG_ADAPTIVE_USE_REQUIRED_COVERAGE=true
RAG_ADAPTIVE_MIN_REQUIRED_COVERAGE=0.5

# 同一 query 只付一次改写费用；超时/失败降级原始 query
RAG_TRANSFORM_CACHE_TTL_S=900
RAG_TRANSFORM_CACHE_MAX=512
RAG_TRANSFORM_TIMEOUT_MS=2500
RAG_TRANSFORM_MAX_VARIANT_CHARS=300
RAG_TRANSFORM_DAILY_CALL_BUDGET=300

RAG_REQUIRE_EMBEDDINGS=true
RAG_ALLOW_LEXICAL_ONLY=false
```

## 3.1 查询改写 / HyDE 的触发条件（adaptive）

业界并不建议“每次检索都改写”。生产检索先用一次便宜的 BM25 + Dense 拿到首轮结果，
再按**置信度/覆盖率**决定是否花 LLM 的钱。本项目把触发信号分成两档：

**硬信号（单条成立即触发，说明首轮确实不可信）**

| 信号 | 判定 | 原因 |
| --- | --- | --- |
| `no_candidates` | 首轮 BM25+Dense 都没有候选 | 典型词汇鸿沟/零结果 |
| `few_candidates:<n>` | 首轮 RRF 候选 < `RAG_ADAPTIVE_MIN_CANDIDATES` | 召回不足 |
| `low_dense_sim:<x>` | Dense Top-1 余弦相似度 < `RAG_ADAPTIVE_MIN_DENSE_SIM` | 语义置信度低 |
| `required_coverage:<x>` | `required_clause_ids` 在首轮发现中覆盖率 < `RAG_ADAPTIVE_MIN_REQUIRED_COVERAGE` | 规则必备条款未被“发现” |

**软信号（需同时满足 `RAG_ADAPTIVE_SOFT_SIGNALS_REQUIRED` 条，默认 2 条）**

| 信号 | 判定 |
| --- | --- |
| `short_query:<n>` | query 长度 ≤ `RAG_ADAPTIVE_SHORT_QUERY_CHARS`（默认 8）且不含明确规则词 |
| `low_margin:<x>` | Top1-Top2 间隔 < `RAG_ADAPTIVE_MIN_MARGIN` 且不含明确规则词 |
| `deictic_reference` | 含“这个/那个/它/刚才/继续/还有”等指代，**且存在可用会话上下文** |

**明确不触发**

- query 含订单号/运单号/SKU/条款号等精确标识符（`exact_identifier`）；
- 首轮 Top-1 相似度高、Top1-Top2 间隔足够（`high_confidence`）；
- 已含明确规则词（“七天无理由 / 运费险 / 闪电退款 / 价保 / 预售 / 生鲜 / 图书 / 服饰 / 退货 / 退款 …”）；
- 含指代但**没有会话上下文**（`deictic_without_context`）：改写无从解析指代对象，宁可跳过；
- `RAG_TRANSFORM_POLICY=off`。

**指代的零成本路径**

指代类问题不必先花钱：`policy_evidence` 把最近 `RAG_ADAPTIVE_CONTEXT_MESSAGES`（默认 4）条
用户话术作为上下文锚点传给检索层（去订单号/运单号等标识符），检索层用 `query + 锚点`
做一次扩展检索（`_expand_with_context`）。这类触发只要**没有硬信号**，就不调用 LLM
（诊断 `query_transform.llm_used=false`）。

> 注意：在本系统中，`policy_engine` 的 `required_clause_ids` 是确定性产物，证据由
> `EvidenceService` 精确补齐。因此查询改写只影响**发现诊断**，不改变政策裁决。
> 若目标是“零成本零延迟”，直接设 `RAG_TRANSFORM_POLICY=off` 即可，业务正确性不受影响。

**阈值标定（2026-10-27，E8 118 条 query，仅首轮检索、零 LLM 调用）**

命令：`docker compose run --rm api python scripts/calibrate_transform_gate.py`

- Dense Top-1 余弦相似度：P05=0.649、P50=0.731、P95=0.810；
- Top1-Top2 间隔：P10=0.0056、P50=0.032、P90=0.103；
- 收紧前（软信号单条即触发、短查询阈值 12）：触发 **12/118 = 10.2%**，全部由 `short_query`/`low_margin` 造成；
- 收紧后（软信号需 2 条、短查询阈值 8、指代需上下文）：触发 **2/118 = 1.69%**，
  两条均因 `required_coverage`（必备条款没被首轮发现）——这才是真正值得花钱的情形；
- 同一份金标集上，改写调用从「10 次里 1 次」降到「100 次里不到 2 次」。
### 3.2 成本与延迟控制

- 首轮永远先跑，只有低置信才触发；触发后最多 1 次多查询 + 1 次 HyDE。
- 硬信号才花钱；纯指代软信号 + 有上下文时只做零成本扩展（`llm_used=false`）。
- 进程级日预算 `RAG_TRANSFORM_DAILY_CALL_BUDGET`（默认 300，0=不限）：超预算后门控直接跳过改写，
  诊断记 `budget_exhausted`，避免突发流量放大成本。
- 同一 query 在 `RAG_TRANSFORM_CACHE_TTL_S` 内命中缓存，不重复计费；缓存命中时 `llm_calls=[]`。
- `RAG_TRANSFORM_TIMEOUT_MS` 到点即取消并降级原始 query，不让 RAG 拖垮主流程。
- 变体经确定性清洗：超长、重复、含“忽略之前指令/跳过审批/API Key”等注入片段的变体直接丢弃。
- 诊断字段：`transform_policy / transform_triggered / transform_reasons / query_transform.cache_hit /
  query_transform.effective / first_pass / post_transform / llm_calls`。

## 4. "Dense 不再静默退化"

旧行为：`RAG_BACKEND=pg` 未配置 embedding 时 Dense 返回空列表（静默降级）。
新行为（工业界标准）：
- **启动期快速失败**：`RAG_BACKEND=pg` 且 `EMBEDDING_MODEL` 为空（且未显式 `RAG_ALLOW_LEXICAL_ONLY=true`）→ 抛错拒启。
- **可观测**：EVIDENCE_BUILT 诊断带 `retrieval.dense_enabled`（是否启用向量）与各召回命中数。

## 5. 部署

```bash
# 1) 下载 BGE Reranker 权重（hf-mirror；Xet 被墙时禁用 HF_HUB_DISABLE_XET=1）
docker run --rm -v E:/agent/models:/models -e HF_HUB_OFFLINE=0 -e HF_HUB_DISABLE_XET=1 \
  -e HF_ENDPOINT=https://hf-mirror.com aftersale-agent-embed \
  python -c "from huggingface_hub import snapshot_download as d; d(repo_id='BAAI/bge-reranker-v2-m3', local_dir='/models/bge-reranker-v2-m3', ignore_patterns=['onnx/*','*.onnx','*.h5','*.msgpack','*.ot','imgs/*'])"

# 2) 启动（compose 含 rerank 服务；api/worker 已装 jieba）
docker compose up -d

# 3) 回填/重建 BM25 索引（新入库会自动写入，无需执行；老数据或换分词器后执行）
docker compose run --rm api python scripts/rebuild_lexical_index.py --tenant tenant_demo

# 4) 检索自检
docker compose run --rm api python scripts/check_retrieval.py "衣服有质量问题我要退货退款"
```

## 6. 实测证据（容器实测，2026-10-25；当时为 always 模式）

- 单元/回归：容器内 `pytest -q` → `101 passed`。
- BM25 SQL（psql 直测，terms=退货/退款）：Top 命中为退货退款相关条款（`CL-JD2595-S9` 4.68、`CL-JD2595-CH2-2#4` 4.65 …）。
- Reranker 健康：`/health` ok；示例 `退货退款政策` vs `[签收7日内退货退款, 24小时内发货]` → 0.834 / 0.017（区分度明显）。
- 端到端工单 `125ab664-8cdc-4f38-9d69-61a7d2b26626`（RETURN_REFUND/APPAREL）：
  - `EVIDENCE_BUILT.status=OK`；诊断 `lexical=pg-bm25, reranker=http, dense_enabled=true, variant_count=5, bm25_hits=100, dense_hits=100, fused=15`。
  - `llm_calls`：`intent_guardrail`(intent-v3.7) / **`policy_retrieval`(rag-multi-query-v1, rag-hyde-v1)** / `action_planner`(planner-v3.6)。
- `check_retrieval.py "衣服有质量问题我要退货退款"`：当时 always 模式下变体 5 条（原文 + 3 改写 + 1 HyDE），重排后 Top-5 均为质量/退货退款相关条款。当前默认 adaptive，只有低置信 query 才会产生这 5 条变体。

## 7. 评测（E8）

`tests/eval/run_retrieval.py` 支持三档查询变换策略（只影响 `full` 配置）：

```bash
# 零成本基线
docker compose run --rm api python tests/eval/run_retrieval.py --transform-policy off

# 对照组：每次必改写（有 API 成本，仅评测用）
docker compose run --rm api python tests/eval/run_retrieval.py --transform-policy always

# 生产候选：自适应
docker compose run --rm api python tests/eval/run_retrieval.py --transform-policy adaptive
```

报告会同时给出 `nDCG@5 / recall@5` 与 **触发率、LLM 调用次数、缓存命中、full 平均耗时**，
用于验证「adaptive 用更少调用拿到接近 always 的收益」。

## 8. 未做（明确暂缓）

- 检索评测的真实 `full/adaptive` 数字尚待复跑（脚本与金标已就绪）。
- 可选后续：加权 RRF（dense 权重 > bm25）、reranker 批量并发、HyDE 与多查询并行化。