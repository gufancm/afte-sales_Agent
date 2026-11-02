# -*- coding: utf-8 -*-
"""本地 BGE-M3 向量服务（OpenAI 兼容 /v1/embeddings）。

用途：为 RAG 的 Dense 检索与 policy_chunks 向量回填提供统一入口；宿主机或容器内都能跑。
接口：
  GET  /health         -> {"status":"ok","model":...,"dim":...,"device":...}
  GET  /v1/models      -> OpenAI 兼容模型列表
  POST /v1/embeddings  -> OpenAI 兼容向量接口（input 支持 str 或 list[str]）

实现说明：BGE-M3 的 dense 向量 = 取 [CLS] 位置隐状态后做 L2 归一化，与官方 FlagEmbedding
的 dense_vecs 口径一致；不引入 sentence-transformers，减少运行时依赖。
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
from transformers import AutoModel, AutoTokenizer

MODEL_LOCK = threading.Lock()


class EmbeddingRequest(BaseModel):
    model: str | None = None
    input: Any = None
    encoding_format: str | None = "float"
    dimensions: int | None = None
    user: str | None = None


def create_app(model_path: str, device: str, max_length: int, batch_size: int,
               dtype: str, api_key: str | None, service_name: str) -> FastAPI:
    tokenizer = AutoTokenizer.from_pretrained(model_path)
    torch_dtype = {"float32": torch.float32, "float16": torch.float16, "bfloat16": torch.bfloat16}[dtype]
    model = AutoModel.from_pretrained(model_path, torch_dtype=torch_dtype)
    model.eval().to(device)
    dim = int(model.config.hidden_size)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        with MODEL_LOCK:
            _encode(["warmup"])
        print(f"[i] warmed up: model={model_path} device={device} dtype={dtype} dim={dim}", flush=True)
        yield

    app = FastAPI(title=f"{service_name}-embedding", version="1.0", lifespan=lifespan)

    def _check_auth(authorization: str | None) -> None:
        if not api_key:
            return
        token = (authorization or "").removeprefix("Bearer ").strip()
        if token != api_key:
            raise HTTPException(status_code=401, detail="invalid api key")

    def _encode(texts: list[str]) -> tuple[list[list[float]], int]:
        vecs: list[list[float]] = []
        total_tokens = 0
        with torch.no_grad():
            for i in range(0, len(texts), batch_size):
                chunk = [t if t and t.strip() else " " for t in texts[i:i + batch_size]]
                enc = tokenizer(chunk, padding=True, truncation=True,
                                max_length=max_length, return_tensors="pt").to(device)
                out = model(**enc)
                cls = torch.nn.functional.normalize(out.last_hidden_state[:, 0], p=2, dim=1)
                vecs.extend(cls.to(torch.float32).cpu().tolist())
                total_tokens += int(enc["attention_mask"].sum().item())
        return vecs, total_tokens

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "service": service_name, "model": model_path,
                "dim": dim, "device": device, "dtype": dtype, "max_length": max_length}

    @app.get("/v1/models")
    def list_models() -> dict[str, Any]:
        return {"object": "list", "data": [{"id": service_name, "object": "model", "owned_by": "local"}]}

    @app.post("/v1/embeddings")
    def embeddings(req: EmbeddingRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _check_auth(authorization)
        inp = req.input
        if isinstance(inp, str):
            texts = [inp]
        elif isinstance(inp, list) and all(isinstance(x, str) for x in inp):
            texts = list(inp)
        else:
            raise HTTPException(status_code=400, detail="input 必须是字符串或字符串数组")
        if not texts:
            raise HTTPException(status_code=400, detail="input 不能为空")
        if req.dimensions is not None and int(req.dimensions) != dim:
            raise HTTPException(status_code=400,
                                detail=f"该模型输出维度固定为 {dim}，不支持 dimensions={req.dimensions}")
        t0 = time.time()
        with MODEL_LOCK:
            vecs, ntok = _encode(texts)
        data = [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vecs)]
        return {"object": "list", "data": data, "model": req.model or service_name,
                "usage": {"prompt_tokens": ntok, "total_tokens": ntok},
                "elapsed_ms": int((time.time() - t0) * 1000)}

    return app


def main() -> int:
    ap = argparse.ArgumentParser(description="本地 BGE-M3 embedding 服务（OpenAI 兼容）")
    ap.add_argument("--model-path", required=True, help="本地模型目录（或用 HF repo id，需可联网）")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=7997)
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--max-length", type=int, default=8192)
    ap.add_argument("--dtype", default="auto", choices=["auto", "float32", "float16", "bfloat16"])
    ap.add_argument("--api-key", default=None, help="设置后要求 Bearer 校验；本地默认不校验")
    ap.add_argument("--service-name", default="BAAI/bge-m3")
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
    print(f"[i] serving on http://{a.host}:{a.port} (openai-compatible /v1/embeddings)", flush=True)
    uvicorn.run(app, host=a.host, port=a.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())