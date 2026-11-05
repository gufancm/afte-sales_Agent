"""业务持久化：SQLAlchemy 实现 + 内存实现（测试）。
写命令与 Outbox 在同一事务内落库（§7.4.11/§18.10）。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.engine import system_transaction, tenant_transaction
from app.observability import metrics as M


def _uuid() -> str:
    return str(uuid.uuid4())


def _uuid_or_none(value: Any) -> str | None:
    """非 UUID 字符串（如内存测试的 "tk1"）转 None，避免写库类型错误。"""
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return None


def _ts(value: Any) -> Any:
    """ISO8601 字符串 -> 时区感知 datetime（asyncpg 的 timestamptz 不接受 str）。"""
    if value is None or isinstance(value, datetime):
        return value
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


class SqlPersistence:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.sf = session_factory

    # ---------------- 工单 / 消息 / 运行 ----------------
    async def create_ticket(self, *, tenant_id: str, user_id: str, order_id: str | None,
                            intent: str, status: str = "NEW",
                            workflow_version: str = "v3") -> dict:
        tid = _uuid()
        async with tenant_transaction(self.sf, tenant_id) as s:
            await s.execute(text(
                "INSERT INTO aftersale_tickets(ticket_id,tenant_id,user_id,order_id,intent,status,workflow_version) "
                "VALUES(:tid,:ten,:uid,:oid,:intent,:st,:wf)"),
                {"tid": tid, "ten": tenant_id, "uid": user_id, "oid": order_id, "intent": intent,
                 "st": status, "wf": workflow_version})
        M.TICKETS.inc()
        return {"ticket_id": tid, "status": status, "version": 0}

    async def get_ticket(self, *, tenant_id: str, ticket_id: str) -> dict | None:
        async with tenant_transaction(self.sf, tenant_id) as s:
            row = (await s.execute(text(
                "SELECT ticket_id,user_id,order_id,intent,status,version,event_sequence,active_run_id,workflow_version "
                "FROM aftersale_tickets WHERE ticket_id=:tid"), {"tid": ticket_id})).mappings().first()
        return dict(row) if row else None

    async def set_ticket_status(self, *, tenant_id: str, ticket_id: str, status: str) -> bool:
        """更新工单状态（投诉转人工等）；状态相同不递增版本，重复调用幂等。"""
        async with tenant_transaction(self.sf, tenant_id) as s:
            row = (await s.execute(text(
                "UPDATE aftersale_tickets SET status=:st, version=version+1 "
                "WHERE tenant_id=:ten AND ticket_id=:tid AND status<>:st RETURNING version"),
                {"st": status, "ten": tenant_id, "tid": ticket_id})).first()
        return True if row else False

    async def append_message(self, *, tenant_id: str, ticket_id: str, thread_id: str, role: str,
                             content: str, content_hash: str, idempotency_key: str | None,
                             client_message_id: str | None = None) -> str:
        mid = _uuid()
        async with tenant_transaction(self.sf, tenant_id) as s:
            await s.execute(text(
                "INSERT INTO ticket_messages(message_id,tenant_id,ticket_id,thread_id,role,content,content_hash,"
                "client_message_id,idempotency_key,metadata) "
                "VALUES(:mid,:ten,:tid,:thr,:role,:c,:h,:cmid,:ik,'{}'::jsonb) "
                "ON CONFLICT (tenant_id,idempotency_key) DO NOTHING"),
                {"mid": mid, "ten": tenant_id, "tid": ticket_id, "thr": thread_id, "role": role,
                 "c": content, "h": content_hash, "cmid": client_message_id, "ik": idempotency_key})
        return mid

    async def create_run(self, *, tenant_id: str, ticket_id: str, thread_id: str) -> str:
        rid = _uuid()
        async with tenant_transaction(self.sf, tenant_id) as s:
            await s.execute(text(
                "INSERT INTO ticket_runs(run_id,tenant_id,ticket_id,thread_id,status,version) "
                "VALUES(:rid,:ten,:tid,:thr,'RUNNING',0)"),
                {"rid": rid, "ten": tenant_id, "tid": ticket_id, "thr": thread_id})
            await s.execute(text(
                "UPDATE aftersale_tickets SET active_run_id=:rid, version=version+1 WHERE ticket_id=:tid"),
                {"rid": rid, "tid": ticket_id})
        return rid

    async def set_run_status(self, *, tenant_id: str, run_id: str, status: str) -> None:
        async with tenant_transaction(self.sf, tenant_id) as s:
            await s.execute(text("UPDATE ticket_runs SET status=:st, version=version+1 WHERE run_id=:rid"),
                            {"st": status, "rid": run_id})

    # ---------------- 事件（单工单有序游标，§18.6） ----------------
    async def append_ticket_event(self, *, tenant_id: str, ticket_id: str, event_type: str, payload: dict) -> int:
        async with tenant_transaction(self.sf, tenant_id) as s:
            seq = (await s.execute(text(
                "UPDATE aftersale_tickets SET event_sequence = event_sequence + 1 "
                "WHERE ticket_id=:tid RETURNING event_sequence"), {"tid": ticket_id})).scalar_one()
            await s.execute(text(
                "INSERT INTO ticket_events(event_id,tenant_id,ticket_id,ticket_sequence,event_type,payload) "
                "VALUES(:eid,:ten,:tid,:seq,:et,CAST(:p AS jsonb))"),
                {"eid": _uuid(), "ten": tenant_id, "tid": ticket_id, "seq": seq, "et": event_type, "p": _json(payload)})
        return seq

    async def list_events(self, *, tenant_id: str, ticket_id: str, after_sequence: int) -> list[dict]:
        async with tenant_transaction(self.sf, tenant_id) as s:
            rows = (await s.execute(text(
                "SELECT ticket_sequence,event_type,payload FROM ticket_events "
                "WHERE ticket_id=:tid AND ticket_sequence > :seq ORDER BY ticket_sequence"),
                {"tid": ticket_id, "seq": after_sequence})).mappings().all()
        return [{"seq": r["ticket_sequence"], "type": r["event_type"], "payload": r["payload"]} for r in rows]

    # ---------------- 记忆系统（短期跨轮 / 情景 / 语义） ----------------
    async def list_messages(self, *, tenant_id: str, ticket_id: str, limit: int = 40) -> list[dict]:
        async with tenant_transaction(self.sf, tenant_id) as s:
            rows = (await s.execute(text(
                "SELECT message_id,thread_id,role,content,message_seq,created_at FROM ticket_messages "
                "WHERE ticket_id=:tid ORDER BY message_seq DESC NULLS LAST, created_at DESC LIMIT :lim"),
                {"tid": ticket_id, "lim": int(limit)})).mappings().all()
        return [{"message_id": str(r["message_id"]), "thread_id": r["thread_id"], "role": r["role"],
                 "content": r["content"], "message_seq": r["message_seq"],
                 "created_at": r["created_at"].isoformat() if r["created_at"] else None}
                for r in reversed(rows)]

    async def get_conversation_summary(self, *, tenant_id: str, ticket_id: str) -> dict | None:
        async with tenant_transaction(self.sf, tenant_id) as s:
            row = (await s.execute(text(
                "SELECT summary,up_to_message_count FROM conversation_summaries WHERE ticket_id=:tid"),
                {"tid": ticket_id})).mappings().first()
        if not row:
            return None
        return {"summary": row["summary"], "up_to_message_count": int(row["up_to_message_count"] or 0)}

    async def upsert_conversation_summary(self, *, tenant_id: str, ticket_id: str, summary: str,
                                          up_to_message_count: int) -> None:
        async with tenant_transaction(self.sf, tenant_id) as s:
            await s.execute(text(
                "INSERT INTO conversation_summaries(tenant_id,ticket_id,summary,up_to_message_count,version) "
                "VALUES(:ten,:tid,:sm,:up,1) "
                "ON CONFLICT (tenant_id,ticket_id) DO UPDATE SET summary=:sm, up_to_message_count=:up, "
                "version=conversation_summaries.version+1, updated_at=now()"),
                {"ten": tenant_id, "tid": ticket_id, "sm": summary, "up": int(up_to_message_count)})

    async def insert_episodic_memory(self, **kw: Any) -> str:
        mid = _uuid()
        emb = kw.get("embedding")
        vec = ("[" + ",".join(str(x) for x in emb) + "]") if emb else None
        async with tenant_transaction(self.sf, kw["tenant_id"]) as s:
            await s.execute(text(
                "INSERT INTO episodic_memories(memory_id,tenant_id,user_id,ticket_id,intent,outcome_type,"
                "summary,embedding,metadata) VALUES(:mid,:ten,:uid,:tid,:intent,:ot,:sm,"
                "CAST(:vec AS vector),CAST(:meta AS jsonb))"),
                {"mid": mid, "ten": kw["tenant_id"], "uid": kw.get("user_id"),
                 "tid": _uuid_or_none(kw.get("ticket_id")), "intent": kw.get("intent"),
                 "ot": kw.get("outcome_type"), "sm": kw.get("summary"), "vec": vec,
                 "meta": _json(kw.get("metadata") or {})})
        return mid

    async def search_episodic_memories(self, *, tenant_id: str, user_id: str,
                                       query_embedding: list, top_k: int) -> list[dict]:
        vec = "[" + ",".join(str(x) for x in query_embedding) + "]"
        params: dict[str, Any] = {"ten": tenant_id, "vec": vec, "k": int(top_k)}
        scope = ""
        if user_id:
            scope = " AND (user_id = :uid OR user_id IS NULL)"
            params["uid"] = user_id
        async with tenant_transaction(self.sf, tenant_id) as s:
            rows = (await s.execute(text(
                "SELECT memory_id,ticket_id,intent,outcome_type,summary,metadata,"
                "  (1 - (embedding <=> CAST(:vec AS vector))) AS score "
                "FROM episodic_memories WHERE tenant_id=:ten AND embedding IS NOT NULL" + scope +
                " ORDER BY embedding <=> CAST(:vec AS vector) ASC LIMIT :k"), params)).mappings().all()
        return [{"memory_id": str(r["memory_id"]),
                 "ticket_id": str(r["ticket_id"]) if r["ticket_id"] else None,
                 "intent": r["intent"], "outcome_type": r["outcome_type"], "summary": r["summary"],
                 "metadata": r["metadata"], "score": float(r["score"] or 0.0)} for r in rows]

    async def get_user_profile(self, *, tenant_id: str, user_id: str) -> dict | None:
        async with tenant_transaction(self.sf, tenant_id) as s:
            row = (await s.execute(text(
                "SELECT profile FROM user_profiles WHERE tenant_id=:ten AND user_id=:uid"),
                {"ten": tenant_id, "uid": user_id})).mappings().first()
        return dict(row["profile"] or {}) if row else None

    async def upsert_user_profile(self, *, tenant_id: str, user_id: str, profile: dict) -> None:
        async with tenant_transaction(self.sf, tenant_id) as s:
            await s.execute(text(
                "INSERT INTO user_profiles(tenant_id,user_id,profile,version) "
                "VALUES(:ten,:uid,CAST(:p AS jsonb),1) "
                "ON CONFLICT (tenant_id,user_id) DO UPDATE SET profile=CAST(:p AS jsonb), "
                "version=user_profiles.version+1, updated_at=now()"),
                {"ten": tenant_id, "uid": user_id, "p": _json(profile)})

    # ---------------- 意图 / 审计 / LLM ----------------
    async def upsert_intent(self, *, tenant_id: str, ticket_id: str, intent: str,
                            expected_version: int | None = None) -> bool:
        """意图权威回写（§2.4.6）：以调用方读到的 version 做 CAS；返回是否真正写入。"""
        sql = ("UPDATE aftersale_tickets SET intent=:intent, version=version+1, updated_at=now() "
               "WHERE ticket_id=:tid AND intent <> :intent")
        params: dict[str, Any] = {"intent": intent, "tid": ticket_id}
        if expected_version is not None:
            sql += " AND version=:ver"
            params["ver"] = int(expected_version)
        sql += " RETURNING ticket_id"
        async with tenant_transaction(self.sf, tenant_id) as s:
            row = (await s.execute(text(sql), params)).first()
        return bool(row)

    async def record_policy_decision(self, **kw: Any) -> str:
        """落库真实裁决：后续 Finance/审批/审计引用同一 policy_decision_id（§4.9）。"""
        pid = kw.get("policy_decision_id") or _uuid()
        async with tenant_transaction(self.sf, kw["tenant_id"]) as s:
            await s.execute(text(
                "INSERT INTO policy_decisions(policy_decision_id,tenant_id,ticket_id,decision,reason_code,"
                "rule_ids,required_clause_ids,amount_cap_minor,policy_version,time_basis,as_of,scope,trace) "
                "VALUES(:pid,:ten,:tid,:dec,:rc,CAST(:rids AS text[]),CAST(:cids AS text[]),:cap,:pv,:tb,:asof,"
                "CAST(:sc AS jsonb),CAST(:trace AS jsonb)) ON CONFLICT (policy_decision_id) DO NOTHING"),
                {"pid": pid, "ten": kw["tenant_id"], "tid": _uuid_or_none(kw.get("ticket_id")),
                 "dec": kw["decision"], "rc": kw["reason_code"],
                 "rids": list(kw.get("rule_ids") or []), "cids": list(kw.get("required_clause_ids") or []),
                 "cap": kw.get("amount_cap_minor"), "pv": kw.get("policy_version"),
                 "tb": kw.get("time_basis"), "asof": _ts(kw.get("as_of")),
                 "sc": _json(kw.get("scope") or {}), "trace": _json(kw.get("trace") or {})})
        return pid

    async def list_command_statuses(self, *, tenant_id: str, command_ids: list[str]) -> list[dict]:
        """按 command_id 核验命令状态：ResultVerifier 的权威输入（§9.5）。"""
        ids = [c for c in (command_ids or []) if _uuid_or_none(c)]
        if not ids:
            return []
        async with tenant_transaction(self.sf, tenant_id) as s:
            rows = (await s.execute(text(
                "SELECT command_id::text AS command_id, action, status, last_error, "
                "       refund_request_id::text AS refund_request_id, plan_id::text AS plan_id "
                "FROM saga_commands WHERE tenant_id=:ten AND command_id = ANY(CAST(:ids AS uuid[]))"),
                {"ten": tenant_id, "ids": ids})).mappings().all()
        return [dict(r) for r in rows]

    async def record_llm_call(self, **kw: Any) -> None:
        cols = {"call_id": _uuid(), "tenant_id": kw.get("tenant_id"), "ticket_id": kw.get("ticket_id"),
                "run_id": kw.get("run_id"), "trace_id": kw.get("trace_id"), "node": kw.get("node", "unknown"),
                "model": kw.get("model", ""), "prompt_version": kw.get("prompt_version", ""),
                "request_hash": kw.get("request_hash"), "response_hash": kw.get("response_hash"),
                "input_tokens": kw.get("input_tokens"), "output_tokens": kw.get("output_tokens"),
                "latency_ms": kw.get("latency_ms"), "status": kw.get("status", "SUCCESS"),
                "error_code": kw.get("error_code")}
        async with tenant_transaction(self.sf, kw["tenant_id"]) as s:
            await s.execute(text(
                "INSERT INTO llm_calls(call_id,tenant_id,ticket_id,run_id,trace_id,node,model,prompt_version,"
                "request_hash,response_hash,input_tokens,output_tokens,latency_ms,status,error_code) "
                "VALUES(:call_id,:tenant_id,:ticket_id,:run_id,:trace_id,:node,:model,:prompt_version,"
                ":request_hash,:response_hash,:input_tokens,:output_tokens,:latency_ms,:status,:error_code)"), cols)
        if kw.get("ticket_id"):
            await self.append_ticket_event(tenant_id=kw["tenant_id"], ticket_id=kw["ticket_id"],
                                           event_type="llm_call", payload={"node": cols["node"], "status": cols["status"]})
        M.record_llm(node=cols["node"], status=cols["status"], latency_ms=cols["latency_ms"],
                     input_tokens=cols["input_tokens"], output_tokens=cols["output_tokens"])

    async def record_tool_call(self, **kw: Any) -> None:
        """只读工具调用审计（超时/重试/熔断/校验在 ToolGateway 完成，此处只落库）。"""
        tenant_id = kw.get("tenant_id")
        if not tenant_id:
            return
        cols = {"call_id": _uuid(), "tenant_id": tenant_id, "ticket_id": _uuid_or_none(kw.get("ticket_id")),
                "run_id": _uuid_or_none(kw.get("run_id")), "trace_id": kw.get("trace_id"),
                "node": kw.get("node"), "tool": kw.get("tool", ""), "status": kw.get("status", "SUCCESS"),
                "error_code": kw.get("error_code"), "attempts": kw.get("attempts"),
                "latency_ms": kw.get("latency_ms"), "critical": bool(kw.get("critical", True))}
        async with tenant_transaction(self.sf, tenant_id) as s:
            await s.execute(text(
                "INSERT INTO tool_calls(call_id,tenant_id,ticket_id,run_id,trace_id,node,tool,status,"
                "error_code,attempts,latency_ms,critical) "
                "VALUES(:call_id,:tenant_id,:ticket_id,:run_id,:trace_id,:node,:tool,:status,"
                ":error_code,:attempts,:latency_ms,:critical)"), cols)
        M.record_tool(tool=cols["tool"], status=cols["status"], latency_ms=cols["latency_ms"])

    async def append_audit(self, *, tenant_id: str, ticket_id: str, event: str, payload: dict, trace_id: str | None = None) -> None:
        async with tenant_transaction(self.sf, tenant_id) as s:
            await s.execute(text(
                "INSERT INTO agent_audit_events(event_id,tenant_id,ticket_id,trace_id,event_type,payload) "
                "VALUES(:eid,:ten,:tid,:tr,:et,CAST(:p AS jsonb))"),
                {"eid": _uuid(), "ten": tenant_id, "tid": ticket_id, "tr": trace_id, "et": event, "p": _json(payload)})

    # ---------------- 计划 / 风险 / 审批 ----------------
    async def create_plan_draft(self, *, tenant_id: str, ticket_id: str, plan_json: dict,
                                plan_hash: str) -> str:
        """规划阶段即落库计划草稿（DRAFT）：让 plan_id 前移，计划可追溯、审批/风控/命令引用同一 id。"""
        pid = _uuid()
        async with tenant_transaction(self.sf, tenant_id) as s:
            await s.execute(text(
                "INSERT INTO action_plans(plan_id,tenant_id,ticket_id,order_id,policy_decision_id,plan_hash,"
                "plan_json,order_version,status) "
                "VALUES(:pid,:ten,:tid,:oid,:pdd,:ph,CAST(:pj AS jsonb),:ov,'DRAFT')"),
                {"pid": pid, "ten": tenant_id, "tid": _uuid_or_none(ticket_id),
                 "oid": plan_json.get("plan", {}).get("order_id"), "pdd": plan_json.get("policy_decision_id"),
                 "ph": plan_hash, "pj": _json(plan_json), "ov": plan_json.get("order_version")})
        return pid

    async def freeze_plan(self, *, tenant_id: str, ticket_id: str, plan_json: dict, plan_hash: str,
                          status: str, plan_id: str | None = None) -> str:
        """冻结计划。传入 plan_id 时**原地更新**草稿（保持同一 plan_id），否则新建。"""
        pid = plan_id or _uuid()
        params = {"pid": pid, "ten": tenant_id, "tid": _uuid_or_none(ticket_id),
                  "oid": plan_json.get("plan", {}).get("order_id"), "pdd": plan_json.get("policy_decision_id"),
                  "ph": plan_hash, "pj": _json(plan_json), "qid": (plan_json.get("quote") or {}).get("quote_id"),
                  "qh": (plan_json.get("quote") or {}).get("quote_hash"),
                  "ov": plan_json.get("order_version"), "st": status}
        async with tenant_transaction(self.sf, tenant_id) as s:
            if plan_id:
                await s.execute(text(
                    "UPDATE action_plans SET plan_json=CAST(:pj AS jsonb), plan_hash=:ph, quote_id=:qid, "
                    "quote_hash=:qh, order_version=:ov, status=:st "
                    "WHERE tenant_id=:ten AND plan_id=CAST(:pid AS uuid)"), params)
            else:
                await s.execute(text(
                    "INSERT INTO action_plans(plan_id,tenant_id,ticket_id,order_id,policy_decision_id,plan_hash,"
                    "plan_json,quote_id,quote_hash,order_version,status) "
                    "VALUES(:pid,:ten,:tid,:oid,:pdd,:ph,CAST(:pj AS jsonb),:qid,:qh,:ov,:st)"), params)
        return pid

    async def record_risk_decision(self, *, tenant_id: str, ticket_id: str, plan_hash: str | None,
                                   quote_id: str | None, decision: str, reason_codes: list[str]) -> str:
        rid = _uuid()
        async with tenant_transaction(self.sf, tenant_id) as s:
            await s.execute(text(
                "INSERT INTO risk_decisions(risk_decision_id,tenant_id,ticket_id,plan_hash,quote_id,decision,reason_codes) "
                "VALUES(:rid,:ten,:tid,:ph,:qid,:d,:rc)"),
                {"rid": rid, "ten": tenant_id, "tid": ticket_id, "ph": plan_hash, "qid": quote_id,
                 "d": decision, "rc": reason_codes})
        return rid

    async def ensure_approval_task(self, *, tenant_id: str, ticket_id: str, plan_id: str | None,
                                   plan_hash: str | None, quote_id: str | None, expected_version: str | None) -> dict:
        """幂等返回审批任务；`created` 表示本次是否真的新建。

        HITL 恢复会重放 `hitl_review`（`interrupt()` 之前的代码），调用方据此避免重复落审计。
        """
        aid = _uuid()
        async with tenant_transaction(self.sf, tenant_id) as s:
            row = (await s.execute(text(
                "INSERT INTO approval_tasks(approval_id,tenant_id,ticket_id,plan_id,plan_hash,quote_id,status,expires_at) "
                "VALUES(:aid,:ten,:tid,:pid,:ph,:qid,'PENDING', now() + interval '24 hours') "
                "ON CONFLICT (tenant_id,ticket_id,plan_hash) DO NOTHING "
                "RETURNING approval_id,plan_hash,status,expires_at"),
                {"aid": aid, "ten": tenant_id, "tid": ticket_id, "pid": plan_id, "ph": plan_hash,
                 "qid": quote_id})).mappings().first()
            created = row is not None
            if row is None:  # 已存在（本节点被恢复重放）：取回既有任务，不再新建
                row = (await s.execute(text(
                    "SELECT approval_id,plan_hash,status,expires_at FROM approval_tasks "
                    "WHERE tenant_id=:ten AND ticket_id=:tid AND plan_hash=:ph"),
                    {"ten": tenant_id, "tid": ticket_id, "ph": plan_hash})).mappings().first()
        if row is None:  # 并发极端：插入冲突但查询未命中 -> 返回确定性占位，绝不重复建
            return {"approval_id": aid, "plan_hash": plan_hash, "status": "PENDING", "created": False}
        return {"approval_id": str(row["approval_id"]), "plan_hash": row["plan_hash"],
                "status": row["status"], "expires_at": row["expires_at"].isoformat(), "created": created}

    async def load_and_verify_approval(self, *, approval_id: str, tenant_id: str) -> dict:
        async with tenant_transaction(self.sf, tenant_id) as s:
            row = (await s.execute(text(
                "SELECT approval_id,status,plan_hash,ticket_id,(expires_at <= now()) AS expired "
                "FROM approval_tasks WHERE approval_id=:aid"),
                {"aid": approval_id})).mappings().first()
        if not row:
            return {"approval_id": approval_id, "status": "REJECTED", "expired": False}
        return {"approval_id": str(row["approval_id"]), "status": row["status"],
                "plan_hash": row["plan_hash"], "ticket_id": str(row["ticket_id"]),
                "expired": bool(row["expired"])}

    async def decide_approval(self, *, tenant_id: str, approval_id: str, decision: str, reviewer_id: str,
                              reviewer_role: str | None = None, comment: str | None = None,
                              allowed_roles: list[str] | None = None) -> None:
        """审批决定：可校验审批人角色；只允许 PENDING 且未过期；记录审批人与意见。"""
        roles = [r.strip().lower() for r in (allowed_roles or []) if r and r.strip()]
        if roles and (reviewer_role or "").strip().lower() not in roles:
            raise PermissionError("REVIEWER_ROLE_NOT_ALLOWED")
        async with tenant_transaction(self.sf, tenant_id) as s:
            await s.execute(text(
                "UPDATE approval_tasks SET status=:d, reviewer_id=:rv, reviewer_role=:rr, decision_comment=:cm "
                "WHERE approval_id=:aid AND status='PENDING' AND expires_at > now()"),
                {"d": decision, "aid": approval_id, "rv": reviewer_id, "rr": reviewer_role, "cm": comment})
        M.APPROVALS.labels(decision=str(decision)).inc()

    # ---------------- 退款 ----------------
    async def create_refund_request(self, *, tenant_id: str, ticket_id: str, order_id: str | None,
                                    policy_decision_id: str | None, currency: str, idempotency_key: str,
                                    payload_hash: str) -> str:
        rid = _uuid()
        async with tenant_transaction(self.sf, tenant_id) as s:
            row = (await s.execute(text(
                "INSERT INTO refund_requests(refund_request_id,tenant_id,ticket_id,order_id,policy_decision_id,"
                "idempotency_key,payload_hash,amount_minor,currency,status) "
                "VALUES(:rid,:ten,:tid,:oid,:pdd,:ik,:ph,0,:cur,'CREATED') "
                "ON CONFLICT (tenant_id,idempotency_key) DO UPDATE SET payload_hash=refund_requests.payload_hash "
                "RETURNING refund_request_id"),
                {"rid": rid, "ten": tenant_id, "tid": ticket_id, "oid": order_id, "pdd": policy_decision_id,
                 "ik": idempotency_key, "ph": payload_hash, "cur": currency})).mappings().first()
        return str(row["refund_request_id"])

    async def insert_quote(self, *, tenant_id: str, refund_request_id: str, quote: dict) -> None:
        async with tenant_transaction(self.sf, tenant_id) as s:
            await s.execute(text(
                "INSERT INTO refund_quotes(quote_id,tenant_id,refund_request_id,policy_decision_id,order_version,"
                "policy_version,amount_minor,currency,breakdown,quote_hash,expires_at,reservation_reference,"
                "reservation_expires_at) "
                "VALUES(:qid,:ten,:rrid,:pdd,:ov,:pv,:amt,:cur,CAST(:bk AS jsonb),:qh,:exp,:rref,:rexp)"),
                {"qid": quote["quote_id"], "ten": tenant_id, "rrid": refund_request_id,
                 "pdd": quote["policy_decision_id"], "ov": quote["order_version"],
                 "pv": quote.get("policy_version", ""), "amt": quote["amount_minor"], "cur": quote["currency"],
                 "bk": _json(quote.get("breakdown", {})), "qh": quote["quote_hash"], "exp": _ts(quote["expires_at"]),
                 "rref": quote.get("reservation_reference"), "rexp": _ts(quote.get("reservation_expires_at"))})
            await s.execute(text(
                "UPDATE refund_requests SET current_quote_id=:qid, amount_minor=:amt, status='QUOTED' WHERE refund_request_id=:rrid"),
                {"qid": quote["quote_id"], "amt": quote["amount_minor"], "rrid": refund_request_id})

    async def mark_refund_authorized(self, *, tenant_id: str, refund_request_id: str, approval_id: str | None, risk_decision_id: str | None) -> None:
        async with tenant_transaction(self.sf, tenant_id) as s:
            await s.execute(text(
                "UPDATE refund_requests SET status='AUTHORIZED', approval_id=:aid, risk_decision_id=:rid "
                "WHERE refund_request_id=:rrid AND status IN ('QUOTED','CREATED')"),
                {"aid": approval_id, "rid": risk_decision_id, "rrid": refund_request_id})

    # ---------------- 命令 / Outbox（同事务） ----------------
    async def enqueue_command(self, **kw: Any) -> str:
        cid = _uuid()
        payload = kw.get("payload") or {}
        async with tenant_transaction(self.sf, kw["tenant_id"]) as s:
            await s.execute(text(
                "INSERT INTO saga_commands(command_id,tenant_id,ticket_id,run_id,operation_id,idempotency_key,"
                "payload_hash,action,refund_request_id,order_id,quote_id,quote_hash,waybill_no,sku_ids,"
                "authorization_ref,payload,plan_id,status,attempt_count,priority) "
                "VALUES(:cid,:ten,:tid,:rid,:opid,:ik,:ph,:act,:rrid,:boid,:qid,:qh,:wbn,CAST(:skus AS text[]),"
                ":auth,CAST(:pl AS jsonb),:pid,'PREPARED',0,:pri) "
                "ON CONFLICT (tenant_id,idempotency_key) DO NOTHING"),
                {"cid": cid, "ten": kw["tenant_id"], "tid": kw["ticket_id"], "rid": kw.get("run_id"),
                 "opid": _uuid(),
                 "ik": kw.get("idempotency_key")
                 or f"{kw.get('authorization_ref') or kw['ticket_id']}:{kw['action']}",
                 "ph": kw["payload_hash"], "act": kw["action"],
                 "rrid": kw.get("refund_request_id"), "boid": kw.get("order_id"),
                 "qid": kw.get("quote_id"), "qh": kw.get("quote_hash"), "wbn": kw.get("waybill_no"),
                 "skus": list(kw.get("sku_ids") or []), "auth": kw.get("authorization_ref"),
                 "pid": _uuid_or_none(kw.get("plan_id")),
                 "pri": int(kw.get("priority", 100)),
                 "pl": _json(payload)})
            await s.execute(text(
                "INSERT INTO outbox_events(event_id,tenant_id,aggregate_id,event_type,payload,status) "
                "VALUES(:eid,:ten,:agg,:et,CAST(:p AS jsonb),'PENDING')"),
                {"eid": _uuid(), "ten": kw["tenant_id"], "agg": cid, "et": "SAGA_DISPATCH",
                 "p": _json({"command_id": cid, "action": kw["action"], "quote_id": kw.get("quote_id")})})
        M.COMMANDS.labels(action=str(kw.get("action") or "unknown")).inc()
        return cid

    # ---------------- Worker 支撑（系统事务） ----------------
    async def claim_next_command(self) -> dict | None:
        async with system_transaction(self.sf) as s:
            row = (await s.execute(text(
                "UPDATE saga_commands SET status='CLAIMED', lease_until=now()+interval '60 seconds', "
                "attempt_count=attempt_count+1 "
                "WHERE command_id = (SELECT command_id FROM saga_commands "
                "  WHERE status='PREPARED' AND (next_retry_at IS NULL OR next_retry_at<=now()) "
                "  ORDER BY priority ASC, created_at ASC FOR UPDATE SKIP LOCKED LIMIT 1) "
                "RETURNING command_id::text,tenant_id,ticket_id::text,run_id::text,action,"
                "refund_request_id::text,order_id,quote_id::text,quote_hash,waybill_no,sku_ids,"
                "authorization_ref,idempotency_key,payload_hash,payload,plan_id::text,attempt_count"))).mappings().first()
        return dict(row) if row else None

    async def mark_command_result(self, *, command_id: str, tenant_id: str, result: dict) -> str:
        status = _map_result_status(result.get("status"))
        async with system_transaction(self.sf) as s:
            await s.execute(text(
                "UPDATE saga_commands SET status=:st, lease_until=NULL, last_error=:err "
                "WHERE command_id=:cid"),
                {"st": status, "err": result.get("error"), "cid": command_id})
            if status == "UNKNOWN":
                await s.execute(text(
                    "INSERT INTO reconciliation_tasks(task_id,tenant_id,ticket_id,refund_request_id,"
                    "reason,status,command_id,max_attempts,next_run_at,deadline) "
                    "SELECT :tid,:ten,ticket_id,refund_request_id,'UNKNOWN_COMMAND','PENDING',command_id,"
                    "10, now(), now()+interval '24 hours' "
                    "FROM saga_commands WHERE command_id=:cid"),
                    {"tid": _uuid(), "ten": tenant_id, "cid": command_id})
        M.COMMAND_RESULTS.labels(status=str(status)).inc()
        return status

    async def write_inbox(self, *, tenant_id: str, source: str, dedup_key: str, payload: dict) -> bool:
        async with system_transaction(self.sf) as s:
            row = (await s.execute(text(
                "INSERT INTO inbox_events(event_id,tenant_id,source,dedup_key,payload) "
                "VALUES(:eid,:ten,:src,:dk,CAST(:p AS jsonb)) "
                "ON CONFLICT (tenant_id,source,dedup_key) DO NOTHING RETURNING event_id"),
                {"eid": _uuid(), "ten": tenant_id, "src": source, "dk": dedup_key, "p": _json(payload)})).first()
        return bool(row)

    async def claim_due_reconciliation(self) -> dict | None:
        async with system_transaction(self.sf) as s:
            row = (await s.execute(text(
                "UPDATE reconciliation_tasks SET status='RUNNING', attempt_count=attempt_count+1 "
                "WHERE task_id = (SELECT task_id FROM reconciliation_tasks WHERE status='PENDING' "
                "  AND next_run_at<=now() ORDER BY next_run_at FOR UPDATE SKIP LOCKED LIMIT 1) "
                "RETURNING task_id::text,tenant_id,ticket_id::text,refund_request_id::text,command_id::text,"
                "attempt_count,max_attempts,reason"))).mappings().first()
        return dict(row) if row else None

    async def complete_reconciliation(self, *, task_id: str, status: str, next_run_at_seconds: int = 0) -> None:
        async with system_transaction(self.sf) as s:
            await s.execute(text(
                "UPDATE reconciliation_tasks SET status=:st, next_run_at=now()+ (:sec || ' seconds')::interval "
                "WHERE task_id=:tid"), {"st": status, "sec": str(next_run_at_seconds), "tid": task_id})
        M.RECONCILIATION.labels(status=str(status)).inc()

    async def reconcile_command_result(self, *, command_id: str, result: dict) -> str | None:
        """权威对账终态收敛：只允许 UNKNOWN 命令按 Finance 权威结果转 CONFIRMED。"""
        if result.get("status") not in ("SUCCEEDED", "SETTLED"):
            return None
        async with system_transaction(self.sf) as s:
            row = await s.execute(text(
                "UPDATE saga_commands SET status='CONFIRMED', lease_until=NULL, last_error=NULL "
                "WHERE command_id=:cid AND status='UNKNOWN' RETURNING 1"),
                {"cid": command_id})
            return "CONFIRMED" if row.first() else None

    # ---------------- 对账 ----------------
    async def create_reconciliation_task(self, *, tenant_id: str, ticket_id: str, refund_request_id: str | None,
                                         reason: str, command_id: str | None = None) -> str:
        """幂等：同一 (tenant, ticket, reason) 已有未完成对账任务时复用，不重复建（§18.10）。"""
        tid = _uuid()
        async with tenant_transaction(self.sf, tenant_id) as s:
            row = (await s.execute(text(
                "INSERT INTO reconciliation_tasks(task_id,tenant_id,ticket_id,refund_request_id,command_id,reason,"
                "status,max_attempts,next_run_at,deadline) "
                "SELECT :tid,:ten,:tk,:rrid,:cid,:r,'PENDING',10, now(), now() + interval '24 hours' "
                "WHERE NOT EXISTS (SELECT 1 FROM reconciliation_tasks WHERE tenant_id=:ten AND ticket_id=:tk "
                "                  AND reason=:r AND status IN ('PENDING','RUNNING')) "
                "RETURNING task_id::text"), {"tid": tid, "ten": tenant_id, "tk": ticket_id,
                                             "rrid": refund_request_id, "cid": command_id, "r": reason})).first()
            if row:
                return str(row[0])
            existing = (await s.execute(text(
                "SELECT task_id::text FROM reconciliation_tasks WHERE tenant_id=:ten AND ticket_id=:tk "
                "AND reason=:r AND status IN ('PENDING','RUNNING') ORDER BY created_at DESC LIMIT 1"),
                {"ten": tenant_id, "tk": ticket_id, "r": reason})).first()
        return str(existing[0]) if existing else tid

    async def ensure_policy_review_task(self, *, tenant_id: str, ticket_id: str, thread_id: str | None,
                                        policy_version: str | None, reason_code: str,
                                        retrieval_bundle: dict, facts_snapshot: dict) -> str:
        """幂等创建 V4 后台政策核实任务；与 HITL 风险审批分离。"""
        tid = _uuid()
        async with tenant_transaction(self.sf, tenant_id) as s:
            row = (await s.execute(text(
                "INSERT INTO policy_review_tasks(task_id,tenant_id,ticket_id,thread_id,workflow_version,"
                "policy_version,reason_code,retrieval_bundle,facts_snapshot,status) "
                "VALUES(:tid,:ten,:tk,:thr,'v4',:pv,:rc,CAST(:rb AS jsonb),CAST(:fs AS jsonb),'PENDING') "
                "ON CONFLICT (tenant_id,ticket_id,reason_code,policy_version) DO UPDATE "
                "SET updated_at=now() RETURNING task_id::text"),
                {"tid": tid, "ten": tenant_id, "tk": _uuid_or_none(ticket_id), "thr": thread_id,
                 "pv": policy_version, "rc": reason_code, "rb": _json(retrieval_bundle),
                 "fs": _json(facts_snapshot)})).first()
        return str(row[0]) if row else tid


    async def reclaim_expired_commands(self, *, max_attempts: int = 3) -> list[dict]:
        """回收租约过期的 CLAIMED 命令：置 UNKNOWN 并建对账任务（绝不盲重试，§9.8/§18.10）。

        命令发放写操作后 Worker 崩溃 -> 我们无法确认外部是否成功，只能转 UNKNOWN 交对账收敛。
        """
        async with system_transaction(self.sf) as s:
            result = await s.execute(text(
                "UPDATE saga_commands SET status='UNKNOWN', lease_until=NULL, last_error='LEASE_EXPIRED' "
                "WHERE status='CLAIMED' AND lease_until IS NOT NULL AND lease_until < now() "
                "RETURNING command_id::text, tenant_id, ticket_id::text, refund_request_id::text"))
            rows = result.mappings().all()
            out = [dict(r) for r in rows]
            for r in out:
                # 把命令上的 refund_request_id 一并带入对账任务，Worker 才能回查 Finance 权威状态
                await s.execute(text(
                    "INSERT INTO reconciliation_tasks(task_id,tenant_id,ticket_id,refund_request_id,command_id,"
                    "reason,status,max_attempts,next_run_at,deadline) "
                    "SELECT :tid, c.tenant_id, c.ticket_id, c.refund_request_id, c.command_id, "
                    "'LEASE_EXPIRED','PENDING',10, now(), now() + interval '24 hours' "
                    "FROM saga_commands c WHERE c.command_id=:cid"),
                    {"tid": _uuid(), "cid": r["command_id"]})
        return out

    async def escalate_expired_approvals(self) -> list[dict]:
        """SLA：PENDING 且已过期的审批任务置 EXPIRED（超时升级为人工兜底）。"""
        async with system_transaction(self.sf) as s:
            result = await s.execute(text(
                "UPDATE approval_tasks SET status='EXPIRED' "
                "WHERE status='PENDING' AND expires_at < now() "
                "RETURNING approval_id::text, tenant_id, ticket_id::text, plan_hash"))
            rows = result.mappings().all()
        return [dict(r) for r in rows]

    async def claim_due_reconciliation_command(self, *, task_id: str) -> dict | None:
        async with system_transaction(self.sf) as s:
            row = (await s.execute(text(
                "SELECT command_id::text FROM reconciliation_tasks WHERE task_id=:tid"),
                {"tid": task_id})).mappings().first()
        return dict(row) if row else None


    async def mark_outbox_published(self, *, aggregate_id: str) -> None:
        """Outbox relay：命令被 Worker 领取/派发后，把对应 outbox 事件标记为 PUBLISHED。"""
        async with system_transaction(self.sf) as s:
            await s.execute(text(
                "UPDATE outbox_events SET status='PUBLISHED' "
                "WHERE aggregate_id=:agg AND status='PENDING'"), {"agg": aggregate_id})

    async def claim_pending_outbox(self, *, limit: int = 50) -> list[dict]:
        """运维可观测：当前待 relay 的 outbox 事件（供 /metrics 或巡检）。"""
        async with system_transaction(self.sf) as s:
            rows = (await s.execute(text(
                "SELECT event_id::text,tenant_id,aggregate_id::text,event_type "
                "FROM outbox_events WHERE status='PENDING' ORDER BY created_at LIMIT :lim"),
                {"lim": int(limit)})).mappings().all()
        return [dict(r) for r in rows]

    async def cancel_command(self, *, command_id: str, tenant_id: str, reason: str) -> bool:
        """取消尚未派发的命令（补偿/撤销入口）：仅 PREPARED 可取消，已派发一律走对账。"""
        async with tenant_transaction(self.sf, tenant_id) as s:
            row = (await s.execute(text(
                "UPDATE saga_commands SET status='CANCELLED', last_error=:r "
                "WHERE command_id=:cid AND status='PREPARED' RETURNING command_id"),
                {"cid": command_id, "r": reason})).first()
        return bool(row)


def _map_result_status(s: str | None) -> str:
    return {
        'SUCCEEDED': 'CONFIRMED', 'MANUAL': 'CONFIRMED', 'UNSUPPORTED': 'FAILED_CONFIRMED',
        'ACCEPTED': 'IN_FLIGHT', 'REQUESTED': 'IN_FLIGHT', 'SUBMITTED': 'IN_FLIGHT',
        'HELD': 'CONFIRMED',
    }.get(s or '', 'UNKNOWN')


def _json(value: Any) -> str:
    from app.domain.canonical import canonical_json
    return canonical_json(value)
