# -*- coding: utf-8 -*-
"""E35 UAT 场景集 runner（端到端，需完整栈：api + postgres + worker + mcp-mock + LLM）。

用法：
  $env:API_BASE="http://localhost:8000"; python tests/eval/run_uat.py
环境变量：API_BASE（默认 http://localhost:8000）、UAT_TIMEOUT（默认 60 秒）。
未连上 API 时自动跳过（退出码 0）。当前为“观察式 + 关键确认硬校验”：
打印每场景 expected(意图/response_type) 与 observed(run_status/回复)，对 review=confirmed 的场景做硬校验。
"""
from __future__ import annotations

import json
import os
import pathlib
import urllib.error
import urllib.request
import uuid

import yaml

DATA = pathlib.Path(__file__).parent / "data" / "uat_scenarios.yaml"
API = os.environ.get("API_BASE", "http://localhost:8000").rstrip("/")
TIMEOUT = float(os.environ.get("UAT_TIMEOUT", "60"))


def _req(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json", "x-tenant-id": "tenant_demo",
               "x-user-id": "u_demo", "x-trace-id": str(uuid.uuid4()),
               "Idempotency-Key": str(uuid.uuid4())}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode("utf-8")[:200]}
    except Exception as e:  # noqa: BLE001 - 超时/连接错误按“无观测”处理，不崩溃
        return 0, {"error": f"{type(e).__name__}: {e}"}


def healthy() -> bool:
    for path in ("/health", "/ready"):
        code, _ = _req("GET", path)
        if code == 200:
            return True
    return False


def main() -> int:
    spec = yaml.safe_load(DATA.read_text(encoding="utf-8"))
    scenarios = spec["scenarios"]
    if not healthy():
        print(f"[E35] API ({API}) 不可达，跳过端到端 UAT。先 docker compose up -d 并设 API_BASE。")
        return 0
    confirmed_fail = 0
    rows = []
    for sc in scenarios:
        code, t = _req("POST", "/api/v1/tickets", {"intent": "UNKNOWN", "order_id": sc.get("order_id")})
        if code >= 300 or not isinstance(t, dict) or "ticket_id" not in t:
            rows.append((sc["id"], sc["expect"]["intent"], sc["expect"]["response_type"],
                         f"create_fail({code})", str(t.get("error", ""))[:30], sc.get("review")))
            confirmed_fail += 1 if sc.get("review") == "confirmed" else 0
            continue
        tid = t["ticket_id"]
        code, r = _req("POST", f"/api/v1/tickets/{tid}/messages", {"text": sc["input"]})
        run_status = r.get("run_status") or r.get("status") or (f"ERR({code})" if code != 200 else None)
        response = (r.get("response") or r.get("error") or "")[:38]
        rows.append((sc["id"], sc["expect"]["intent"], sc["expect"]["response_type"],
                     str(run_status), response, sc.get("review")))
        if sc.get("review") == "confirmed" and not (r.get("response") or r.get("run_status")):
            confirmed_fail += 1
    print(f"[E35] UAT 观察表（total={len(rows)}）")
    print(f"  {'ID':8s} {'exp_intent':16s} {'exp_type':12s} {'obs_run_status':18s} review")
    for cid, ei, er, obs, _resp, rev in rows:
        print(f"  {cid:8s} {ei:16s} {er:12s} {obs:18s} {rev}")
    print(f"[E35] confirmed 硬失败：{confirmed_fail}")
    print("E35_UAT_OK" if confirmed_fail == 0 else "E35_UAT_FAIL")
    return 0 if confirmed_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())