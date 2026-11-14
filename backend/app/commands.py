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


def command_checks(gateway, route, arguments, principal, operation):
    """Run boundary refusals through the same failure handler as business work.

    Only checks run here: wrapping the business operation again would audit its
    failures twice. In keyed calls the outer transaction commits the refusal
    audit before the saved error is raised to either transport.
    """
    if route == "gateway.find_offers":
        return gateway.lookup._run(arguments["need"], arguments["actor"], lambda counter: operation())
    if route == "baskets.find_basket_offers":
        return gateway.baskets._run(arguments["request"], arguments["actor"], lambda counter: operation())
    if route.startswith("orders.") and route in KEYED_ROUTES:
        name = route.split(".")[1]
        if name == "create_quote":
            action, actor = "CREATE_QUOTE", f"agent:{arguments['agent_id']}"
            inputs = arguments["request"].model_dump(mode="json")
        elif name == "place_order":
            action, actor = "PLACE_ORDER", f"agent:{arguments['agent_id']}"
            inputs = {key: arguments[key] for key in ("quote_id", "idempotency_key")}
        elif name == "cancel":
            action, actor = "CANCEL", arguments["actor"]
            inputs = {key: arguments[key] for key in ("order_id", "reason")}
            if arguments["stated_name"] is not None:
                inputs["stated_name"] = arguments["stated_name"]
        else:
            action = name.upper()
            actor = arguments["actor"] or f"human:{arguments['approver']}"
            if principal and principal.kind == "buyer_user":
                actor = f"human:buyer_approver:{principal.credential_ref}"
            inputs = {"order_id": arguments["order_id"], "comment": arguments["comment"],
                      "stated_name": arguments["approver"]}
            if name == "approve" and arguments["evidence_revision"] is not None:
                inputs["evidence_revision"] = arguments["evidence_revision"]
        return gateway.orders._run(action, actor, inputs, lambda counter: operation())
    return operation()


def replay_authorization(gateway, route, conn, arguments, principal, saved):
    """Recheck authority, never transitions/expiry or the original business effect.

    Registration fails closed for new keyed writes until they have a guard here.
    The guards use the same domain authorization functions as fresh commands.
    """
    from .auth import check_owner, require_operator
    from .autonomy import authorize_review
    owner = principal.owner if principal else arguments.get("agent_id")
    agent = principal.id if principal else arguments.get("agent_id") or (arguments.get("actor") or "agent:external").split(":", 1)[-1]
    if route == "autonomy.change":
        gateway.autonomy._target(conn, arguments["agent_id"], principal or arguments["principal"], change=True)
    elif route == "policy.update":
        if principal is not None:
            require_operator(principal)
    elif route == "mandates.revoke":
        gateway.mandates.authorize_revocation(conn, arguments["token"])
    elif route == "buyer.control":
        gateway.buyer.authorize_control(arguments["agent_id"], principal or arguments["principal"])
    elif route == "buyer.revoke":
        gateway.buyer.authorize_revocation(arguments["jti"], principal or arguments["principal"])
    elif route in {"webhooks.create", "webhooks.update", "webhooks.disable", "webhooks.redeliver"}:
        hooks = gateway.webhooks
        if route == "webhooks.create":
            body = arguments["body"]
            hooks._scope(principal, body.customer_ids, body.audience)
            subscription_id = saved["value"]["id"]
        else:
            subscription_id = arguments["subscription_id"]
        sub = hooks._get(subscription_id, principal, conn)
        if route == "webhooks.update":
            proposed = {**sub, **arguments["body"].model_dump(exclude_unset=True)}
            hooks._scope(principal, proposed["customer_ids"], proposed["audience"])
        elif route == "webhooks.redeliver":
            delivery = conn.execute("SELECT id FROM webhook_deliveries WHERE id=? AND subscription_id=?",
                                    (arguments["delivery_id"], subscription_id)).fetchone()
            if delivery is None:
                raise DomainError("WEBHOOK_NOT_FOUND", "Delivery was not found", 404)
    elif route in {"gateway.find_offers", "baskets.find_basket_offers"}:
        basket = route == "baskets.find_basket_offers"
        result = saved["value"]
        request = arguments["request" if basket else "need"]
        offers = result["basket"]["lines"] if basket else result["offers"]
        lookup_id = result["basket_lookup_id" if basket else "lookup_id"]
        gateway.mandates.authorize(conn, arguments["mandate"], agent, request.customer_id,
                                  products=[offer["product_id"] for offer in offers],
                                  bound=("lookup", lookup_id))
    elif route == "orders.create_quote":
        request = arguments["request"]
        row = conn.execute("SELECT data FROM lookups WHERE id=?", (request.need_ref,)).fetchone()
        if row is None:
            raise DomainError("NEED_NOT_FOUND", "need_ref must be a lookup_id returned by this gateway", 404)
        from .models import LookupResult
        lookup = LookupResult.model_validate_json(row[0], context={"stored": True})
        check_owner(lookup, None if arguments["operator"] else agent, "LOOKUP_NOT_FOUND")
        quote_id = saved["value"]["id"]
        bound = ("quote", quote_id) if gateway.mandates.stored(conn, "quote", quote_id) else ("lookup", request.need_ref)
        gateway.mandates.authorize(conn, arguments["mandate"], agent, request.customer_id,
                                  products=[line["product_id"] for line in saved["value"]["lines"]],
                                  bound=bound)
    elif route == "orders.place_order":
        quote = check_owner(gateway.orders._quote(conn, arguments["quote_id"]),
                            None if arguments["operator"] else agent, "QUOTE_NOT_FOUND")
        order_id = saved["value"]["id"]
        bound = ("order", order_id) if gateway.mandates.stored(conn, "order", order_id) else ("quote", quote.id)
        if (arguments["mandate"] is not None or gateway.mandates.stored(conn, *bound) is not None
                or agent in gateway.policy.get().agents):
            gateway.mandates.authorize(conn, arguments["mandate"], agent, quote.customer_id,
                products=[line["product_id"] for line in saved["value"]["lines"]], bound=bound)
        # Spec 06 R-2 explicitly permits returning an existing policy-row order
        # after its mandate was removed. It never grants a new purchase.
    elif route in {"orders.approve", "orders.reject", "orders.cancel"}:
        order = gateway.orders._order(conn, arguments["order_id"])
        if route == "orders.cancel":
            actor = arguments["actor"]
            check_owner(order, actor.removeprefix("agent:") if actor.startswith("agent:") else owner,
                        "ORDER_NOT_FOUND")
        else:
            authorize_review(gateway.policy.get(), principal, order)
            if route == "orders.approve":
                # Rejecting needs no purchasing authority; approving still does.
                gateway.mandates.authorize(conn, None, order.agent_id, order.customer_id,
                    products=[line.product_id for line in order.lines], bound=("order", order.id))
    else:
        raise RuntimeError(f"No replay authorization registered for {route}")


KEYED_ROUTES = frozenset({"orders.create_quote", "orders.place_order", "orders.cancel", "orders.approve",
                          "orders.reject", "gateway.find_offers", "baskets.find_basket_offers", "policy.update", "mandates.revoke", "autonomy.change",
                           "buyer.control", "buyer.revoke",
                          "webhooks.create", "webhooks.update", "webhooks.disable", "webhooks.redeliver"})


def fingerprint(scope, route, arguments):
    # agent_id may identify a route target (autonomy), so it is never discarded.
    # Principal metadata and presented credentials are authority, not request payload.
    payload = {"principal": scope, "route": route, "arguments": json_value({k: v for k, v in arguments.items()
               if k not in {"principal", "actor", "operator", "mandate", "idempotency_key"}})}
    return sha256(canonical_json(payload)).hexdigest()


def matches_command(previous, digest, name, route, arguments, saved):
    if previous["digest"] == digest:
        return True
    if saved.get("fingerprint_version") is not None:
        return False
    # Earlier command envelopes remain replayable. Their other path parameters
    # were hashed already; autonomy's omitted target must also match the saved result.
    legacy = {"command": name, "arguments": json_value({k: v for k, v in arguments.items()
              if k not in {"agent_id", "actor", "operator", "mandate", "idempotency_key"}})}
    # Lane B excluded the resolved principal from its earlier payload hashes.
    lane_legacy = {"command": name, "arguments": {k: v for k, v in legacy["arguments"].items() if k != "principal"}}
    return (previous["digest"] in {sha256(canonical_json(value)).hexdigest() for value in (legacy, lane_legacy)}
            and (route != "autonomy.change" or saved["value"].get("agent_id") == arguments["agent_id"]))


def install(gateway, target, name, category):
    operation = getattr(target, name)
    signature = inspect.signature(operation)
    namespace = next(key for key in ("orders", "policy", "mandates", "autonomy", "buyer", "webhooks", "baskets") if getattr(gateway, key, None) is target) if target is not gateway else "gateway"
    route = f"{namespace}.{name}"
    if category != "read" and route not in KEYED_ROUTES:
        raise RuntimeError(f"No replay authorization registered for {route}")
    if category != "read":
        gateway.keyed_routes = getattr(gateway, "keyed_routes", set()) | {route}

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
        scope = f"{principal.kind}:{principal.id}" if principal else f"agent:{identity}"
        key = supplied_key if supplied_key is not None else arguments.get("idempotency_key")
        digest = fingerprint(scope, route, arguments)
        tokens = CORRELATION.set(CORRELATION.get() or correlation_id()), CAUSATION.set(key)
        trace_token = TRACE.set(TRACE.get() or trace_id())
        try:
            def preflight():
                if category != "read" and (principal is None or principal.kind == "agent"):
                    from .buyer import check_suspension
                    check_suspension(gateway.db, identity)
                if key is not None:
                    if not key.strip():
                        raise DomainError("IDEMPOTENCY_REQUIRED", "A non-empty idempotency key is required", 422)
                    if route not in KEYED_ROUTES:
                        raise DomainError("VALIDATION_ERROR", "This read operation does not accept an idempotency key", 422)
            command_checks(gateway, route, arguments, principal, preflight)
            if key is None:
                command_checks(gateway, route, arguments, principal, lambda: gateway.limiter.consume(identity, category))
                return linked(operation(*args, **kwargs))
            # SQLite serialises command lookup, domain changes and saved results as one commit.
            error = None
            with gateway.db.connect(write=True) as conn:
                previous = conn.execute("SELECT * FROM commands WHERE principal=? AND key=?", (scope, key)).fetchone()
                try:
                    command_checks(gateway, route, arguments, principal,
                                   lambda: gateway.limiter.consume(identity, "read" if previous else category))
                    if previous:
                        saved = json.loads(previous["result"])
                        def check_replay():
                            if not matches_command(previous, digest, name, route, arguments, saved):
                                if previous["command"] == name and route == "autonomy.change" and "fingerprint_version" not in saved:
                                    replay_authorization(gateway, route, conn, arguments, principal, saved)
                                raise DomainError("IDEMPOTENCY_CONFLICT", "Idempotency key belongs to another quote or agent, or a changed command payload", 409)
                            replay_authorization(gateway, route, conn, arguments, principal, saved)
                        command_checks(gateway, route, arguments, principal, check_replay)
                        if name == "place_order":
                            # Preserve buyer mandate revalidation and the historical replay audit.
                            return linked(operation(*args, **kwargs))
                        from . import models
                        model = getattr(models, saved["model"]) if saved["model"] else None
                        return model.model_validate(saved["value"], context={"stored": True}) if model else saved["value"]
                    result = linked(operation(*args, **kwargs))
                except DomainError as caught:
                    error = caught
                else:
                    conn.execute("INSERT INTO commands VALUES(?,?,?,?,?)",
                                 (scope, key, name, digest, json.dumps({"model": type(result).__name__ if isinstance(result, BaseModel) else None,
                                                                     "value": json_value(result), "fingerprint_version": 2})))
            if error:
                raise error
            return result
        finally:
            gateway.valio.flush_rate_limit_audits(gateway.db)
            TRACE.reset(trace_token)
            CORRELATION.reset(tokens[0])
            CAUSATION.reset(tokens[1])

    setattr(target, name, command)
