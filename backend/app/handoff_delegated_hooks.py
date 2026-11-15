"""Small domain hooks keep the shared procurement engine authoritative."""
import json
from contextvars import ContextVar
from contextlib import contextmanager
from functools import wraps
from hashlib import sha256

from . import audit
from .canonical import canonical_json
from .db import DomainError
from .foundation import PRINCIPAL
from .handoff_delegated_authority import HandoffAuthority, WORK

DISPATCH = ContextVar("handoff_dispatch", default=False)
EXCLUDE_ORDER = ContextVar("delegated_existing_order", default=None)


@contextmanager
def work(gateway, id):
    with gateway.db.connect() as conn:
        obj = gateway.intents.load(conn, id)
    token = WORK.set(obj)
    try:
        yield obj
    finally:
        WORK.reset(token)


def audit_data(data=None):
    p = PRINCIPAL.get()
    result = dict(data or {})
    obj = WORK.get()
    if obj:
        result.setdefault("intent_id", obj["id"])
    if p and p.kind == "agent":
        result.setdefault("actor_chain", list((p.actor_chain if p.delegation_id or len(p.actor_chain) > 1 else
            obj.get("actor_chain") if obj else None) or p.actor_chain or (p.id,)))
        result.setdefault("delegation_id", p.delegation_id)
    return result


def intent_audit(row, intent_id):
    """Only events for this exact intent are disclosed to its order's reader."""
    data = json.loads(row["data"])
    return bool(intent_id and data.get("intent_id") == intent_id and not data.get("error"))


def actor_context():
    p = PRINCIPAL.get()
    obj = WORK.get()
    if not p or p.kind != "agent":
        return {}
    chain = list(p.actor_chain or (p.id,))
    if obj:
        ancestry = list(obj.get("actor_chain", []))
        ancestry.extend(name for name in chain[:-1] if name not in ancestry)
        if not ancestry or ancestry[-1] != p.id:
            ancestry.append(p.id)
        chain = ancestry
    return {"actor_chain": chain, "delegation_id": p.delegation_id}


def timeline_rows(conn, order_id, quote_id, lookup_id):
    row = conn.execute("SELECT json_extract(data,'$.intent_id') FROM quotes WHERE id=?", (quote_id,)).fetchone()
    intent_id = row[0] if row else None
    return conn.execute("""SELECT * FROM audit WHERE order_id=? OR quote_id=?
        OR (action='LOOKUP' AND json_extract(data,'$.lookup_id')=?)
        OR (? IS NOT NULL AND json_extract(data,'$.intent_id')=?)""", (order_id, quote_id, lookup_id, intent_id, intent_id))


def intent_event_visible(gateway, conn, event, p):
    row = conn.execute("SELECT data,tenant_id FROM purchase_intents WHERE id=?", (event["aggregate_id"],)).fetchone()
    if not row:
        return False
    obj, payload = json.loads(row[0]), event["payload"]
    if p.kind == "agent":
        agent = conn.execute("SELECT tenant_id FROM main.tenant_agents WHERE agent_id=?", (p.id,)).fetchone()
        grant = payload.get("grant") or {}
        return bool(agent and agent[0] == row["tenant_id"] and p.id in {
            obj["active_agent"], payload.get("previous_agent"), grant.get("from_agent_id"), grant.get("to_agent_id")})
    return gateway.buyer.visible(p, obj["active_agent"], [obj["request"]["customer_id"]])


def install(gateway):
    authority = gateway.handoff_authority = HandoffAuthority(gateway)
    governance, intents, orders = gateway.governance, gateway.intents, gateway.orders
    connection_check = gateway.connection_trust.check
    @wraps(connection_check)
    def live_chain(p, conn=None, *, mutation=False):
        if conn is None:
            with gateway.db.connect() as conn:
                return live_chain(p, conn, mutation=mutation)
        if p and p.delegation_id:
            authority.valid_grant(conn, p.delegation_id, p.tenant_id)
        if p:
            for name in p.actor_chain:
                governance.check_active(name, conn)
        return connection_check(p, conn, mutation=mutation)
    gateway.connection_trust.check = live_chain

    dispatch_authority = orders._dispatch_authority
    @wraps(dispatch_authority)
    def delegated_dispatch(conn, order, quote, credential):
        if credential and credential.get("delegation_id"):
            from .auth import Principal
            p = Principal(order.agent_id, **{k: v for k, v in credential.items()
                if k in Principal.__dataclass_fields__ and k != "agent_id"})
            token = PRINCIPAL.set(p)
            try:
                authority.guard("order", order.intent_id)
                return dispatch_authority(conn, order, quote, {**credential, "credential_ref": "internal"})
            finally:
                PRINCIPAL.reset(token)
        return dispatch_authority(conn, order, quote, credential)
    orders._dispatch_authority = delegated_dispatch
    resolve = governance.resolve

    @wraps(resolve)
    def effective(agent_id, *, conn=None, **kwargs):
        if conn is None:
            with gateway.db.connect() as conn:
                return effective(agent_id, conn=conn, **kwargs)
        value = resolve(agent_id, conn=conn, **kwargs)
        p = PRINCIPAL.get()
        if p and (p.kind != "agent" or p.id != agent_id) or not p and WORK.get() is None:
            return value
        sources = []
        for name in p.actor_chain if p else ():
            if name == agent_id:
                continue
            authority.agent(conn, name, p.tenant_id)
            sources.append({k: v.value for k, v in resolve(name, conn=conn).fields.items()})
        if p and p.delegation_id:
            grant = authority.valid_grant(conn, p.delegation_id, p.tenant_id)
            while grant:
                sources.append(grant["envelope"])
                grant = authority.valid_grant(conn, grant["parent_id"], p.tenant_id) if grant["parent_id"] else None
        obj = WORK.get()
        if obj and obj.get("authority_envelope"):
            sources.append(obj["authority_envelope"])
        if not sources:
            return value
        from .governance import restrictive
        original_value = value
        value = value.model_copy(deep=True)
        changed = False
        for source in sources:
            for key, limit in source.items():
                if key not in value.fields:
                    continue
                field = value.fields[key]
                meet = restrictive(key, field.value, limit)
                if meet == field.value:
                    continue
                changed = True
                field.value = meet
                field.sources.append({"source_layer": "agent", "author": "authority_chain", "version_id": "delegated", "value": limit})
        if not changed:
            return original_value
        value.version_id = "eff_" + sha256(canonical_json(value.model_dump(exclude={"version_id"}))).hexdigest()
        return value

    governance.resolve = effective
    def rolling_total(conn, agent, exclude=None):
        from datetime import datetime, timedelta
        from decimal import Decimal
        now = audit.utcnow()
        epoch = conn.execute("SELECT value FROM workspace WHERE key='budget_start'").fetchone()
        total = Decimal(0)
        for row in conn.execute("SELECT id,data FROM orders WHERE status IN ('CONFIRMED','PENDING_EXECUTION') AND rowid>?", (int(epoch[0]) if epoch else 0,)):
            obj = json.loads(row["data"])
            if row["id"] == exclude or obj.get("approver") or obj.get("currency") != "EUR":
                continue
            if obj["agent_id"] != agent and agent not in (obj.get("context") or {}).get("actor_chain", []):
                continue
            if now - timedelta(hours=24) <= datetime.fromisoformat(obj["created_at"]) <= now:
                total += Decimal(str(obj["total"]))
        return total
    gateway.policy.autonomous_total = lambda conn, agent_id: rolling_total(conn, agent_id)
    legacy_evaluate = gateway.policy.evaluate
    @wraps(legacy_evaluate)
    def evaluate_rolling(total, lines, *args, **kwargs):
        result = legacy_evaluate(total, lines, *args, **kwargs)
        p, obj = PRINCIPAL.get(), WORK.get()
        caller = kwargs.get("agent_id")
        limits = {}
        with gateway.db.connect() as conn:
            envelopes = [obj["authority_envelope"]] if obj and obj.get("authority_envelope") else []
            if p and p.delegation_id:
                grant = authority.valid_grant(conn, p.delegation_id, p.tenant_id)
                while grant:
                    envelopes.append(grant["envelope"])
                    grant = authority.valid_grant(conn, grant["parent_id"], p.tenant_id) if grant["parent_id"] else None
            for envelope in envelopes:
                for name, limit in envelope.get("_rolling_limits", {}).items():
                    limits[name] = min(limits.get(name, limit), limit)
            policy = gateway.policy.get()
            for name in p.actor_chain if p else ():
                if name != caller and name in policy.agents:
                    limit = policy.agents[name].max_auto_daily_total_eur
                    limits[name] = min(limits.get(name, limit), limit)
            from decimal import Decimal
            from .models import RuleHit
            for name, limit in limits.items():
                used = kwargs.get("autonomous_total", Decimal(0)) if name == caller else rolling_total(conn, name, EXCLUDE_ORDER.get())
                if used + Decimal(str(total)) > Decimal(str(limit)):
                    if name == caller and any(h.rule == "max_auto_daily_total_eur" for h in result.hits):
                        continue
                    result.hits.append(RuleHit(rule="max_auto_daily_total_eur", detail=f"{name}: {used:.2f} + {total:.2f} exceeds {limit:.2f} EUR in the rolling 24-hour autonomous window."))
                    if result.outcome == "CONFIRMED":
                        result.outcome = "PENDING_APPROVAL"
        return result
    gateway.policy.evaluate = evaluate_rolling

    evaluate_intent = intents.evaluate
    @wraps(evaluate_intent)
    def existing_commitment(conn, quote, agent, policy, order=None):
        token = EXCLUDE_ORDER.set(order.id if order else None)
        try:
            return evaluate_intent(conn, quote, agent, policy, order)
        finally:
            EXCLUDE_ORDER.reset(token)
    intents.evaluate = existing_commitment
    spent = governance.spend
    @wraps(spent)
    def chain_spend(conn, agent_id, *, at=None, exclude_order=None):
        result = spent(conn, agent_id, at=at, exclude_order=exclude_order)
        from datetime import datetime
        from decimal import Decimal
        at = at or audit.utcnow()
        # A delegated commitment consumes every ancestor's budget, including
        # purchases made through another helper or after a client restart.
        for row in conn.execute("SELECT id,data FROM orders WHERE status IN ('CONFIRMED','PENDING_EXECUTION') AND json_extract(data,'$.agent_id')<>?", (agent_id,)):
            obj = json.loads(row["data"])
            if row["id"] == exclude_order or agent_id not in (obj.get("context") or {}).get("actor_chain", []):
                continue
            created = datetime.fromisoformat(obj["created_at"])
            if created > at:
                continue
            for key, matches in (("day_eur", created.date() == at.date()),
                                 ("month_eur", (created.year, created.month) == (at.year, at.month))):
                if matches:
                    result[key] = float(Decimal(str(result[key])) + Decimal(str(obj["total"])))
        return result
    governance.spend = chain_spend

    evaluate_policy = governance.evaluate
    @wraps(evaluate_policy)
    def evaluate_chain(conn, effective, total, lines, **kwargs):
        result = evaluate_policy(conn, effective, total, lines, **kwargs)
        p = PRINCIPAL.get()
        if p and p.id == effective.agent_id:
            from .models import RuleHit
            for name in p.actor_chain:
                if name == p.id:
                    continue
                parent = resolve(name, conn=conn)
                parent_result = evaluate_policy(conn, parent, total, lines,
                    **{**kwargs, "spent": chain_spend(conn, name, at=kwargs.get("at"))})
                if parent_result.outcome == "REJECTED":
                    result.outcome = "REJECTED"
                elif parent_result.hits and result.outcome == "CONFIRMED":
                    result.outcome = "PENDING_APPROVAL"
                result.hits.extend(RuleHit(rule=h.rule, detail=name + ": " + h.detail) for h in parent_result.hits)
        return result
    governance.evaluate = evaluate_chain

    before_commit = gateway.connection_trust.before_commit
    @wraps(before_commit)
    def check_chain_commit(conn, epoch, **kwargs):
        p = PRINCIPAL.get()
        if p and (p.delegation_id or len(p.actor_chain) > 1):
            authority.declared(p)
        return before_commit(conn, epoch, **kwargs)
    gateway.connection_trust.before_commit = check_chain_commit
    original_authority = intents.authority

    @wraps(original_authority)
    def intent_authority(conn, obj, agent, mandate=None, **kwargs):
        result = original_authority(conn, obj, agent, mandate, **kwargs)
        obj.setdefault("context_id", obj["id"])
        obj.setdefault("actor_chain", list((PRINCIPAL.get().actor_chain if PRINCIPAL.get() else ()) or (agent,)))
        if "authority_envelope" not in obj and obj["active_agent"] == agent:
            obj["authority_envelope"] = authority.snapshot(conn, obj, agent, mandate)
        return result

    intents.authority = intent_authority

    dispatch_pending = intents.dispatch_pending
    @wraps(dispatch_pending)
    def dispatch():
        token = DISPATCH.set(True)
        try:
            return dispatch_pending()
        finally:
            DISPATCH.reset(token)
    intents.dispatch_pending = dispatch

    for name in ("amend", "resume", "transition", "recover"):
        original = getattr(intents, name)
        def build(original, name):
            @wraps(original)
            def call(id, *args, **kwargs):
                # These methods all end in (agent, mandate=None).
                import inspect
                values = inspect.signature(original).bind(id, *args, **kwargs).arguments
                agent = values["agent"]
                with work(gateway, id) as obj:
                    operation = "order" if name in {"transition", "recover"} else "quote"
                    if name == "recover" and DISPATCH.get():
                        if obj.get("handoff_id"):
                            from .auth import Principal
                            p = Principal(**obj["holder_credential"])
                            token = PRINCIPAL.set(p)
                            try:
                                gateway.connection_trust.check(p)
                                authority.holder(obj, p.id, operation)
                                values["agent"] = p.id
                                return original(**values)
                            finally:
                                PRINCIPAL.reset(token)
                        return original(id, *args, **kwargs)
                    if name != "resume" or obj.get("handoff_id"):
                        authority.holder(obj, agent, operation)
                    return original(id, *args, **kwargs)
            return call
        setattr(intents, name, build(original, name))

    create_quote = orders.create_quote
    @wraps(create_quote)
    def quote(request, agent_id="external", **kwargs):
        authority.guard("quote", request.intent_id)
        obj = WORK.get()
        if not request.intent_id and obj:
            request = request.model_copy(update={"intent_id": obj["id"], "intent_version": obj["version"]})
        p = PRINCIPAL.get()
        token = None
        if p and request.need_ref:
            with gateway.db.connect() as conn:
                row = conn.execute("SELECT data FROM lookups WHERE id=?", (request.need_ref,)).fetchone()
                chain = (json.loads(row[0]).get("context") or {}).get("actor_chain") if row else None
            if chain:
                token = PRINCIPAL.set(authority.declared(p, chain))
        try:
            if request.intent_id:
                with work(gateway, request.intent_id) as obj:
                    authority.holder(obj, agent_id, "quote")
                    return create_quote(request, agent_id, **kwargs)
            return create_quote(request, agent_id, **kwargs)
        finally:
            if token is not None:
                PRINCIPAL.reset(token)
    orders.create_quote = quote

    place_order = orders.place_order
    @wraps(place_order)
    def place(quote_id, idempotency_key, agent_id="external", **kwargs):
        authority.guard("order")
        with gateway.db.connect() as conn:
            row = conn.execute("SELECT data FROM quotes WHERE id=?", (quote_id,)).fetchone()
        if not row:
            return place_order(quote_id, idempotency_key, agent_id, **kwargs)
        from .models import Quote
        q = Quote.model_validate_json(row[0])
        p = PRINCIPAL.get()
        chain = (q.context or {}).get("actor_chain")
        token = PRINCIPAL.set(authority.declared(p, chain)) if p and chain else None
        try:
            if q.intent_id:
                with work(gateway, q.intent_id) as obj:
                    authority.holder(obj, agent_id, "order")
                    return place_order(quote_id, idempotency_key, agent_id, **kwargs)
            return place_order(quote_id, idempotency_key, agent_id, **kwargs)
        finally:
            if token is not None:
                PRINCIPAL.reset(token)
    orders.place_order = place

    cancel = orders.cancel
    @wraps(cancel)
    def cancel_order(order_id, actor="system", *args, **kwargs):
        authority.guard("order")
        with gateway.db.connect() as conn:
            o = orders._order(conn, order_id)
        p = PRINCIPAL.get()
        if o.intent_id and not (p and p.operator or actor.startswith("human:")):
            with work(gateway, o.intent_id) as obj:
                authority.holder(obj, actor.removeprefix("agent:"), "order")
                return cancel(order_id, actor, *args, **kwargs)
        return cancel(order_id, actor, *args, **kwargs)
    orders.cancel = cancel_order

    find = gateway.find_offers
    @wraps(find)
    def find_offers(need, *args, **kwargs):
        authority.guard("lookup")
        p = PRINCIPAL.get()
        if p and p.delegation_id:
            with gateway.db.connect() as conn:
                grant = authority.valid_grant(conn, p.delegation_id, p.tenant_id)
                obj = intents.load(conn, grant["intent_id"])
            if need.customer_id != obj["request"]["customer_id"] or need.supplier_id != obj["request"]["supplier_id"]:
                raise DomainError("OUTSIDE_DELEGATION", "Lookup must stay inside the delegated intent", 403)
            with work(gateway, obj["id"]):
                return find(need, *args, **kwargs)
        return find(need, *args, **kwargs)
    gateway.find_offers = find_offers

    original_send = gateway.a2a.send
    def bind_task(task, params, p):
        from .a2a import metadata, t
        from google.protobuf.json_format import MessageToDict
        id = metadata(task).get("intent_id")
        if not id and p.delegation_id:
            with gateway.db.connect() as conn:
                id = authority.valid_grant(conn, p.delegation_id, p.tenant_id)["intent_id"]
            task.metadata.update({"intent_id": id})
        if not id:
            return task
        with gateway.db.connect(write=True) as conn:
            obj = intents.load(conn, id)
            if not obj.get("a2a_context_bound"):
                obj["context_id"] = params.message.context_id or obj.get("context_id", id)
                obj["a2a_context_bound"] = True
                conn.execute("UPDATE purchase_intents SET data=? WHERE id=? AND tenant_id=?",
                             (json.dumps(obj), id, obj["tenant_id"]))
            task.context_id = obj["context_id"]
            safe_task = t.Task()
            safe_task.CopyFrom(task)
            kept = [a for a in safe_task.artifacts if a.name != "single_use_credential"]
            safe_task.artifacts.clear()
            safe_task.artifacts.extend(kept)
            conn.execute("UPDATE a2a_tasks SET data=? WHERE id=?", (json.dumps(MessageToDict(safe_task)), task.id))
        return task
    @wraps(original_send)
    def send(params, p, mandate=None):
        from google.protobuf.json_format import MessageToDict
        from .a2a import command_data, metadata
        from .handoff_delegated_surfaces import send_command, operation
        parts = [MessageToDict(x)["data"] for x in params.message.parts if x.WhichOneof("content") == "data"]
        command = command_data(parts[0]) if len(parts) == 1 else {}
        meta = {**MessageToDict(params.message).get("metadata", {}), **MessageToDict(params).get("metadata", {}), **command.get("_meta", {})}
        p = authority.declared(p, meta.get("on_behalf_of"))
        token = PRINCIPAL.set(p)
        try:
            if command.get("command") != "delegate_task":
                authority.guard(operation(command.get("command", "")))
            task = send_command(gateway.a2a, params, p, mandate, command)
            if task is not None:
                return bind_task(task, params, p)
            if params.message.task_id:
                with gateway.db.connect() as conn:
                    id = metadata(gateway.a2a._load(conn, params.message.task_id, p)).get("intent_id")
                if id:
                    with work(gateway, id):
                        return bind_task(original_send(params, p, mandate), params, p)
            return bind_task(original_send(params, p, mandate), params, p)
        finally:
            PRINCIPAL.reset(token)
    gateway.a2a.send = send
