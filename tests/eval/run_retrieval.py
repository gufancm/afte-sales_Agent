# -*- coding: utf-8 -*-
"""E8 检索离线评测 runner（recall@k / MRR / nDCG@k）。

零 API 成本：BM25 为本地分词，Dense/Rerank 走本地 BGE 服务；默认不做 LLM 查询变换。
消融配置：bm25 / dense / hybrid(RRF) / full(RRF+Rerank)。
门槛：E8 为「发现用途」，默认不设硬门槛（报告为主）；--min-recall5 可选软门槛。
用法（容器内，含 DB+embed+rerank）：
  docker compose run --rm api python tests/eval/run_retrieval.py
用法（宿主机，内存后端 BM25-only 冒烟）：
  python tests/eval/run_retrieval.py --backend memory
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import pathlib
import sys
import time
from collections import Counter
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.bootstrap import build_deps  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.rag.retrieval import PolicyChunk, rrf_fuse  # noqa: E402

DATA = pathlib.Path(__file__).parent / "data" / "retrieval_queries.jsonl"
REPORTS = ROOT / "reports"


def load_queries() -> list[dict]:
    if not DATA.exists():
        raise SystemExit(f"E8_INTEGRITY_FAIL: 缺少 {DATA}")
    rows = [json.loads(l) for l in DATA.read_text(encoding="utf-8").splitlines() if l.strip()]
    return rows


def integrity_check(rows: list[dict]) -> list[str]:
    problems: list[str] = []
    ids = [r["qid"] for r in rows]
    if len(ids) != len(set(ids)):
        problems.append("qid 存在重复")
    qs = [r["query"] for r in rows]
    if len(qs) != len(set(qs)):
        problems.append("query 文本存在重复")
    cl = json.loads((ROOT / "policy" / "clauses_jd.json").read_text(encoding="utf-8"))
    valid = {c["clause_id"] for c in cl["clauses"]}
    for r in rows:
        g = r.get("gold_clause_ids") or []
        if not g:
            problems.append(f"{r['qid']} gold 为空")
        for cid in g:
            if cid not in valid:
                problems.append(f"{r['qid']} gold 不在条款库: {cid}")
    return problems


# ---------- 排序策略 ----------
async def rank_bm25(r, tenant, q):
    return await r.lexical.search(tenant, q, 50, {})


async def rank_dense(r, tenant, q):
    return await r.dense.search(tenant, q, 50, {})


def _fuse(bm, dn, r):
    rk = [x for x in (bm, dn) if x]
    return rrf_fuse(rk, r.rrf_k)[: r.rrf_top_n] if rk else []


async def rank_hybrid(r, tenant, q):
    bm = await r.lexical.search(tenant, q, r.bm25_top_k, {})
    dn = await r.dense.search(tenant, q, r.dense_top_k, {})
    return _fuse(bm, dn, r)


async def rank_full(r, tenant, q):
    fused = await rank_hybrid(r, tenant, q)
    if not fused:
        return []
    return await r.reranker.rerank(q, fused, r.rerank_top_n)


CONFIGS = {"bm25": rank_bm25, "dense": rank_dense, "hybrid": rank_hybrid, "full": rank_full}


# ---------- 指标 ----------
def _ndcg(ranked_ids: list[str], gold: set[str], k: int) -> float:
    dcg = 0.0
    for i, cid in enumerate(ranked_ids[:k], start=1):
        if cid in gold:
            dcg += 1.0 / math.log2(i + 1)
    idcg = sum(1.0 / math.log2(j + 1) for j in range(1, min(len(gold), k) + 1))
    return dcg / idcg if idcg > 0 else 0.0


def _first_rank(ranked_ids: list[str], gold: set[str]) -> int | None:
    for i, cid in enumerate(ranked_ids, start=1):
        if cid in gold:
            return i
    return None


def evaluate(rows: list[dict], ranked_by_q: dict[str, list[list[str]]], ks: list[int]) -> dict:
    n = len(rows)
    names = list(CONFIGS)
    out = {"n": n}
    for name in names:
        idx = names.index(name)
        rec = {k: 0 for k in ks}
        ndcg = {k: 0.0 for k in ks}
        mrr = 0.0
        misses = 0
        for r in rows:
            ids = ranked_by_q[r["qid"]][idx]
            gold = set(r["gold_clause_ids"])
            fr = _first_rank(ids, gold)
            if fr is None:
                misses += 1
            else:
                mrr += 1.0 / fr
                for k in ks:
                    if fr <= k:
                        rec[k] += 1
            for k in ks:
                ndcg[k] += _ndcg(ids, gold, k)
        out[name] = {
            "recall": {f"recall@{k}": round(rec[k] / n, 4) for k in ks},
            "mrr": round(mrr / n, 4),
            "ndcg": {f"ndcg@{k}": round(ndcg[k] / n, 4) for k in ks},
            "miss": misses,
            "hit1": rec.get(1, 0),
        }
    return out


async def main() -> int:
    ap = argparse.ArgumentParser(description="E8 检索离线评测")
    ap.add_argument("--backend", default=None, choices=["memory", "pg"], help="覆盖 RAG_BACKEND")
    ap.add_argument("--tenant", default="tenant_demo")
    ap.add_argument("--with-transform", action="store_true", help="兼容旧参数：等价于 --transform-policy always")
    ap.add_argument("--transform-policy", default=None, choices=["off", "always", "adaptive"],
                    help="full 配置的查询变换策略（默认 off；adaptive=首轮低置信才调用 LLM）")
    ap.add_argument("--min-recall5", type=float, default=None, help="可选软门槛：full 配置 recall@5 下限")
    ap.add_argument("--ks", default="1,3,5,10")
    a = ap.parse_args()
    ks = [int(x) for x in a.ks.split(",")]

    rows = load_queries()
    problems = integrity_check(rows)
    if problems:
        print("E8_INTEGRITY_FAIL:")
        for p in problems[:20]:
            print("  -", p)
        return 4

    settings = get_settings()
    if a.backend:
        settings.rag_backend = a.backend
    policy = a.transform_policy or ("always" if a.with_transform else "off")
    settings.rag_transform_policy = policy
    if policy == "off":
        settings.rag_query_rewrite_enabled = False
        settings.rag_hyde_enabled = False
    else:
        settings.rag_query_rewrite_enabled = True
        settings.rag_hyde_enabled = True

    deps = build_deps(settings)
    r = deps.retriever
    if policy == "off":
        r.transformer = None  # 零成本：关闭 LLM 查询变换

    dense_on = bool(getattr(r.dense, "enabled", False))
    cl = json.loads((ROOT / "policy" / "clauses_jd.json").read_text(encoding="utf-8"))
    c2doc = {c["clause_id"]: c["doc_id"] for c in cl["clauses"]}

    ranked_by_q: dict[str, list[list[str]]] = {}
    detail: list[dict] = []
    names = list(CONFIGS)
    stats = {"queries": 0, "triggered": 0, "effective": 0, "llm_calls": 0,
             "cache_hits": 0, "latency_ms": 0.0, "reasons": Counter()}
    for i, row in enumerate(rows, 1):
        q = row["query"]
        per = []
        for name in names:
            if name == "full":
                t0 = time.perf_counter()
                try:
                    chunks, diag = await r.discover_ex(tenant_id=a.tenant, query=q, filters={})
                except Exception as exc:  # noqa: BLE001
                    print(f"[warn] {row['qid']} {name} 失败: {str(exc)[:100]}")
                    chunks, diag = [], {}
                stats["queries"] += 1
                stats["latency_ms"] += (time.perf_counter() - t0) * 1000.0
                if diag.get("transform_triggered"):
                    stats["triggered"] += 1
                if (diag.get("query_transform") or {}).get("effective"):
                    stats["effective"] += 1
                if (diag.get("query_transform") or {}).get("cache_hit"):
                    stats["cache_hits"] += 1
                stats["llm_calls"] += len(diag.get("llm_calls") or [])
                for reason in diag.get("transform_reasons") or []:
                    stats["reasons"][reason.split(":")[0]] += 1
            else:
                try:
                    chunks = await CONFIGS[name](r, a.tenant, q)
                except Exception as exc:  # noqa: BLE001
                    print(f"[warn] {row['qid']} {name} 失败: {str(exc)[:100]}")
                    chunks = []
            per.append([c.clause_id for c in chunks])
        ranked_by_q[row["qid"]] = per
        rec = {"qid": row["qid"], "query": q, "type": row["type"], "gold": row["gold_clause_ids"]}
        for name, ids in zip(names, per):
            fr = _first_rank(ids, set(row["gold_clause_ids"]))
            rec[f"{name}_rank"] = fr if fr else "miss"
            rec[f"{name}_top5"] = "|".join(ids[:5])
        detail.append(rec)
        if i % 10 == 0:
            print(f"  ... {i}/{len(rows)}", flush=True)

    # 文档级 recall@5
    doc_hit = {}
    for name in names:
        idx = names.index(name)
        h = 0
        for row in rows:
            top5 = ranked_by_q[row["qid"]][idx][:5]
            golddocs = set(row["gold_doc_ids"])
            if any(c2doc.get(cid) in golddocs for cid in top5):
                h += 1
        doc_hit[name] = round(h / len(rows), 4)

    metrics = evaluate(rows, ranked_by_q, ks)
    for name in names:
        metrics[name]["doc_recall@5"] = doc_hit[name]

    ts = datetime.now().strftime("%Y-%m-%d")
    REPORTS.mkdir(exist_ok=True)
    trig_rate = stats["triggered"] / max(1, stats["queries"])
    avg_ms = stats["latency_ms"] / max(1, stats["queries"])
    top_reasons = "、".join(f"{k}={v}" for k, v in stats["reasons"].most_common(6)) or "无"
    md = [f"# E8 检索离线评测（{ts}）", "",
          f"- 查询集：`tests/eval/data/retrieval_queries.jsonl`（{len(rows)} 条，人工编写，gold=真实条款）",
          f"- 后端：RAG_BACKEND=`{settings.rag_backend}`，Dense={'on' if dense_on else 'off'}，"
          f"查询变换策略=`{policy}`",
          f"- 检索深度：bm25/dense top50；RRF 候选 {r.rrf_top_n}；重排输出 {r.rerank_top_n}",
          f"- 变换统计：触发 {stats['triggered']}/{stats['queries']}（{trig_rate:.1%}），"
          f"生效 {stats['effective']}，LLM 调用 {stats['llm_calls']} 次，缓存命中 {stats['cache_hits']}，"
          f"full 平均耗时 {avg_ms:.0f} ms/query",
          f"- 触发原因：{top_reasons}", "",
          "| 配置 | recall@1 | recall@3 | recall@5 | recall@10 | MRR | nDCG@5 | nDCG@10 | 文档recall@5 | miss |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for name in names:
        m = metrics[name]
        md.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %d |" % (
            name,
            m["recall"]["recall@1"], m["recall"]["recall@3"], m["recall"]["recall@5"], m["recall"]["recall@10"],
            m["mrr"], m["ndcg"]["ndcg@5"], m["ndcg"]["ndcg@10"], m["doc_recall@5"], m["miss"]))
    md += ["", "> 说明：`full` 生产输出仅 Top-5，recall@10 与 recall@5 相同；`bm25/dense` 报 top50 便于看召回上限。",
           "> E10 重排增益 = `full.nDCG@5 - hybrid.nDCG@5`（见下）。", ""]
    deg = getattr(getattr(r, "reranker", None), "degraded_count", 0)
    if deg:
        md.append(f"- ⚠️ 重排降级 {deg} 次（返回 RRF 原序）：{getattr(r.reranker, 'last_error', '')}")
    gain = metrics["full"]["ndcg"]["ndcg@5"] - metrics["hybrid"]["ndcg"]["ndcg@5"]
    md.append(f"- E10 重排增益 nDCG@5：**{gain:+.4f}**（门槛 ≥ +0.03）" + ("  ✅" if gain >= 0.03 else "  ⚠️ 未达门槛"))
    (REPORTS / f"e8-retrieval-{ts}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (REPORTS / f"e8-retrieval-{ts}.json").write_text(
        json.dumps({"metrics": metrics, "detail": detail, "dense_on": dense_on,
                    "transform_policy": policy,
                    "transform_stats": {**stats, "reasons": dict(stats["reasons"])},
                    "rerank_degraded": getattr(getattr(r, "reranker", None), "degraded_count", 0)},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")

    print("\n".join(md[:12]))
    print(f"\n报告已写入 reports/e8-retrieval-{ts}.md / .json")

    if a.min_recall5 is not None and metrics["full"]["recall"]["recall@5"] < a.min_recall5:
        print(f"E8_GATE_FAIL: full recall@5={metrics['full']['recall']['recall@5']} < {a.min_recall5}")
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))