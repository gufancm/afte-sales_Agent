"""全局配置。所有模型/端点/阈值均来自环境变量，代码不写死（LLD §2.1/§2.2）。"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ---- 应用 ----
    app_env: str = "development"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    log_level: str = "INFO"

    # ---- 数据库 ----
    database_url: str = "postgresql+asyncpg://agent_user:change-me@localhost:5432/aftersale"
    db_pool_min: int = 5
    db_pool_max: int = 30
    db_statement_timeout_ms: int = 5000
    db_lock_timeout_ms: int = 3000
    # Worker 使用独立角色连接（可含 BYPASSRLS，用于跨租户领取命令/对账）；留空则复用 database_url
    worker_database_url: str = ""

    # ---- LangGraph Checkpointer（HITL 暂停点持久化，§18.3）----
    # memory   = 进程内 InMemorySaver（单进程/测试）
    # postgres = PostgresSaver（跨重启、多副本可恢复；需 psycopg + 同步 DSN）
    checkpoint_backend: Literal["memory", "postgres"] = "memory"
    # 留空 -> 由 database_url 派生（去掉 +asyncpg 驱动前缀）
    checkpoint_database_url: str = ""
    # True: Checkpointer 不可用即拒绝启动；False: 降级 InMemorySaver 并告警
    checkpoint_strict: bool = False
    # 启动时自动建表（本地/首次部署）；生产可交由迁移任务
    checkpoint_setup_on_start: bool = True
    checkpoint_connect_timeout_s: float = 5.0
    checkpoint_setup_timeout_s: float = 15.0

    # ---- Redis ----
    redis_url: str = "redis://localhost:6379/0"

    # ---- Auth ----
    jwt_secret: str = "change-me-in-secrets"
    jwt_alg: str = "HS256"
    jwt_issuer: str = "aftersale-agent"

    # ---- LLM ----
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = "replace-me"
    llm_model: str = ""
    llm_api_style: Literal["chat_completions", "responses"] = "chat_completions"
    llm_context_window: int = 32768
    llm_max_output_tokens: int = 4096
    llm_supports_json_schema: bool = True
    llm_supports_json_object: bool = True
    llm_supports_streaming: bool = True
    llm_supports_seed: bool = False
    llm_timeout_ms: int = 60000
    llm_connect_timeout_ms: int = 5000
    llm_max_retries: int = 1
    llm_temperature: float = 0.0
    llm_top_p: float = 1.0
    # 透传给 OpenAI 兼容端点的额外请求体（JSON 字符串），例如百炼关思考：{"enable_thinking": false}
    llm_extra_body: str = ""
    llm_safety_margin_tokens: int = 2048
    llm_intent_confidence_threshold: float = 0.6
    # 意图识别工程化：规则快路径 + 两级模型路由（简单问题小模型 / 复杂问题大模型）
    llm_intent_fastpath_enabled: bool = True
    llm_model_small: str = ""
    llm_small_base_url: str = ""
    llm_small_api_key: str = ""
    llm_small_max_output_tokens: int = 0
    llm_small_context_window: int = 0
    llm_intent_small_model_enabled: bool = True
    llm_intent_complexity_threshold: int = 2
    # 只读答复：事实模板 + LLM 仅润色（可关闭 -> 纯模板）
    readonly_llm_rephrase_enabled: bool = True
    # ActionPlanner：确定性快路径（意图明确+单动作+ALLOW+槽位齐 时跳过 LLM）
    planner_fastpath_enabled: bool = True
    # 计划审查：确定性 critic 始终执行；LLM 复核默认关闭（避免每次规划 +1 次调用）
    planner_llm_critic_enabled: bool = False
    # 有界重规划：critic 发现可自行修正的问题时，最多回 planner 重规划次数
    planner_max_replans: int = 1

    # ---- 审批（HITL）治理 ----
    # 允许审批的角色（逗号分隔，小写）；留空 = 不限制角色（本地联调）
    approval_allowed_roles: str = ""
    # ---- Worker 租约回收：命令租约过期后回收为 UNKNOWN 的最大尝试次数 ----
    worker_command_max_attempts: int = 3
    # ---- 风控维度（频次 / 累计 / 名单）----
    risk_max_refund_count_30d: int = 5
    risk_max_refund_amount_30d_minor: int = 500000
    risk_blacklist_deny: bool = True

    # ---- RAG ----
    rag_bm25_top_k: int = 20
    rag_dense_top_k: int = 20
    rag_rrf_k: int = 60
    rag_rerank_candidates: int = 15
    rag_final_top_k: int = 5
    rag_backend: Literal["memory", "pg"] = "memory"
    # 词法检索：真实 BM25（jieba 分词 + idf/tf 饱和/长度归一化；k1/b 为标准默认）
    rag_bm25_k1: float = 1.2
    rag_bm25_b: float = 0.75
    rag_tokenizer: str = "jieba"
    # 重排：BGE Reranker（cross-encoder）；未配置端点时为空操作
    rag_rerank_enabled: bool = True
    rerank_base_url: str = ""
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_api_key: str = ""
    rerank_timeout_ms: int = 60000
    # 查询变换：多查询改写 + HyDE（LLM；失败自动降级为单查询）
    # off      = 永不触发（推荐：政策证据由 required_clause_ids 精确补齐时，零成本零延迟）
    # always   = 每次触发（仅用于离线评测对照，不要用于生产）
    # adaptive = 首轮 BM25+Dense 低置信/覆盖不足时才触发（生产默认）
    rag_transform_policy: Literal["off", "always", "adaptive"] = "adaptive"
    rag_query_rewrite_enabled: bool = True
    rag_multi_query_n: int = 3
    rag_hyde_enabled: bool = True
    # adaptive 触发阈值
    # 硬信号（单条成立即触发）：无候选 / 候选不足 / Dense Top1 过低 / 必备条款覆盖不足
    # 软信号（需同时满足 rag_adaptive_soft_signals_required 条）：短查询 / 间隔小 / 含指代
    rag_adaptive_min_dense_sim: float = 0.60
    rag_adaptive_min_margin: float = 0.015
    rag_adaptive_short_query_chars: int = 8
    rag_adaptive_min_candidates: int = 5
    rag_adaptive_soft_signals_required: int = 2
    # 指代消解：从会话历史提取锚点做零成本扩展（无历史则不触发，改写无从解析指代）
    rag_adaptive_context_chars: int = 240
    rag_adaptive_context_messages: int = 4
    # 必备条款未被“发现”覆盖时触发（仅影响发现诊断，不影响授权证据）
    rag_adaptive_use_required_coverage: bool = True
    rag_adaptive_min_required_coverage: float = 0.5
    # 同一 query 的改写结果缓存 / 总超时（超时即降级原始 query）
    rag_transform_cache_ttl_s: int = 900
    rag_transform_cache_max: int = 512
    rag_transform_timeout_ms: int = 2500
    # 进程级日预算（0=不限）：超预算后门控直接跳过改写，避免突发流量放大成本
    rag_transform_daily_call_budget: int = 300
    rag_transform_max_variant_chars: int = 300
    # 检索必须配置 embedding：缺失即快速失败（工业界：Dense 不得静默退化为空）
    rag_require_embeddings: bool = True
    rag_allow_lexical_only: bool = False

    # ---- Embeddings（OpenAI 兼容；用于 policy_chunks.embedding 与 Dense 检索）----
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_dim: int = 1024

    # ---- Memory ----
    memory_recent_turns: int = 8
    memory_max_turns: int = 12
    memory_history_tokens: int = 6000
    memory_summary_tokens: int = 1500
    memory_input_soft_tokens: int = 24000
    # 短期跨轮：滚动摘要（需 LLM；关闭则仅按窗口截断，不生成摘要）
    memory_summary_enabled: bool = True
    # 情景记忆（历史工单摘要 -> 向量召回）；非权威，仅注入理解/措辞
    memory_episodic_enabled: bool = True
    memory_episodic_top_k: int = 3
    memory_episodic_min_score: float = 0.35
    memory_episodic_llm_summary: bool = True
    # 语义记忆（用户画像，非权威，可选）
    memory_semantic_enabled: bool = True

    # ---- Risk ----
    max_requote_cycles: int = 1

    # ---- Worker ----
    worker_poll_interval_ms: int = 500
    outbox_poll_interval_ms: int = 500

    # ---- 图节点级重试（LangGraph RetryPolicy）----
    # 仅对瞬时故障（超时/连接重置/上游 5xx）生效；资金写节点一律不配置重试。
    graph_node_retry_enabled: bool = True
    graph_node_retry_max_attempts: int = 3
    graph_node_retry_initial_interval_s: float = 0.2
    graph_node_retry_backoff_factor: float = 2.0
    graph_node_retry_max_interval_s: float = 3.0

    # ---- MCP 端点 ----
    oms_mcp_url: str = "http://localhost:9101"
    tms_mcp_url: str = "http://localhost:9102"
    finance_mcp_url: str = "http://localhost:9103"

    # ---- MCP 工具网关（只读调用的超时/重试/熔断/限流/并发，LLD §2.7）----
    mcp_tool_timeout_ms: int = 10000
    mcp_tool_max_retries: int = 2
    mcp_tool_backoff_base_ms: int = 150
    mcp_tool_backoff_max_ms: int = 1000
    mcp_tool_circuit_failure_threshold: int = 5
    mcp_tool_circuit_cooldown_ms: int = 30000
    mcp_tool_rate_limit_per_s: float = 50.0
    mcp_tool_max_concurrency: int = 8

    # ---- Safety ----
    finance_real_writes_enabled: bool = False

    # ------------------------------------------------------------------
    # 派生状态与启动校验（LLD §2.2 / §19.9）
    # ------------------------------------------------------------------
    @property
    def embeddings_enabled(self) -> bool:
        return bool(self.embedding_model)

    @property
    def rerank_enabled(self) -> bool:
        return self.rag_rerank_enabled and bool(self.rerank_base_url)

    @property
    def query_transform_enabled(self) -> bool:
        return (self.rag_transform_policy != "off"
                and (self.rag_query_rewrite_enabled or self.rag_hyde_enabled))

    @property
    def effective_worker_database_url(self) -> str:
        return self.worker_database_url or self.database_url

    @property
    def checkpoint_dsn(self) -> str:
        """PostgresSaver 需要 psycopg 同步 DSN（postgresql://），不能带 +asyncpg。"""
        url = self.checkpoint_database_url or self.database_url
        for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://", "postgres+psycopg://"):
            if url.startswith(prefix):
                return "postgresql://" + url[len(prefix):]
        return url

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_model) and self.llm_model != "<YOUR_MODEL>"

    @property
    def llm_small_enabled(self) -> bool:
        return self.llm_enabled and bool(self.llm_model_small)

    @property
    def hard_input_tokens(self) -> int:
        return self.llm_context_window - self.llm_max_output_tokens - self.llm_safety_margin_tokens

    @property
    def soft_input_tokens(self) -> int:
        return min(self.memory_input_soft_tokens, int(self.hard_input_tokens * 0.92))

    def startup_violations(self) -> list[str]:
        """返回阻断启动的问题列表；空列表表示可自动处置（§2.2）。"""
        errs: list[str] = []
        if self.llm_enabled:
            if self.llm_context_window < 32768:
                errs.append("LLM_CONTEXT_WINDOW < 32768：拒绝启动自动处置流程")
            if self.llm_max_output_tokens >= self.llm_context_window:
                errs.append("LLM_MAX_OUTPUT_TOKENS >= LLM_CONTEXT_WINDOW")
            if not (self.llm_supports_json_schema or self.llm_supports_json_object):
                errs.append("端点不支持 json_schema 也不支持 json_object：无法做结构化输出")
        return errs


@lru_cache
def get_settings() -> Settings:
    return Settings()
