"""认证与租户上下文（LLD §12.1）。tenant_id/user_id 只来自可信令牌，不取请求体。"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import jwt
from fastapi import Header, HTTPException

from app.config import get_settings


@dataclass
class Ctx:
    tenant_id: str
    user_id: str
    trace_id: str


async def get_ctx(
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
    x_trace_id: str | None = Header(default=None),
) -> Ctx:
    s = get_settings()
    trace = x_trace_id or str(uuid.uuid4())
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1]
        try:
            payload = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_alg], issuer=s.jwt_issuer)
            return Ctx(tenant_id=payload["tenant_id"], user_id=payload["sub"], trace_id=trace)
        except jwt.PyJWTError:
            raise HTTPException(status_code=401, detail="INVALID_TOKEN")
    # 仅开发环境允许用头部直接传租户/用户
    if s.app_env in ("development", "test") and x_tenant_id and x_user_id:
        return Ctx(tenant_id=x_tenant_id, user_id=x_user_id, trace_id=trace)
    raise HTTPException(status_code=401, detail="MISSING_CREDENTIALS")