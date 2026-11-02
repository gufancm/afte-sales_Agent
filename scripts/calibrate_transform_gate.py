# -*- coding: utf-8 -*-
"""零成本标定 adaptive 查询变换门控：只跑首轮检索，不调用任何 LLM。

用途：用真实 E8 query 统计“如果按当前阈值，会不会触发改写”，
从而在不开销、不增加延迟的前提下调阈值。

注意（数据泄漏）：运行时 `discover()` 在政策裁决之前调用，**拿不到** required_clause_ids，
因此门控里的 required_coverage 信号在真实链路上不会触发。默认按运行时口径（none）标定；
`--required-from gold` 只用于离线敏感性分析，不能当线上触发率汇报。

用法（容器内）：
  docker compose run --rm api python scripts/calibrate_transform_gate.py
  docker compose run --rm api python scripts/calibrate_transform_gate.py --history-aware
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.bootstrap import build_deps  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.rag.retrieval import rrf_fuse  # noqa: E402

DATA = pathlib.Path(__file__).resolve().parent.parent / "tests" / "eval" / "data" / "retrieval_queries.jsonl"


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(p * (len(ordered) - 1)))))
    return round(ordered[idx], 4)


async def main() -> int:
    ap = argparse.ArgumentParser(description="adaptive 门控零成本标定")
    ap.add_argument("--tenant", default="tenant_demo")
    ap.add_argument("--data", default=str(DATA))
    ap.add_argument("--history-aware", action="store_true",
                    help="模拟有会话上下文的场景（给每条 query 配一段前文）")
    ap.add_argument("--required-from", choices=("none", "gold"), default="none",
                    help="none=运行时口径（默认，模拟拿不到必备条款）；gold=离线泄漏口径，仅供敏感性分析")
    ap.add_argument("--json-out", default="")
    a = ap.parse_args()

    settings = get_settings()
    deps = build_deps(settings)
    r = deps.retriever

    rows = [json.loads(line) for line in pathlib.Path(a.data).read_text(encoding="utf-8").splitlines() if line.strip()]
    triggered = 0
    llm_needed = 0
    reason_counts: dict[str, int] = {}
    sims: list[float] = []
    margins: list[float] = []
    details = []

    for row in rows:
        q = row["query"]
        history = ["我之前申请了退货退款，现在想问一下进度"] if a.history_aware else None
        bm = await r.lexical.search(a.tenant, q, r.bm25_top_k, {})
        dn = await r.dense.search(a.tenant, q, r.dense_top_k, {})
        fused = rrf_fuse([x for x in (bm, dn) if x], r.rrf_k)[: r.rrf_top_n] if (bm or dn) else []
        hit, reasons, info, need_llm = r._decide_transform(
            query=q, dense_hits=dn, fused=fused,
            required_clause_ids=(row.get("gold_clause_ids") if a.required_from == "gold" else None),
            history=history)
        if hit:
            triggered += 1
            if need_llm:
                llm_needed += 1
            for reason in reasons:
                key = reason.split(":")[0]
                reason_counts[key] = reason_counts.get(key, 0) + 1
        if info.get("dense_top1_sim") is not None:
            sims.append(info["dense_top1_sim"])
        if info.get("dense_margin") is not None:
            margins.append(info["dense_margin"])
        details.append({"qid": row.get("qid"), "query": q, "triggered": hit,
                        "llm_needed": need_llm, "reasons": reasons,
                        "dense_top1_sim": info.get("dense_top1_sim"),
                        "dense_margin": info.get("dense_margin")})

    total = len(rows)
    summary = {
        "queries": total,
        "triggered": triggered,
        "trigger_rate": round(triggered / total, 4) if total else 0.0,
        "llm_needed": llm_needed,
        "llm_rate": round(llm_needed / total, 4) if total else 0.0,
        "reasons": dict(sorted(reason_counts.items(), key=lambda kv: -kv[1])),
        "dense_top1_sim_p05_p50_p95": [_pct(sims, 0.05), _pct(sims, 0.50), _pct(sims, 0.95)],
        "dense_margin_p10_p50_p90": [_pct(margins, 0.10), _pct(margins, 0.50), _pct(margins, 0.90)],
        "policy": r.transform_policy,
        "thresholds": {"min_dense_sim": r.adaptive_min_dense_sim,
                       "min_margin": r.adaptive_min_margin,
                       "short_query_chars": r.adaptive_short_query_chars,
                       "min_candidates": r.adaptive_min_candidates,
                       "soft_signals_required": r.adaptive_soft_signals_required},
        "history_aware": bool(a.history_aware),
        "required_from": a.required_from,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if a.json_out:
        pathlib.Path(a.json_out).write_text(
            json.dumps({"summary": summary, "details": details}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
