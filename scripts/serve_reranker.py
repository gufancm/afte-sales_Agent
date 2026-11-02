# -*- coding: utf-8 -*-
"""本地 BGE Reranker 服务（cross-encoder，/rerank 接口）。

用途：RAG 的检索重排（RRF 融合后的候选 -> cross-encoder 精排）。
接口：
  GET  /health  -> {"status":"ok","model":...,"device":...}
  POST /rerank  -> {"model":...,"query":...,"documents":[...],"top_n":N}
                   => {"results":[{"index":i,"relevance_score":s}, ...]}（按分数降序）
权重不打进镜像：由 docker-compose 以只读卷挂载（RERANK_HOST_DIR），容器内 HF_HUB_OFFLINE=1。
实现：AutoModelForSequenceClassification 的 logits -> sigmoid，与 BGE reranker 官方口径一致。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import argparse
import threading
import time
from typing import Any

import torch
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from transformers import AutoModelForSequenceClassification, AutoTokenizer

MODEL_LOCK = threading.Lock()


class RerankRequest(BaseModel):
    model: str | None = None
    query: str | None = None
    documents: list[str] | None = None
    top_n: int | None = None


def create_app(model_path: str, device: str, max_length: int, batch_size: int, dtype: str,
               api_key: str | None, service_name: str) -> FastAPI:
    tokenizer = AutoTokenizer.from_pretrained(model_path)
    torch_dtype = {"float32": torch.float32, "float16": torch.float16, "bfloat16": torch.bfloat16}[dtype]
    model = AutoModelForSequenceClassification.from_pretrained(model_path, torch_dtype=torch_dtype)
    model.eval().to(device)

    def _score(pairs: list[tuple[str, str]]) -> list[float]:
        out: list[float] = []
        with torch.no_grad():
            for i in range(0, len(pairs), batch_size):
                batch = pairs[i:i + batch_size]
                enc = tokenizer([p[0] for p in batch], [p[1] for p in batch], padding=True,
                                truncation=True, max_length=max_length, return_tensors="pt").to(device)
                logits = model(**enc).logits.view(-1).to(torch.float32)
                out.extend(torch.sigmoid(logits).cpu().tolist())
        return out

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        with MODEL_LOCK:
            _score([("warmup", "warmup")])
        print(f"[i] warmed up: model={model_path} device={device} dtype={dtype}", flush=True)
        yield

    app = FastAPI(title=f"{service_name}-rerank", version="1.0", lifespan=lifespan)

    def _check_auth(authorization: str | None) -> None:
        if not api_key:
            return
        if (authorization or "").removeprefix("Bearer ").strip() != api_key:
            raise HTTPException(status_code=401, detail="invalid api key")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "service": service_name, "model": model_path, "device": device, "dtype": dtype}

    @app.post("/rerank")
    def rerank(req: RerankRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _check_auth(authorization)
        if not req.query or not req.documents:
            raise HTTPException(status_code=400, detail="query 与 documents 均不能为空")
        t0 = time.time()
        with MODEL_LOCK:
            scores = _score([(req.query, d or " ") for d in req.documents])
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        top_n = req.top_n or len(order)
        results = [{"index": i, "relevance_score": scores[i]} for i in order[:top_n]]
        return {"model": req.model or service_name, "results": results,
                "elapsed_ms": int((time.time() - t0) * 1000)}

    return app


def main() -> int:
    ap = argparse.ArgumentParser(description="本地 BGE Reranker 服务（cross-encoder）")
    ap.add_argument("--model-path", required=True)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=7998)
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--max-length", type=int, default=1024)
    ap.add_argument("--dtype", default="auto", choices=["auto", "float32", "float16", "bfloat16"])
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--service-name", default="BAAI/bge-reranker-v2-m3")
    a = ap.parse_args()
    device = a.device
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = a.dtype
    if dtype == "auto":
        dtype = "float16" if device == "cuda" else "float32"
    if device == "cpu" and dtype in ("float16", "bfloat16"):
        dtype = "float32"
    app = create_app(a.model_path, device, a.max_length, a.batch_size, dtype, a.api_key, a.service_name)
    import uvicorn
    print(f"[i] serving on http://{a.host}:{a.port} (/rerank)", flush=True)
    uvicorn.run(app, host=a.host, port=a.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())