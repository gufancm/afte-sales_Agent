"""Saga：命令模型与幂等标识（LLD §9.1/§9.2）。"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from app.domain.canonical import payload_hash


def new_operation_id() -> str:
    return str(uuid.uuid4())


def idempotency_key(business_ref: str, step: str) -> str:
    """稳定幂等键：同一业务请求/步骤必须可重复生成同一 key（§9.2）。"""
    return f"{business_ref}:{step}"


@dataclass
class SagaCommand:
    tenant_id: str
    ticket_id: str
    run_id: str
    action: str
    order_id: str | None
    payload: dict[str, Any]
    operation_id: str = field(default_factory=new_operation_id)
    idempotency_key: str = ""
    payload_hash: str = ""
    quote_id: str | None = None
    quote_hash: str | None = None
    authorization_ref: str | None = None
    status: str = "PREPARED"

    def __post_init__(self) -> None:
        self.payload_hash = self.payload_hash or payload_hash(self.payload)
        self.idempotency_key = self.idempotency_key or idempotency_key(
            self.authorization_ref or self.ticket_id, self.action)


def is_same_request(existing: dict, incoming: SagaCommand) -> bool:
    """同 key 同 payload -> 幂等返回；同 key 不同 payload -> 冲突（§9.2）。"""
    if existing.get("idempotency_key") != incoming.idempotency_key:
        return False
    if existing.get("payload_hash") != incoming.payload_hash:
        raise ValueError("IDEMPOTENCY_CONFLICT：同键不同 payload")
    return True