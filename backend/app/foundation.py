"""Transport-independent request context, domain events and durable consumer delivery."""
import json
from contextvars import ContextVar
from hashlib import sha256
from importlib.metadata import version
from uuid import uuid4

from .canonical import canonical_json

CORRELATION = ContextVar("correlation_id", default=None)
CAUSATION = ContextVar("causation_id", default=None)
PRINCIPAL = ContextVar("principal", default=None)
TRACE = ContextVar("trace_id", default=None)
TASK = ContextVar("task_id", default=None)


def correlation_id():
    return CORRELATION.get() or str(uuid4())


def trace_id():
    from opentelemetry.trace import get_current_span
    context = get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else TRACE.get() or uuid4().hex


def procurement_context(policy, customer_id, request_ref, constraints, mandate=None):
    document = policy._editable(policy.get())
    return {"customer_id": customer_id, "supplier_id": "valio-aimo", "request_ref": request_ref,
            "effective_constraints": constraints.model_dump(mode="json"),
            "mandate_ref": mandate.id if mandate else None,
            "policy_version": sha256(canonical_json(document)).hexdigest(), "environment": "live"}


EVENT_ACTIONS = {"LOOKUP": "request.created", "CREATE_QUOTE": "quote.created", "QUOTE_ACCEPTED": "quote.accepted",
                 "QUOTE_EXPIRED": "quote.expired", "PLACE_ORDER": "order.placed",
                 "APPROVE": "order.confirmed", "REJECT": "order.rejected",
                 "CANCEL": "order.cancelled", "ORDER_EXPIRED": "order.expired",
                 "EVIDENCE_CHANGED": "order.evidence_changed", "POLICY_UPDATED": "policy.updated",
                 "MANDATE_REVOKED": "mandate.revoked"}
EVENT_ACTIONS["PROPOSAL_RECEIVED"] = "proposal.received"
EVENT_ACTIONS.update(AUTONOMY_CHANGED="autonomy.changed", AUTONOMY_REVIEW_SUGGESTED="autonomy.review_suggested")
EVENT_ACTIONS.update(AGENT_SUSPENDED="agent.suspended", AGENT_RESUMED="agent.resumed")


def emit(conn, action, actor, ts, audit_id, data, order_id=None, quote_id=None):
    if action == "MANDATE_REVOKED" and data.get("customer_ids") and not data.get("customer_id"):
        scoped_data = {key: value for key, value in data.items() if key != "customer_ids"}
        for customer in data["customer_ids"]:
            for mandate in data.get("jti_revoked", []):
                emit(conn, action, actor, ts, audit_id,
                     {**scoped_data, "customer_id": customer, "mandate_id": mandate, "jti_revoked": [mandate]})
        return
    event_type = EVENT_ACTIONS.get(action)
    if event_type is None:
        return
    kind = event_type.split(".")[0]
    aggregate_id = order_id if kind == "order" else quote_id if kind == "quote" else data.get("lookup_id", kind)
    if kind == "mandate":
        aggregate_id = f"{data.get('customer_id')}:{data.get('issuer')}:{data.get('mandate_id')}"
    if kind == "proposal":
        aggregate_id = data.get("proposal_id", aggregate_id)
    if kind in {"autonomy", "agent"}:
        aggregate_id = data["agent_id"]
    if aggregate_id is None:
        return  # Historical/imported audit notes do not themselves change an aggregate.
    table = {"order": "orders", "quote": "quotes", "request": "lookups"}.get(kind)
    row = conn.execute(f"SELECT data FROM {table} WHERE id=?", (aggregate_id,)).fetchone() if table else None
    obj = json.loads(row[0]) if row else {}
    context = obj.get("context") or {}
    if kind in {"autonomy", "agent", "mandate"}:
        context = {"customer_id": data.get("customer_id"), "customer_ids": data.get("customer_ids", []),
                   "organisation_id": data.get("organisation_id")}
    revision = conn.execute("SELECT COALESCE(MAX(aggregate_revision),0)+1 FROM events WHERE aggregate_type=? AND aggregate_id=?",
                            (kind, aggregate_id)).fetchone()[0]
    event_id = obj.get("links", {}).get("event_id") if action in {"LOOKUP", "CREATE_QUOTE"} else None
    event_id = event_id or str(uuid4())
    correlation = CORRELATION.get() or obj.get("correlation_id") or correlation_id()
    links = {"request_id": obj.get("need_ref") or context.get("request_ref"), "quote_id": quote_id,
             "order_id": order_id, "event_id": event_id, "audit_checkpoint": audit_id,
             "receipt_id": order_id if obj.get("receipt") else None,
             "trace_id": TRACE.get() or obj.get("links", {}).get("trace_id") or trace_id(), "task_id": TASK.get()}
    payload = {**data, "links": links, "status": obj.get("status"), "context": context,
               "evidence_revision": obj.get("evidence_revision")}
    conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (event_id, 1, event_type, obj.get("customer_id") or context.get("customer_id") or data.get("customer_id"),
                  context.get("environment", "live"), kind, aggregate_id, revision, actor, ts,
                  correlation, CAUSATION.get(), json.dumps(payload), audit_id))
    conn.execute("INSERT INTO outbox(event_id,consumer,status,attempts,next_attempt_at) "
                 "SELECT ?,name,'pending',0,? FROM event_consumers WHERE enabled=1", (event_id, ts))
    trace = conn.execute("SELECT links FROM trace_links WHERE id=?", (links["trace_id"],)).fetchone()
    trace_links = json.loads(trace[0]) if trace else {}
    for name, value in links.items():
        if value is not None:
            trace_links.setdefault(name, [])
            if value not in trace_links[name]:
                trace_links[name].append(value)
    conn.execute("INSERT INTO trace_links VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET links=excluded.links",
                 (links["trace_id"], correlation, json.dumps(trace_links)))
    if kind == "order" and row:
        related = conn.execute("SELECT id,audit_id,json_extract(payload,'$.links.trace_id') AS trace_id FROM events WHERE aggregate_type='order' AND aggregate_id=? ORDER BY aggregate_revision", (aggregate_id,)).fetchall()
        obj.setdefault("links", {}).update(event_ids=[r["id"] for r in related],
            audit_checkpoints=[r["audit_id"] for r in related],
            trace_ids=list(dict.fromkeys(r["trace_id"] for r in related)))
        conn.execute("UPDATE orders SET data=? WHERE id=?", (json.dumps(obj), aggregate_id))
    if action == "PLACE_ORDER":
        emit(conn, "QUOTE_ACCEPTED", actor, ts, audit_id, data, order_id, quote_id)


class Outbox:
    """At-least-once delivery. Consumers deduplicate by event.id; callbacks run after commit.

    Registration is durable and transport-neutral: webhooks, A2A and activity can each
    attach a callback by stable name without changing the procurement transaction.
    """
    def __init__(self, db):
        import threading
        self.db, self.handlers = db, {}
        self.lock = threading.Lock()

    def register(self, name, handler=None, *, replay=False):
        if not name or (handler is not None and not callable(handler)):
            raise ValueError("A consumer name and callable handler are required")
        with self.db.connect(write=True, notify=False) as conn:
            conn.execute("INSERT INTO event_consumers VALUES(?,1) ON CONFLICT(name) DO UPDATE SET enabled=1", (name,))
            if replay:
                conn.execute("INSERT OR IGNORE INTO outbox(event_id,consumer,status,attempts,next_attempt_at) "
                             "SELECT id,?,'pending',0,ts FROM events", (name,))
        if handler is not None:
            self.handlers[name] = handler

    def dispatch(self, limit=100):
        from .audit import utcnow
        from datetime import timedelta
        if not self.lock.acquire(blocking=False):
            return
        try:
            with self.db.connect() as conn:
                consumers = tuple(self.handlers)
                if not consumers:
                    return
                rows = conn.execute("SELECT o.*,e.payload FROM outbox o JOIN events e ON e.id=o.event_id "
                                    "JOIN event_consumers c ON c.name=o.consumer "
                                    "WHERE c.enabled=1 AND o.consumer IN (" + ",".join("?" for _ in consumers) + ") "
                                    "AND o.status='pending' AND o.next_attempt_at<=? ORDER BY e.rowid LIMIT ?",
                                    (*consumers, utcnow().isoformat(), limit)).fetchall()
            for row in rows:
                handler = self.handlers.get(row["consumer"])
                if handler is None:
                    continue
                with self.db.connect() as conn:
                    event = dict(conn.execute("SELECT * FROM events WHERE id=?", (row["event_id"],)).fetchone())
                event["payload"] = json.loads(event["payload"])
                status = "delivered"
                try:
                    handler(event)
                except Exception:
                    status = "pending"
                with self.db.connect(write=True, notify=False) as conn:
                    conn.execute("UPDATE outbox SET status=?,attempts=attempts+1,next_attempt_at=? WHERE event_id=? AND consumer=?",
                                 (status, (utcnow() + timedelta(seconds=min(3600, 2 ** min(row["attempts"] + 1, 12)))).isoformat(),
                                  row["event_id"], row["consumer"]))
        finally:
            self.lock.release()


def manifest():
    from mcp_types import LATEST_PROTOCOL_VERSION
    return {"specifications": {"openapi": "3.1.0", "mcp_protocol": LATEST_PROTOCOL_VERSION,
            "mcp_server_card": {"status": "draft; implemented", "draft_commit": "526201bbc80231daa40ffcdecfc9da4e54e5dc93"},
            "a2a": {"version": "1.0", "status": "implemented"}, "ubl": "2.1",
            "vc_data_model": "2.0", "vc_securing_profile": "VC-JWT EdDSA / RFC 7519 / RFC 8037",
            "problem_details": "RFC 9457", "standard_webhooks": {"commit": "36afae792227eae8a182a7a1a5711dbdc08a4334", "status": "implemented", "signature": "v1a Ed25519"}},
            "packages": {name: version(name) for name in ("fastapi", "mcp", "pydantic", "httpx", "a2a-sdk")},
            "limiter_scope": "in-process, single instance", "recovery_version": 1}
