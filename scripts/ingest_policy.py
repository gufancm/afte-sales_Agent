"""政策库入库：把 docs/policy 下的真实政策文档切分写入 policy_documents/policy_chunks，
并可选把 policy/rules.json 的规则写入 policy_rules（供 DB/pg 后端使用）。

切分与 clause_id 规则见 app/policy/mdclauses.py；文档清单见 policy/rules.json 的 documents。

常用：
  # 预览（不连库）：单文件
  python scripts/ingest_policy.py docs/policy/生鲜售后政策.md --doc-id doc_fresh --dry-run
  # 预览：全部文档 + 规则
  python scripts/ingest_policy.py --all --rules --dry-run
  # 全量入库（规则 + 文档），先清空该 doc+version 再写
  python scripts/ingest_policy.py --all --rules --replace \
      --database-url postgresql+asyncpg://aftersale_app:app_pw_change_me@localhost:5432/aftersale
  # 入库并生成 embedding（需 EMBEDDING_* 配置）
  python scripts/ingest_policy.py --all --replace --embed --database-url ...
  # 查看已入库内容
  python scripts/ingest_policy.py --list --database-url ...

说明：容器内主机名用 postgres；从宿主机执行请用 localhost。写入需非超级用户 + 已 set app.tenant_id（脚本已处理）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from datetime import datetime

import asyncpg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.policy.library import documents, load_rules, library_version  # noqa: E402
from app.policy.mdclauses import build_chunks  # noqa: E402
from app.rag.tokenize import token_counts  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CLAUSES_JD = REPO_ROOT / "policy" / "clauses_jd.json"


def _dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _embed(settings: Settings, texts: list[str]) -> list[list[float]]:
    import httpx
    base = (settings.embedding_base_url or settings.llm_base_url).rstrip("/")
    key = settings.embedding_api_key or settings.llm_api_key
    out: list[list[float]] = []
    async with httpx.AsyncClient(timeout=60) as c:
        for i in range(0, len(texts), 16):
            batch = texts[i:i + 16]
            r = await c.post(f"{base}/embeddings", headers={"Authorization": f"Bearer {key}"},
                             json={"model": settings.embedding_model, "input": batch})
            r.raise_for_status()
            out.extend(d["embedding"] for d in r.json()["data"])
    return out


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


async def _ensure_dim(conn: asyncpg.Connection, dim: int) -> None:
    cur = await conn.fetchval(
        "SELECT atttypmod FROM pg_attribute WHERE attrelid='policy_chunks'::regclass AND attname='embedding'")
    if cur != dim:
        await conn.execute(f"ALTER TABLE policy_chunks ALTER COLUMN embedding TYPE vector({int(dim)})")
        print(f"embedding 维度已调整为 vector({dim})")


async def do_list(conn: asyncpg.Connection, tenant: str) -> None:
    docs = await conn.fetch(
        "SELECT doc_id,title,version,status FROM policy_documents WHERE tenant_id=$1 ORDER BY doc_id", tenant)
    print(f"policy_documents ({len(docs)}):")
    for d in docs:
        print(f"  - {d['doc_id']} | {d['title']} | {d['version']} | {d['status']}")
    rules = await conn.fetch(
        "SELECT rule_id,priority,decision,reason_code,array_length(required_clause_ids,1) AS n "
        "FROM policy_rules WHERE tenant_id=$1 ORDER BY priority DESC, rule_id", tenant)
    print(f"policy_rules ({len(rules)}):")
    for r in rules:
        print(f"  - {r['rule_id']} p={r['priority']} {r['decision']} {r['reason_code']} clauses={r['n']}")
    rows = await conn.fetch(
        "SELECT doc_id, clause_id, clause_group_id, is_exception, embedding_model, "
        "left(text, 60) AS preview FROM policy_chunks WHERE tenant_id=$1 ORDER BY doc_id, clause_id", tenant)
    print(f"policy_chunks ({len(rows)}):")
    for r in rows:
        flag = "EXC" if r["is_exception"] else "   "
        emb = r["embedding_model"] or "-"
        print(f"  [{flag}] {r['clause_id']} ({r['doc_id']}) grp={r['clause_group_id']} emb={emb} :: {r['preview']}")


def preview(text: str, doc_id: str, version: str, chunk_size: int, overlap: int) -> list[dict]:
    chunks = build_chunks(text, doc_id, doc_id, chunk_size, overlap)
    print(f"\n== {doc_id} version={version} chunks={len(chunks)} ==")
    for c in chunks[:5]:
        print(f"  {c['clause_id']} [{len(c['text'])}字] grp={c['clause_group_id']} exc={c['is_exception']}")
    if len(chunks) > 5:
        print(f"  ... 其余 {len(chunks) - 5} 条")
    return chunks


def _ts(value):
    """规则 effective_from/effective_to 是 ISO 字符串，asyncpg 需要 datetime。"""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


async def upsert_rules(conn: asyncpg.Connection, tenant: str, replace: bool) -> int:
    rules = load_rules()
    if replace:
        await conn.execute("DELETE FROM policy_rules WHERE tenant_id=$1", tenant)
    for r in rules:
        await conn.execute(
            "INSERT INTO policy_rules(rule_id,tenant_id,version,priority,when_json,decision,reason_code,"
            "required_clause_ids,amount_cap_minor,cap_type,stage,scope,status,effective_from,effective_to) "
            "VALUES($1,$2,$3,$4, CAST($5 AS jsonb),$6,$7,$8,$9,$10,$11, CAST($12 AS jsonb),$13,$14,$15) "
            "ON CONFLICT (rule_id) DO UPDATE SET version=EXCLUDED.version, priority=EXCLUDED.priority, "
            "when_json=EXCLUDED.when_json, decision=EXCLUDED.decision, reason_code=EXCLUDED.reason_code, "
            "required_clause_ids=EXCLUDED.required_clause_ids, amount_cap_minor=EXCLUDED.amount_cap_minor, "
            "cap_type=EXCLUDED.cap_type, stage=EXCLUDED.stage, "
            "scope=EXCLUDED.scope, status=EXCLUDED.status, effective_from=EXCLUDED.effective_from, "
            "effective_to=EXCLUDED.effective_to",
            r.rule_id, tenant, r.version, r.priority,
            json.dumps([{"fact": c.fact, "op": c.op, "value": c.value} for c in r.when], ensure_ascii=False),
            r.decision, r.reason_code, list(r.required_clause_ids), r.amount_cap_minor,
            r.cap_type, r.resolved_stage,
            json.dumps(r.scope, ensure_ascii=False), r.status, _ts(r.effective_from), _ts(r.effective_to))
    print(f"已写入 {len(rules)} 条规则（policy_rules，tenant={tenant}）")
    return len(rules)


async def ingest_doc(conn: asyncpg.Connection, settings: Settings, tenant: str, *, doc_id: str, title: str,
                     version: str, text: str, chunk_size: int, overlap: int, do_embed: bool,
                     replace: bool, dim: int) -> int:
    chunks = preview(text, doc_id, version, chunk_size, overlap)
    vecs = None
    if do_embed:
        if not settings.embeddings_enabled:
            print("  [warn] 未配置 EMBEDDING_MODEL，跳过 embedding")
        else:
            vecs = await _embed(settings, [c["text"] for c in chunks])
            dim = len(vecs[0])
            await _ensure_dim(conn, dim)
            print(f"  已生成 {len(vecs)} 条 embedding（维度 {dim}）")
    async with conn.transaction():
        await conn.execute(
            "INSERT INTO policy_documents(doc_id,tenant_id,title,version,status,effective_from,source_uri) "
            "VALUES($1,$2,$3,$4,'PUBLISHED', now(), $5) "
            "ON CONFLICT (doc_id) DO UPDATE SET title=EXCLUDED.title, version=EXCLUDED.version, "
            "status='PUBLISHED', source_uri=EXCLUDED.source_uri",
            doc_id, tenant, title, version, "")
        if replace:
            await conn.execute("DELETE FROM policy_chunks WHERE tenant_id=$1 AND doc_id=$2 AND version=$3",
                               tenant, doc_id, version)
        for i, c in enumerate(chunks):
            emb = _vec_literal(vecs[i]) if vecs else None
            tc = token_counts(c["text"] or "")
            await conn.execute(
                "INSERT INTO policy_chunks(tenant_id,doc_id,version,clause_id,clause_group_id,"
                "is_exception,text,embedding_model,embedding,tokens,doc_len) "
                "VALUES($1,$2,$3,$4,$5,$6,$7,$8, CAST($9 AS vector), CAST($10 AS jsonb), $11) "
                "ON CONFLICT (tenant_id,clause_id,version) DO UPDATE SET text=EXCLUDED.text, "
                "clause_group_id=EXCLUDED.clause_group_id, is_exception=EXCLUDED.is_exception, "
                "embedding_model=EXCLUDED.embedding_model, embedding=EXCLUDED.embedding, "
                "tokens=EXCLUDED.tokens, doc_len=EXCLUDED.doc_len",
                tenant, doc_id, version, c["clause_id"], c["clause_group_id"], c["is_exception"], c["text"],
                settings.embedding_model or None, emb, json.dumps(tc, ensure_ascii=False), sum(tc.values()))
    print(f"  已入库 {len(chunks)} 条条款")
    return len(chunks)


async def ingest_clauses_jd(conn: asyncpg.Connection, settings: Settings, tenant: str, *,
                             replace: bool, do_embed: bool, dim: int) -> int:
    """从 policy/clauses_jd.json（真实语料单一事实源）入库文档与条款。

    与 --all（按 md 现切）不同：这里直接写入已冻结的 child chunk 及其 parent_id/
    logical_clause_id/heading_path/token_count，保证内存后端与 DB 后端读到同一批条款，
    且 Parent-Child 关联不丢失。
    """
    payload = json.loads(CLAUSES_JD.read_text(encoding="utf-8"))
    version = payload.get("version", library_version())
    docs = payload.get("documents", [])
    clauses = payload.get("clauses", [])
    if not clauses:
        print(f"[warn] {CLAUSES_JD} 无可入库条款")
        return 0
    vecs: dict[str, list[float]] = {}
    if do_embed:
        if not settings.embeddings_enabled:
            print("  [warn] 未配置 EMBEDDING_MODEL，跳过 embedding")
        else:
            embedded = await _embed(settings, [c["text"] for c in clauses])
            vecs = {c["clause_id"]: v for c, v in zip(clauses, embedded)}
            dim = len(embedded[0])
            await _ensure_dim(conn, dim)
            print(f"  已生成 {len(embedded)} 条 embedding（维度 {dim}）")
    doc_ids = sorted({c["doc_id"] for c in clauses})
    async with conn.transaction():
        if replace:
            await conn.execute(
                "DELETE FROM policy_chunks WHERE tenant_id=$1 AND doc_id = ANY($2::text[])", tenant, doc_ids)
        for d in docs:
            await conn.execute(
                "INSERT INTO policy_documents(doc_id,tenant_id,title,version,status,effective_from,source_uri) "
                "VALUES($1,$2,$3,$4,'PUBLISHED', now(), $5) "
                "ON CONFLICT (doc_id) DO UPDATE SET title=EXCLUDED.title, version=EXCLUDED.version, "
                "status='PUBLISHED', source_uri=EXCLUDED.source_uri",
                d["doc_id"], tenant, d.get("title", d["doc_id"]), d.get("version", version), "")
        for c in clauses:
            emb = _vec_literal(vecs[c["clause_id"]]) if vecs else None
            tc = token_counts(c["text"] or "")
            await conn.execute(
                "INSERT INTO policy_chunks(tenant_id,doc_id,version,clause_id,clause_group_id,is_exception,"
                "text,parent_id,logical_clause_id,heading_path,token_count,embedding_model,embedding,"
                "tokens,doc_len) "
                "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12, CAST($13 AS vector), CAST($14 AS jsonb), $15) "
                "ON CONFLICT (tenant_id,clause_id,version) DO UPDATE SET text=EXCLUDED.text, "
                "clause_group_id=EXCLUDED.clause_group_id, is_exception=EXCLUDED.is_exception, "
                "parent_id=EXCLUDED.parent_id, logical_clause_id=EXCLUDED.logical_clause_id, "
                "heading_path=EXCLUDED.heading_path, token_count=EXCLUDED.token_count, "
                "embedding_model=EXCLUDED.embedding_model, "
                "embedding=COALESCE(EXCLUDED.embedding, policy_chunks.embedding), "
                "tokens=EXCLUDED.tokens, doc_len=EXCLUDED.doc_len",
                tenant, c["doc_id"], c.get("version", version), c["clause_id"], c.get("clause_group_id"),
                bool(c.get("is_exception")), c["text"], c.get("parent_id"), c.get("logical_clause_id"),
                list(c.get("heading_path") or []), c.get("token_count"),
                settings.embedding_model or None, emb,
                json.dumps(tc, ensure_ascii=False), sum(tc.values()))
    print(f"  已入库 {len(docs)} 篇文档 / {len(clauses)} 条条款（version={version}）")
    return len(clauses)


async def rebuild_rule_clause_map(conn: asyncpg.Connection, tenant: str) -> int:
    """重建 policy_rule_clause_map：把每条规则引用的 clause 解析到真实条款/父块。

    规则写入后由真实语料解析闭包：命中条款（clause_id 或 logical_clause_id）-> RESOLVED，
    并回填 resolved_parent_id；未命中 -> MISSING（可被 check_rule_clause_links 直接发现）。"""
    exists = await conn.fetchval("SELECT to_regclass('public.policy_rule_clause_map')")
    if exists is None:
        return 0
    await conn.execute("DELETE FROM policy_rule_clause_map WHERE tenant_id=$1", tenant)
    rows = await conn.fetch(
        "INSERT INTO policy_rule_clause_map(tenant_id,rule_id,logical_clause_id,version,resolved_parent_id,status) "
        "SELECT r.tenant_id, r.rule_id, cid, r.version, "
        "       (SELECT ch.parent_id FROM policy_chunks ch "
        "         WHERE ch.tenant_id=r.tenant_id AND (ch.clause_id=cid OR ch.logical_clause_id=cid) "
        "         ORDER BY (ch.clause_id=cid) DESC LIMIT 1), "
        "       CASE WHEN EXISTS (SELECT 1 FROM policy_chunks ch WHERE ch.tenant_id=r.tenant_id "
        "                          AND (ch.clause_id=cid OR ch.logical_clause_id=cid)) "
        "            THEN 'RESOLVED' ELSE 'MISSING' END "
        "FROM policy_rules r, unnest(r.required_clause_ids) AS cid "
        "WHERE r.tenant_id=$1 "
        "ON CONFLICT (tenant_id,rule_id,logical_clause_id,version) DO UPDATE SET "
        "  resolved_parent_id=EXCLUDED.resolved_parent_id, status=EXCLUDED.status, updated_at=now() "
        "RETURNING status", tenant)
    resolved = sum(1 for r in rows if r["status"] == "RESOLVED")
    print(f"policy_rule_clause_map：{len(rows)} 条（RESOLVED={resolved}, MISSING={len(rows)-resolved}）")
    return len(rows)


async def main() -> None:
    ap = argparse.ArgumentParser(description="政策文档/规则入库")
    ap.add_argument("files", nargs="*", help="待入库文档（.md/.txt/.markdown）；用 --all 时忽略")
    ap.add_argument("--all", action="store_true", help="按 policy/rules.json 的 documents 清单入库全部文档")
    ap.add_argument("--rules", action="store_true", help="把 policy/rules.json 的规则写入 policy_rules")
    ap.add_argument("--from-clauses-jd", action="store_true",
                    help="从 policy/clauses_jd.json 入库真实语料（文档+条款，含 parent_id/logical_clause_id）")
    ap.add_argument("--doc-id", default=None)
    ap.add_argument("--title", default=None)
    ap.add_argument("--version", default=None, help="默认取 rules.json 的 version")
    ap.add_argument("--tenant", default="tenant_demo")
    ap.add_argument("--chunk-size", type=int, default=900)
    ap.add_argument("--overlap", type=int, default=150)
    ap.add_argument("--embed", action="store_true")
    ap.add_argument("--dim", type=int, default=None)
    ap.add_argument("--replace", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--database-url", default=None)
    args = ap.parse_args()

    settings = Settings()
    version = args.version or library_version()
    dim = args.dim or settings.embedding_dim

    # 预览模式：不连库
    if args.dry_run:
        if args.rules:
            rules = load_rules()
            print(f"rules.json: {len(rules)} 条规则")
            for r in rules:
                print(f"  {r.rule_id} p={r.priority} {r.decision} {r.reason_code} "
                      f"cap={r.amount_cap_minor} scope={r.scope} clauses={r.required_clause_ids}")
        if args.all:
            for d in documents():
                rel = d.get("file")
                p = REPO_ROOT / str(rel) if rel else None
                if p is not None and p.is_file():
                    preview(p.read_text(encoding="utf-8"), d["doc_id"], d.get("version", version),
                            args.chunk_size, args.overlap)
                else:
                    print(f"[skip] {d['doc_id']}：无对应 md 文件；真实语料请用 --from-clauses-jd / policy/clauses_jd.json")
        for path in args.files:
            p = pathlib.Path(path)
            if p.is_file():
                preview(p.read_text(encoding="utf-8"), args.doc_id or p.stem, version,
                        args.chunk_size, args.overlap)
        return

    dsn = _dsn(args.database_url or settings.database_url)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("SELECT set_config('app.tenant_id', $1, false)", args.tenant)

        if args.list or (not args.files and not args.all and not args.rules and not args.from_clauses_jd):
            await do_list(conn, args.tenant)
            return

        if args.rules:
            await upsert_rules(conn, args.tenant, args.replace)
            await rebuild_rule_clause_map(conn, args.tenant)

        if args.from_clauses_jd:
            await ingest_clauses_jd(conn, settings, args.tenant, replace=args.replace,
                                    do_embed=args.embed, dim=dim)

        if args.all:
            for d in documents():
                rel = d.get("file")
                p = REPO_ROOT / str(rel) if rel else None
                if p is None or not p.is_file():
                    print(f"[skip] {d['doc_id']}：无对应 md 文件；真实语料请用 --from-clauses-jd")
                    continue
                await ingest_doc(conn, settings, args.tenant, doc_id=d["doc_id"], title=d.get("title", d["doc_id"]),
                                 version=d.get("version", version), text=p.read_text(encoding="utf-8"),
                                 chunk_size=args.chunk_size, overlap=args.overlap, do_embed=args.embed,
                                 replace=args.replace, dim=dim)

        for path in args.files:
            p = pathlib.Path(path)
            if not p.is_file():
                print(f"[skip] 不是文件：{path}")
                continue
            await ingest_doc(conn, settings, args.tenant, doc_id=args.doc_id or p.stem,
                             title=args.title or p.stem, version=version, text=p.read_text(encoding="utf-8"),
                             chunk_size=args.chunk_size, overlap=args.overlap, do_embed=args.embed,
                             replace=args.replace, dim=dim)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())