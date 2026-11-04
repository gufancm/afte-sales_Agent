"""内存持久化（测试 / 无 DB 本地运行）。接口与 SqlPersistence 一致。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from app.observability import metrics as M


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class InMemoryPersistence:
    def __init__(self) -> None:
        self.tickets: dict[str, dict] = {}
        self.messages: dict[str, list] = {}
        self.runs: dict[str, dict] = {}
        self.events: dict[str, list] = {}
        self.llm_calls: list[dict] = []
        self.audits: list[dict] = []
        self.plans: dict[str, dict] = {}
        self.risks: dict[str, dict] = {}
        self.approvals: dict[str, dict] = {}
        self.refund_requests: dict[str, dict] = {}
        self.quotes: list[dict] = []
        self.commands: list[dict] = []
        self.outbox_published: list[str] = []
        self.reconciliations: list[dict] = []
        self.policy_review_tasks: list[dict] = []
        self.inbox: list[dict] = []
        self.policy_decisions: list[dict] = []
        self.tool_calls: list[dict] = []
        self.conversation_summaries: dict[str, dict] = {}
        self.episodic_memories: list[dict] = []
        self.user_profiles: dict[tuple[str, str], dict] = {}

    async def create_ticket(self, *, tenant_id, user_id, order_id, intent, status="NEW",
                            workflow_version="v3"):
        tid = _uuid()
        self.tickets[tid] = {"ticket_id": tid, "tenant_id": tenant_id, "user_id": user_id, "order_id": order_id,
                             "intent": intent, "status": status, "version": 0, "event_sequence": 0,
                             "active_run_id": None, "workflow_version": workflow_version}
        self.messages[tid] = []
        self.events[tid] = []
        M.TICKETS.inc()
        return {"ticket_id": tid, "status": status, "version": 0}

    async def get_ticket(self, *, tenant_id, ticket_id):
        return self.tickets.get(ticket_id)

    async def set_ticket_status(self, *, tenant_id, ticket_id, status):
        """更新工单状态（投诉转人工等）。幂等：状态相同不递增版本。"""
        t = self.tickets.get(ticket_id)
        if not t or t.get("tenant_id") != tenant_id:
            return False
        if t.get("status") == status:
            return True
        t["status"] = status
        t["version"] = int(t.get("version") or 0) + 1
        return True

    async def append_message(self, *, tenant_id, ticket_id, thread_id, role, content, content_hash,
                             idempotency_key, client_message_id=None):
        mid = _uuid()
        bucket = self.messages.setdefault(ticket_id, [])
        seq = (bucket[-1]["message_seq"] + 1) if bucket else 1
        bucket.append({"message_id": mid, "message_seq": seq, "thread_id": thread_id, "role": role,
                       "content": content, "idempotency_key": idempotency_key,
                       "client_message_id": client_message_id, "created_at": _now()})
        return mid

    async def create_run(self, *, tenant_id, ticket_id, thread_id):
        rid = _uuid()
        self.runs[rid] = {"run_id": rid, "ticket_id": ticket_id, "status": "RUNNING"}
        if ticket_id in self.tickets:
            self.tickets[ticket_id]["active_run_id"] = rid
        return rid

    async def set_run_status(self, *, tenant_id, run_id, status):
        if run_id in self.runs:
            self.runs[run_id]["status"] = status

    async def append_ticket_event(self, *, tenant_id, ticket_id, event_type, payload):
        t = self.tickets.setdefault(ticket_id, {"event_sequence": 0})
        t["event_sequence"] = t.get("event_sequence", 0) + 1
        self.events.setdefault(ticket_id, []).append(
            {"seq": t["event_sequence"], "type": event_type, "payload": payload})
        return t["event_sequence"]

    async def list_events(self, *, tenant_id, ticket_id, after_sequence):
        return [e for e in self.events.get(ticket_id, []) if e["seq"] > after_sequence]

    # ---------------- 记忆系统（短期跨轮 / 情景 / 语义） ----------------
    async def list_messages(self, *, tenant_id, ticket_id, limit=40):
        msgs = self.messages.get(ticket_id, [])
        return [dict(m) for m in msgs[-int(limit):]]

    async def get_conversation_summary(self, *, tenant_id, ticket_id):
        rec = self.conversation_summaries.get(ticket_id)
        return dict(rec) if rec else None

    async def upsert_conversation_summary(self, *, tenant_id, ticket_id, summary, up_to_message_count):
        self.conversation_summaries[ticket_id] = {"summary": summary,
                                                  "up_to_message_count": int(up_to_message_count)}
        return None

    async def insert_episodic_memory(self, **kw):
        rec = {"memory_id": _uuid(), "created_at": _now(), **kw}
        self.episodic_memories.append(rec)
        return rec["memory_id"]

    async def search_episodic_memories(self, *, tenant_id, user_id, query_embedding, top_k):
        from app.memory.service import _cosine
        out = []
        for m in self.episodic_memories:
            if m.get("tenant_id") != tenant_id:
                continue
            if user_id and m.get("user_id") and m.get("user_id") != user_id:
                continue
            emb = m.get("embedding")
            if not emb:
                continue
            out.append({"memory_id": m.get("memory_id"), "ticket_id": m.get("ticket_id"),
                        "intent": m.get("intent"), "outcome_type": m.get("outcome_type"),
                        "summary": m.get("summary"), "metadata": m.get("metadata"),
                        "score": _cosine(query_embedding, emb)})
        out.sort(key=lambda r: r["score"], reverse=True)
        return out[:int(top_k)]

    async def get_user_profile(self, *, tenant_id, user_id):
        rec = self.user_profiles.get((tenant_id, user_id))
        return dict(rec) if rec else None

    async def upsert_user_profile(self, *, tenant_id, user_id, profile):
        self.user_profiles[(tenant_id, user_id)] = dict(profile)
        return None

    async def upsert_intent(self, *, tenant_id, ticket_id, intent, expected_version=None):
        t = self.tickets.get(ticket_id)
        if not t:
            return False
        if t.get("intent") == intent:
            return False
        if expected_version is not None and int(t.get("version", 0)) != int(expected_version):
            return False
        t["intent"] = intent
        t["version"] = int(t.get("version", 0)) + 1
        return True

    async def record_policy_decision(self, **kw):
        pid = kw.get("policy_decision_id") or _uuid()
        self.policy_decisions.append({**kw, "policy_decision_id": pid})
        return pid

    async def list_command_statuses(self, *, tenant_id, command_ids):
        ids = set(command_ids or [])
        return [{"command_id": c["command_id"], "action": c.get("action"), "status": c.get("status"),
                 "last_error": c.get("last_error"), "refund_request_id": c.get("refund_request_id"),
                 "plan_id": c.get("plan_id")} for c in self.commands if c["command_id"] in ids]

    async def record_llm_call(self, **kw):
        self.llm_calls.append(kw)
        M.record_llm(node=kw.get("node"), status=kw.get("status", "SUCCESS"), latency_ms=kw.get("latency_ms"),
                     input_tokens=kw.get("input_tokens"), output_tokens=kw.get("output_tokens"))
        if kw.get("ticket_id"):
            await self.append_ticket_event(tenant_id=kw["tenant_id"], ticket_id=kw["ticket_id"],
                                           event_type="llm_call", payload={"node": kw.get("node")})

    async def record_tool_call(self, **kw):
        self.tool_calls.append(kw)
        M.record_tool(tool=kw.get("tool"), status=kw.get("status", "SUCCESS"), latency_ms=kw.get("latency_ms"))

    async def append_audit(self, *, tenant_id, ticket_id, event, payload, trace_id=None):
        self.audits.append({"ticket_id": ticket_id, "event": event, "payload": payload})

    async def create_plan_draft(self, *, tenant_id, ticket_id, plan_json, plan_hash):
        pid = _uuid()
        self.plans[pid] = {"plan_id": pid, "plan_hash": plan_hash, "plan_json": plan_json, "status": "DRAFT"}
        return pid

    async def freeze_plan(self, *, tenant_id, ticket_id, plan_json, plan_hash, status, plan_id=None):
        pid = plan_id or _uuid()
        self.plans[pid] = {"plan_id": pid, "plan_hash": plan_hash, "plan_json": plan_json, "status": status}
        return pid

    async def record_risk_decision(self, *, tenant_id, ticket_id, plan_hash, quote_id, decision, reason_codes):
        rid = _uuid()
        self.risks[rid] = {"risk_decision_id": rid, "decision": decision, "reason_codes": reason_codes}
        return rid

    async def ensure_approval_task(self, *, tenant_id, ticket_id, plan_id, plan_hash, quote_id, expected_version):
        key = (tenant_id, plan_hash)
        for a in self.approvals.values():
            if (a["tenant_id"], a["plan_hash"]) == key:
                return {**a, "created": False}
        aid = _uuid()
        rec = {"approval_id": aid, "tenant_id": tenant_id, "ticket_id": ticket_id, "plan_id": plan_id,
               "plan_hash": plan_hash, "quote_id": quote_id, "status": "PENDING", "expires_at": "2999-01-01T00:00:00Z"}
        self.approvals[aid] = rec
        return {**rec, "created": True}

    async def load_and_verify_approval(self, *, approval_id, tenant_id):
        a = self.approvals.get(approval_id)
        if not a:
            return {"approval_id": approval_id, "status": "REJECTED", "expired": False}
        exp = a.get("expires_at")
        expired = bool(exp and str(exp) < _now()) if exp else False
        return {"approval_id": approval_id, "status": a.get("status", "REJECTED"),
                "plan_hash": a.get("plan_hash"), "expired": expired}

    async def decide_approval(self, *, tenant_id, approval_id, decision, reviewer_id,
                              reviewer_role=None, comment=None, allowed_roles=None):
        roles = [r.strip().lower() for r in (allowed_roles or []) if r and r.strip()]
        if roles and (reviewer_role or "").strip().lower() not in roles:
            raise PermissionError("REVIEWER_ROLE_NOT_ALLOWED")
        if approval_id in self.approvals:
            self.approvals[approval_id].update({"status": decision, "reviewer_id": reviewer_id,
                                               "reviewer_role": reviewer_role, "decision_comment": comment})
        M.APPROVALS.labels(decision=str(decision)).inc()

    async def create_refund_request(self, *, tenant_id, ticket_id, order_id, policy_decision_id, currency,
                                    idempotency_key, payload_hash):
        rid = _uuid()
        self.refund_requests[rid] = {"refund_request_id": rid, "tenant_id": tenant_id, "order_id": order_id,
                                     "status": "CREATED", "amount_minor": 0, "idempotency_key": idempotency_key}
        return rid

    async def insert_quote(self, *, tenant_id, refund_request_id, quote):
        self.quotes.append(quote)
        if refund_request_id in self.refund_requests:
            self.refund_requests[refund_request_id].update(
                {"current_quote_id": quote["quote_id"], "amount_minor": quote["amount_minor"], "status": "QUOTED",
                 "reservation_reference": quote.get("reservation_reference"),
                 "reservation_expires_at": quote.get("reservation_expires_at")})

    async def mark_refund_authorized(self, *, tenant_id, refund_request_id, approval_id, risk_decision_id):
        if refund_request_id in self.refund_requests:
            self.refund_requests[refund_request_id]["status"] = "AUTHORIZED"

    async def enqueue_command(self, **kw):
        cid = _uuid()
        rec = {"command_id": cid, "status": "PREPARED", "attempt_count": 0,
               "priority": int(kw.get("priority", 100)),
               "idempotency_key": kw.get("idempotency_key")
               or f"{kw.get('authorization_ref') or kw.get('ticket_id')}:{kw.get('action')}",
               **kw}
        self.commands.append(rec)
        M.COMMANDS.labels(action=str(kw.get("action") or "unknown")).inc()
        return cid

    async def create_reconciliation_task(self, *, tenant_id, ticket_id, refund_request_id, reason,
                                         command_id=None):
        for t in self.reconciliations:
            if (t.get("ticket_id") == ticket_id and t.get("reason") == reason
                    and t.get("status") in ("PENDING", "RUNNING")):
                return t["task_id"]
        tid = _uuid()
        self.reconciliations.append({"task_id": tid, "tenant_id": tenant_id, "ticket_id": ticket_id,
                                     "refund_request_id": refund_request_id, "command_id": command_id,
                                     "reason": reason, "status": "PENDING", "attempt_count": 0})
        return tid

    async def ensure_policy_review_task(self, *, tenant_id, ticket_id, thread_id=None,
                                        policy_version=None, reason_code, retrieval_bundle, facts_snapshot):
        """幂等创建后台政策核实任务（内存实现，供测试与本地运行）。"""
        for task in self.policy_review_tasks:
            if (task.get("tenant_id") == tenant_id and task.get("ticket_id") == ticket_id
                    and task.get("reason_code") == reason_code
                    and task.get("policy_version") == policy_version
                    and task.get("status") in ("PENDING", "RUNNING")):
                return task["task_id"]
        tid = _uuid()
        self.policy_review_tasks.append({
            "task_id": tid, "tenant_id": tenant_id, "ticket_id": ticket_id, "thread_id": thread_id,
            "workflow_version": "v4", "policy_version": policy_version, "reason_code": reason_code,
            "retrieval_bundle": retrieval_bundle, "facts_snapshot": facts_snapshot,
            "status": "PENDING", "created_at": _now(), "updated_at": _now(),
        })
        return tid

    async def reclaim_expired_commands(self, *, max_attempts: int = 3):
        # 内存实现无真实租约时钟语义：返回空（回收逻辑由 SqlPersistence 覆盖）
        return []

    async def escalate_expired_approvals(self):
        now = _now()
        out = []
        for a in self.approvals.values():
            if a.get("status") == "PENDING" and a.get("expires_at") and str(a["expires_at"]) < now:
                a["status"] = "EXPIRED"
                out.append({"approval_id": a["approval_id"], "tenant_id": a["tenant_id"],
                            "ticket_id": a.get("ticket_id"), "plan_hash": a.get("plan_hash")})
        return out

    # ---------------- Worker 支撑（与 SqlPersistence 对齐） ----------------
    async def claim_next_command(self):
        ready = [c for c in self.commands if c.get("status") == "PREPARED"]
        ready.sort(key=lambda c: (int(c.get("priority", 100)), self.commands.index(c)))
        for c in ready:
            c["status"] = "CLAIMED"
            c["attempt_count"] = c.get("attempt_count", 0) + 1
            return dict(c)
        return None

    async def cancel_command(self, *, command_id, tenant_id, reason):
        for c in self.commands:
            if c["command_id"] == command_id and c.get("status") == "PREPARED":
                c["status"] = "CANCELLED"
                c["last_error"] = reason
                return True
        return False

    async def claim_pending_outbox(self, *, limit=50):
        return []

    async def mark_command_result(self, *, command_id, tenant_id, result):
        status = _map_result_status(result.get("status"))
        for c in self.commands:
            if c["command_id"] == command_id:
                c["status"] = status
                c["last_error"] = result.get("error")
        if status == "UNKNOWN":
            self.reconciliations.append({"task_id": _uuid(), "ticket_id": None, "refund_request_id": None,
                                         "reason": "UNKNOWN_COMMAND", "status": "PENDING", "attempt_count": 0})
        M.COMMAND_RESULTS.labels(status=str(status)).inc()
        return status

    async def write_inbox(self, *, tenant_id, source, dedup_key, payload):
        if any(e["source"] == source and e["dedup_key"] == dedup_key for e in self.inbox):
            return False
        self.inbox.append({"tenant_id": tenant_id, "source": source, "dedup_key": dedup_key, "payload": payload})
        return True

    async def mark_outbox_published(self, *, aggregate_id):
        self.outbox_published.append(aggregate_id)
        return None

    async def claim_due_reconciliation(self):
        for t in self.reconciliations:
            if t.get("status", "PENDING") == "PENDING":
                t["status"] = "RUNNING"
                t["attempt_count"] = t.get("attempt_count", 0) + 1
                return dict(t)
        return None

    async def complete_reconciliation(self, *, task_id, status, next_run_at_seconds=0):
        for t in self.reconciliations:
            if t["task_id"] == task_id:
                t["status"] = status
        M.RECONCILIATION.labels(status=str(status)).inc()
        return None


def _map_result_status(s):
    return {
        "SUCCEEDED": "CONFIRMED", "MANUAL": "CONFIRMED", "UNSUPPORTED": "FAILED_CONFIRMED",
        "ACCEPTED": "IN_FLIGHT", "REQUESTED": "IN_FLIGHT", "SUBMITTED": "IN_FLIGHT",
        "HELD": "CONFIRMED",
    }.get(s or "", "UNKNOWN")
