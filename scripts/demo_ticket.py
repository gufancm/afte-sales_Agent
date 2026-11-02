# -*- coding: utf-8 -*-
"""一键测试：通过 HTTP 驱动完整工单链路（建单 -> 发消息 -> HITL 审批 -> 终态）。真实调用你配置的 LLM。

用法（容器内，服务已在 compose 网络里）：
    docker compose run --rm api python scripts/demo_ticket.py
    docker compose run --rm api python scripts/demo_ticket.py "订单 o_1002 衣服破洞，退货退款" o_1002
    docker compose run --rm api python scripts/demo_ticket.py "生鲜坏了要退款" o_1001
    docker compose run --rm api python scripts/demo_ticket.py "..." o_1002 --deny     # 拒绝审批

可用环境变量覆盖：API_BASE(默认 http://api:8000)、TENANT_ID、USER_ID。
"""
from __future__ import annotations

import json
import os
import sys

import httpx

BASE = os.environ.get("API_BASE", "http://api:8000")
TENANT = os.environ.get("TENANT_ID", "tenant_demo")
USER = os.environ.get("USER_ID", "u_demo")
H = {"x-tenant-id": TENANT, "x-user-id": USER}
REVIEWER = {"x-tenant-id": TENANT, "x-user-id": "reviewer_1"}


def show(title: str, obj: object) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main() -> int:
    text = sys.argv[1] if len(sys.argv) > 1 else "订单 o_1002 这件衣服到货就有破洞，属于质量问题，我要退货退款，金额 500 元"
    order_id = sys.argv[2] if len(sys.argv) > 2 else "o_1002"
    decision = "DENIED" if "--deny" in sys.argv else "APPROVED"

    with httpx.Client(base_url=BASE, timeout=300.0) as c:
        r = c.post("/api/v1/tickets", json={"order_id": order_id, "intent": "UNKNOWN"}, headers=H)
        r.raise_for_status()
        ticket = r.json()
        tid = ticket["ticket_id"]
        show("1) 建单", ticket)

        r = c.post(f"/api/v1/tickets/{tid}/messages", json={"text": text}, headers=H)
        r.raise_for_status()
        run = r.json()
        show("2) 运行结果", run)

        approval = run.get("approval")
        if approval:
            r = c.post(f"/api/v1/tickets/{tid}/approval",
                       json={"approval_id": approval["approval_id"], "decision": decision}, headers=REVIEWER)
            r.raise_for_status()
            show(f"3) 审批 {decision}", r.json())

        r = c.get(f"/api/v1/tickets/{tid}", headers=H)
        if r.status_code == 200:
            show("4) 工单终态", r.json())

    print(f"\nTICKET_ID={tid}")
    print("查看事件流：GET /api/v1/tickets/<id>/events（SSE，-H 'Last-Event-ID: 0'）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())