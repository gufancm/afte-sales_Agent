"""FastAPI 入口。启动校验（§2.2/§19.9）。"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import Response

from app.api.routes import router
from app.bootstrap import build_deps
from app.config import get_settings
from app.graph.build import build_graph
from app.graph.checkpointer import open_checkpointer
from app.observability import metrics as M

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("aftersale")

# Windows 的默认 ProactorEventLoop 不被 psycopg 异步驱动支持；启用 Postgres Checkpointer 时切 Selector。
# 必须在 uvicorn 创建事件循环之前生效（模块导入阶段），否则启动会静默降级为内存 Checkpointer。
if sys.platform == "win32" and get_settings().checkpoint_backend == "postgres":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    log.info("Checkpointer=postgres：Windows 下切换为 SelectorEventLoop（psycopg 要求）")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    violations = settings.startup_violations()
    if violations:
        # 拒绝启动自动处置流程；仅保留只读/人工（§2.2）
        log.warning("LLM 启动校验未通过，进入只读/人工模式：%s", violations)
    if not settings.finance_real_writes_enabled:
        log.warning("FINANCE_REAL_WRITES_ENABLED=false：外部资金写保持禁用（沙箱模式）")
    deps = build_deps(settings)
    # HITL 暂停点持久化：postgres 时跨重启/多副本可恢复，失败按 CHECKPOINT_STRICT 降级或拒绝启动
    ckpt = await open_checkpointer(settings)
    if not ckpt.durable:
        log.warning("Checkpointer 非持久化：%s（重启后待审批的 interrupt 会丢失）", ckpt.detail)
    app.state.checkpointer = ckpt
    deps.graph = build_graph(deps, checkpointer=ckpt.saver)  # type: ignore[attr-defined]
    app.state.deps = deps
    app.state.startup_violations = violations
    try:
        yield
    finally:
        await ckpt.aclose()


app = FastAPI(title="aftersale-agent", version="3.5", lifespan=lifespan)
app.include_router(router)


@app.middleware("http")
async def _observe(request: Request, call_next):
    """HTTP 指标中间件：路径去掉 UUID，避免标签基数爆炸。"""
    started = time.perf_counter()
    response = await call_next(request)
    path = _UUID_RE.sub("{id}", request.url.path)
    if path != "/metrics":
        M.HTTP_REQUESTS.labels(method=request.method, path=path, status=str(response.status_code)).inc()
        M.HTTP_LATENCY.labels(method=request.method).observe((time.perf_counter() - started) * 1000)
    return response


@app.get("/metrics")
async def metrics_endpoint() -> Response:
    """Prometheus 抓取端点（text/plain; version=0.0.4）。"""
    return Response(content=M.render(), media_type=M.content_type())


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/ready")
async def ready() -> dict:
    s = get_settings()
    ckpt = getattr(app.state, "checkpointer", None)
    return {
        "ready": not s.startup_violations(),
        "startup_violations": s.startup_violations(),
        "llm_enabled": s.llm_enabled,
        "finance_real_writes_enabled": s.finance_real_writes_enabled,
        "checkpointer": getattr(ckpt, "backend", "unknown"),
        "checkpointer_durable": bool(getattr(ckpt, "durable", False)),
    }
