"""Shared command boundary used by current and future transports."""
import inspect
import json
import time
from dataclasses import asdict, is_dataclass
from functools import wraps
from hashlib import sha256

from pydantic import BaseModel

from .canonical import canonical_json
from .db import DomainError
from .foundation import CAUSATION, CORRELATION, PRINCIPAL, TRACE, correlation_id, trace_id


def json_value(value):
    if is_dataclass(value):
        return json_value(asdict(value))
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(v) for v in value]
    return value


def install(gateway, target, name, category):
    operation = getattr(target, name)
    signature = inspect.signature(operation)

    def linked(result):
        from .models import Order
        if isinstance(result, Order):
            with gateway.db.connect() as conn:
                events = conn.execute("SELECT id,audit_id,json_extract(payload,'$.links.trace_id') AS trace_id FROM events WHERE aggregate_type='order' AND aggregate_id=? ORDER BY aggregate_revision", (result.id,)).fetchall()
            result.links.update(event_ids=[r["id"] for r in events], audit_checkpoints=[r["audit_id"] for r in events],
                                trace_ids=list(dict.fromkeys(r["trace_id"] for r in events)))
        return result

    @wraps(operation)
    def execute(*args, **kwargs):
        supplied_key = kwargs.pop("command_key", None)
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        arguments = bound.arguments
        principal = PRINCIPAL.get() or arguments.get("principal")
        identity = principal.agent_id if principal else arguments.get("agent_id") or (arguments.get("actor") or "agent:external").split(":", 1)[-1]
        scope = f"{principal.kind}:{principal.id}" if principal else f"agent:{identity}"
        key = supplied_key if supplied_key is not None else arguments.get("idempotency_key")
        payload = {"command": name, "arguments": json_value({k: v for k, v in arguments.items()
                   if k not in {"agent_id", "actor", "operator", "mandate", "principal", "idempotency_key"}})}
        digest = sha256(canonical_json(payload)).hexdigest()
        tokens = CORRELATION.set(CORRELATION.get() or correlation_id()), CAUSATION.set(key)
        trace_token = TRACE.set(TRACE.get() or trace_id())
        try:
            if key is None:
                gateway.limiter.consume(identity, category)
                return linked(operation(*args, **kwargs))
            if not key.strip():
                raise DomainError("IDEMPOTENCY_REQUIRED", "A non-empty idempotency key is required", 422)
            # SQLite serialises command lookup, domain changes and saved results as one commit.
            error = None
            with gateway.db.connect(write=True) as conn:
                previous = conn.execute("SELECT * FROM commands WHERE principal=? AND key=?", (scope, key)).fetchone()
                if previous:
                    if previous["digest"] != digest:
                        raise DomainError("IDEMPOTENCY_CONFLICT", "Idempotency key belongs to another quote or agent, or a changed command payload", 409)
                try:
                    gateway.limiter.consume(identity, "read" if previous else category)
                    if previous:
                        if name == "place_order":
                            # Preserve buyer mandate revalidation and the historical replay audit.
                            return linked(operation(*args, **kwargs))
                        from . import models
                        saved = json.loads(previous["result"])
                        model = getattr(models, saved["model"]) if saved["model"] else None
                        return model.model_validate(saved["value"]) if model else saved["value"]
                    result = linked(operation(*args, **kwargs))
                except DomainError as caught:
                    error = caught
                else:
                    conn.execute("INSERT INTO commands VALUES(?,?,?,?,?)",
                                 (scope, key, name, digest, json.dumps({"model": type(result).__name__ if isinstance(result, BaseModel) else None,
                                                                     "value": json_value(result)})))
            if error:
                raise error
            return result
        finally:
            gateway.valio.flush_rate_limit_audits(gateway.db)
            TRACE.reset(trace_token)
            CORRELATION.reset(tokens[0])
            CAUSATION.reset(tokens[1])

    @wraps(operation)
    def command(*args, **kwargs):
        from .runtime import SUPPLIER
        from .telemetry import span, metric, links
        values = signature.bind_partial(*args, **{k: v for k, v in kwargs.items() if k != "command_key"}).arguments
        supplier = getattr(values.get("need"), "supplier_id", None)
        saved_links = []
        reference = values.get("order_id") or values.get("quote_id") or getattr(values.get("request"), "need_ref", None)
        if reference:
            table = "orders" if values.get("order_id") else "quotes" if values.get("quote_id") else "lookups"
            with gateway.db.connect() as conn:
                row = conn.execute(f"SELECT data FROM {table} WHERE id=?", (reference,)).fetchone()
            if row:
                obj = json.loads(row[0])
                supplier = obj.get("supplier_id") or obj.get("need", {}).get("supplier_id", "valio")
                if name in {"approve", "reject"}:
                    saved_links = links(obj.get("links", {}).get("placement_span"))
        token = SUPPLIER.set(supplier or "valio")
        label = {"create_quote": "quote.create", "place_order": "order.place", "approve": "approval.review",
                 "reject": "approval.review"}.get(name, "command." + name)
        started = time.perf_counter()
        try:
            with gateway.activate(), span(label, links=saved_links) as current:
                result = execute(*args, **kwargs)
                if gateway.sandbox:
                    gateway.sandbox.after(name, result)
                if current is not None:
                    for key, value in {"order.ref": getattr(result, "id", None) if target is gateway.orders and name != "create_quote" else None,
                                       "quote.ref": getattr(result, "quote_id", None) or (getattr(result, "id", None) if name == "create_quote" else None),
                                       "outcome": getattr(result, "status", "success")}.items():
                        if value is not None:
                            current.set_attribute(key, value)
                if name == "find_offers":
                    metric("candidates.screened", result.screened_count)
                    metric("candidates.priced", result.priced_count)
                    if current is not None:
                        current.set_attribute("candidates.screened", result.screened_count)
                        current.set_attribute("candidates.priced", result.priced_count)
                if name in {"approve", "reject"}:
                    from .audit import utcnow
                    metric("approval.wait", max(0, (utcnow() - result.created_at).total_seconds()))
                return result
        finally:
            with gateway.activate():
                metric("request.latency", time.perf_counter() - started)
            SUPPLIER.reset(token)

    setattr(target, name, command)
