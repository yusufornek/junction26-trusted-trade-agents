"""Shared command boundary used by current and future transports."""
import inspect
import json
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
    def command(*args, **kwargs):
        supplied_key = kwargs.pop("command_key", None)
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        arguments = bound.arguments
        principal = PRINCIPAL.get() or arguments.get("principal")
        identity = principal.agent_id if principal else arguments.get("agent_id") or (arguments.get("actor") or "agent:external").split(":", 1)[-1]
        if category != "read" and (principal is None or principal.kind == "agent"):
            from .buyer import check_suspension
            check_suspension(gateway.db, identity)
        scope = f"{principal.kind}:{principal.id}" if principal else f"agent:{identity}"
        key = supplied_key if supplied_key is not None else arguments.get("idempotency_key")
        payload = {"command": name, "arguments": json_value({k: v for k, v in arguments.items()
                   if k not in {"agent_id", "actor", "operator", "mandate", "idempotency_key"}})}
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

    setattr(target, name, command)
