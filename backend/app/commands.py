"""Shared command boundary used by current and future transports."""
import inspect
import json
import time
from datetime import date, datetime
from dataclasses import asdict, is_dataclass
from functools import wraps
from hashlib import sha256
from contextvars import ContextVar

from pydantic import BaseModel

from .canonical import canonical_json
from .db import DomainError
from .foundation import CAUSATION, CORRELATION, PRINCIPAL, TRACE, correlation_id, trace_id


ADMISSION = ContextVar("command_admission", default=None)


def charge(gateway, identity, category):
    credit = ADMISSION.get()
    if credit and credit["identity"] == identity and credit["category"] == category and not credit["used"]:
        credit["used"] = True
    else:
        gateway.limiter.consume(identity, category)


def failure_audit(gateway, route, arguments, identity, error):
    if getattr(error, "audited", False) or error.code == "RATE_LIMITED":
        return
    from . import audit
    action = {"gateway.find_offers": "LOOKUP", "orders.create_quote": "CREATE_QUOTE",
              "orders.place_order": "PLACE_ORDER", "orders.cancel": "CANCEL", "orders.approve": "APPROVE",
              "orders.reject": "REJECT", "a2a.command": "A2A_COMMAND"}.get(route)
    if action is None:
        return
    principal = PRINCIPAL.get()
    actor = f"agent:{identity}"
    if route in {"orders.approve", "orders.reject"} and principal:
        actor = "human:operator" if principal.operator else f"human:buyer_approver:{principal.credential_ref}"
    with gateway.db.connect(write=True) as conn:
        audit.append(conn, actor, action + "_FAILED", error.message,
                     order_id=arguments.get("order_id"), quote_id=arguments.get("quote_id"),
                     data={"error": error.code, **error.data, "live_calls": {"price": 0, "availability": 0}})
    error.audited = True


def json_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
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
    elif route in {"policy.update", "connections.create"}:
        if principal is not None:
            require_operator(principal)
    elif route == "mandates.revoke":
        gateway.mandates.authorize_revocation(conn, arguments["token"])
    elif route == "buyer.control":
        gateway.buyer.authorize_control(arguments["agent_id"], principal or arguments["principal"])
    elif route == "buyer.revoke":
        gateway.buyer.authorize_revocation(arguments["jti"], principal or arguments["principal"])
    elif route.startswith("recurring."):
        who = principal or arguments["principal"]
        recurring_id = saved["value"]["id"] if route == "recurring.create" else arguments["id"]
        row = gateway.recurring.get(recurring_id, who)
        if route == "recurring.baseline":
            from .recurring import reviewer
            if not reviewer(who, row["customer_id"]):
                raise DomainError("BUYER_APPROVER_REQUIRED", "Only a buyer reviewer may change the baseline", 403)
        if who.kind == "buyer_user":
            from .recurring import reviewer
            if not reviewer(who, row["customer_id"]):
                raise DomainError("BUYER_APPROVER_REQUIRED", "A buyer approver is required", 403)
        else:
            token = arguments.get("mandate") or gateway.mandates.stored(conn, "recurring", row["id"])
            gateway.recurring.workflow_authority(who, row["customer_id"], token)
        gateway.recurring.authority(conn, row)
    elif route == "proposals.submit":
        who = principal or arguments["principal"]
        if who.kind != "supplier_agent":
            raise DomainError("SUPPLIER_REQUIRED", "Only a supplier_agent may submit proposals", 403)
        if saved["value"].get("id"):
            row = conn.execute("SELECT data FROM proposal_campaigns WHERE id=?", (saved["value"]["id"],)).fetchone()
            if row is None:
                raise DomainError("CAMPAIGN_NOT_FOUND", "Campaign was not found", 404)
            gateway.proposals.counts(json.loads(row[0]), who)
    elif route == "proposals.action":
        who = gateway.proposals.effective_principal(principal or arguments["principal"], arguments.get("mandate"))
        proposal = gateway.proposals.get(conn, arguments["id"], who)
        if who.kind == "buyer_user" and who.role not in {"buyer_admin", "buyer_approver"}:
            raise DomainError("BUYER_APPROVER_REQUIRED", "A buyer approver is required", 403)
        if arguments["action"] == "accept":
            body = arguments["body"]
            agent = who.id if who.kind == "agent" else body.buyer_agent_id
            if agent != proposal.get("accepted_by", agent):
                raise DomainError("BUYER_AGENT_REQUIRED", "Replay requires the original purchasing agent", 403)
            bound = ("order", proposal["order_id"]) if proposal.get("order_id") else ("quote", saved["value"]["quote"]["id"])
            commitment = (gateway.orders._order(conn, bound[1]) if bound[0] == "order"
                          else gateway.orders._quote(conn, bound[1]))
            gateway.mandates.authorize(conn, arguments.get("mandate"), agent, commitment.customer_id,
                products=[line.product_id for line in commitment.lines], bound=bound, supplier_id=commitment.supplier_id)
    elif route == "memory.act":
        who = principal or arguments["principal"]
        # Hidden state and a newer purchase are command effects, not loss of
        # authority over the original result. Reauthorize its immutable scope.
        scoped = gateway.memory.scoped(who, arguments.get("mandate"))
        value = saved["value"]
        if arguments["action"] == "prepared":
            customer = value["need"]["customer_id"]
            product = (value.get("context") or {}).get("suggestion_ref", {}).get("product_id")
        else:
            customer = value["customer_id"]
            product = value.get("product_id") or value["baseline"]["lines"][0]["product"]
        from .purchase_memory import preferences
        prefs = preferences(conn, customer)
        if (not scoped.operator and customer not in scoped.customer_ids or not prefs.get("enabled", True)
                or prefs.get("forgotten_at") or product in prefs.get("excluded_products", [])):
            raise DomainError("SUGGESTION_NOT_FOUND", "Suggestion was not found", 404)
        pattern = {"customer_id": customer, "product_id": product}
        if who.kind == "agent" and arguments.get("mandate"):
            gateway.mandates.authorize(conn, arguments["mandate"], who.id, customer, products=[product] if product else [])
        if who.kind == "buyer_user" and who.role not in {"buyer_admin", "buyer_approver"}:
            raise DomainError("BUYER_APPROVER_REQUIRED", "A buyer approver is required", 403)
        if arguments["action"] in {"prepared", "made_recurring"}:
            body = arguments.get("body")
            agent = gateway.memory.purchasing_agent(who, pattern["customer_id"], body.buyer_agent_id if body else None)
            gateway.mandates.authorize(conn, arguments.get("mandate"), agent, pattern["customer_id"], products=[pattern["product_id"]])
            if arguments["action"] == "made_recurring":
                row = gateway.recurring.get(saved["value"]["id"])
                from .auth import Principal
                token = arguments.get("mandate") or gateway.mandates.stored(conn, "recurring", row["id"])
                gateway.recurring.workflow_authority(Principal(agent, customer_ids=(customer,)), customer, token)
                gateway.recurring.authority(conn, row)
    elif route in {"memory.put_preferences", "proposals.put_preferences"}:
        gateway.proposals.scope(principal or arguments["principal"], arguments.get("customer", arguments.get("customer_id")), admin=True)
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
                                  supplier_id=getattr(request, "supplier_id", "valio"),
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
                                  bound=bound, supplier_id=lookup.need.supplier_id)
    elif route == "orders.place_order":
        quote = check_owner(gateway.orders._quote(conn, arguments["quote_id"]),
                            None if arguments["operator"] else agent, "QUOTE_NOT_FOUND")
        order_id = saved["value"]["id"]
        bound = ("order", order_id) if gateway.mandates.stored(conn, "order", order_id) else ("quote", quote.id)
        if (arguments["mandate"] is not None or gateway.mandates.stored(conn, *bound) is not None
                or agent in gateway.policy.get().agents):
            gateway.mandates.authorize(conn, arguments["mandate"], agent, quote.customer_id,
                products=[line["product_id"] for line in saved["value"]["lines"]], bound=bound, supplier_id=quote.supplier_id)
        # Spec 06 R-2 explicitly permits returning an existing policy-row order
        # after its mandate was removed. It never grants a new purchase.
    elif route in {"orders.approve", "orders.reject", "orders.cancel"}:
        order = gateway.orders._order(conn, arguments["order_id"])
        if route == "orders.cancel":
            actor = arguments["actor"]
            check_owner(order, actor.removeprefix("agent:") if actor.startswith("agent:") else owner,
                        "ORDER_NOT_FOUND")
            if actor.startswith("agent:"):
                gateway.mandates.authorize(conn, arguments.get("mandate"), actor.removeprefix("agent:"), order.customer_id,
                    products=[line.product_id for line in order.lines], bound=("order", order.id), supplier_id=order.supplier_id)
        else:
            authorize_review(gateway.policy.get(), principal, order)
            if route == "orders.approve":
                # Rejecting needs no purchasing authority; approving still does.
                gateway.mandates.authorize(conn, None, order.agent_id, order.customer_id,
                    products=[line.product_id for line in order.lines], bound=("order", order.id), supplier_id=order.supplier_id)
    else:
        raise RuntimeError(f"No replay authorization registered for {route}")


KEYED_ROUTES = frozenset({"orders.create_quote", "orders.place_order", "orders.cancel", "orders.approve",
                          "orders.reject", "gateway.find_offers", "baskets.find_basket_offers", "policy.update", "mandates.revoke", "autonomy.change",
                           "buyer.control", "buyer.revoke",
                          "webhooks.create", "webhooks.update", "webhooks.disable", "webhooks.redeliver", "connections.create",
                          "recurring.create", "recurring.baseline", "recurring.run", "recurring.pause",
                          "proposals.submit", "proposals.action", "proposals.put_preferences", "memory.act", "memory.put_preferences"})


def omit_anomaly_defaults(route, values):
    # Preserve saved command hashes from before this additive policy section.
    # Field-presence remains in v2 hashes; non-default thresholds stay bound.
    if route == "policy.update":
        from .models import AnomalyPolicy
        policy = values.get("request", {}).get("policy", {})
        if policy.get("anomalies") == AnomalyPolicy().model_dump():
            policy.pop("anomalies")
    return values


def omit_policy_defaults(route, values):
    if route == "policy.update":
        # This field was added after command records existed. Omission and
        # its default are the same payload; retain all other payload fields.
        from .models import ApprovalPolicy
        approval = values.get("request", {}).get("policy", {}).get("approval", {})
        if approval.get("max_auto_daily_total_eur") == ApprovalPolicy.model_fields["max_auto_daily_total_eur"].default:
            approval.pop("max_auto_daily_total_eur")
    return values


def fingerprint(scope, route, arguments, *, policy_defaults=False):
    # agent_id may identify a route target (autonomy), so it is never discarded.
    # Principal metadata and presented credentials are authority, not request payload.
    excluded = {"principal", "actor", "operator", "mandate", "idempotency_key"}
    if route == "connections.create":
        excluded.update({"origin", "mcp_served"})
    values = json_value({k: v for k, v in arguments.items() if k not in excluded})
    if route == "gateway.find_offers" and values.get("product_ids") is None:
        values.pop("product_ids", None)  # Preserve previously issued command fingerprints.
    omit_anomaly_defaults(route, values)
    if not policy_defaults:
        omit_policy_defaults(route, values)
    payload = {"principal": scope, "route": route, "arguments": values}
    if route == "policy.update" and not arguments.get("reset"):
        policy = arguments["request"].policy
        payload["policy_presence"] = {"sections": sorted(policy.model_fields_set),
                                      "shortlist": sorted(policy.shortlist.model_fields_set)}
    return "v2:" + sha256(canonical_json(payload)).hexdigest()


def matches_command(previous, digest, name, route, arguments, saved):
    if previous["digest"] in {digest, digest.removeprefix("v2:")}:
        return True
    if route == "policy.update" and previous["command"] == name:
        # Also accept records issued while the new default was serialized.
        if previous["digest"] == fingerprint(previous["principal"], route, arguments, policy_defaults=True):
            return True
        if not previous["digest"].startswith("v2:"):
            # Lane D's v2 records predate the field-presence discriminator.
            # New prefixed records must still distinguish omitted policy sections.
            values = json_value({k: v for k, v in arguments.items()
                                 if k not in {"principal", "actor", "operator", "mandate", "idempotency_key"}})
            for include_defaults in (True, False):
                if not include_defaults:
                    omit_policy_defaults(route, values)
                payload = {"principal": previous["principal"], "route": route, "arguments": values}
                older = json.loads(json.dumps(payload))
                omit_anomaly_defaults(route, older["arguments"])
                if previous["digest"] in {sha256(canonical_json(p)).hexdigest() for p in (payload, older)}:
                    return True
    if saved.get("fingerprint_version") is not None:
        return False
    # Earlier command envelopes remain replayable. Their other path parameters
    # were hashed already; autonomy's omitted target must also match the saved result.
    legacy = {"command": name, "arguments": json_value({k: v for k, v in arguments.items()
              if k not in {"agent_id", "actor", "operator", "mandate", "idempotency_key"}})}
    if route == "gateway.find_offers" and legacy["arguments"].get("product_ids") is None:
        legacy["arguments"].pop("product_ids", None)
    # Lane B excluded the resolved principal from its earlier payload hashes.
    lane_legacy = {"command": name, "arguments": {k: v for k, v in legacy["arguments"].items() if k != "principal"}}
    lane_versioned = dict(lane_legacy)
    if route == "policy.update" and not arguments.get("reset"):
        policy = arguments["request"].policy
        lane_versioned["policy_presence"] = {"sections": sorted(policy.model_fields_set),
                                             "shortlist": sorted(policy.shortlist.model_fields_set)}
    compatible = {sha256(canonical_json(value)).hexdigest() for value in (legacy, lane_legacy)}
    compatible.add("v2:" + sha256(canonical_json(lane_versioned)).hexdigest())
    for value in (legacy, lane_legacy):
        older = json.loads(json.dumps(value))
        omit_anomaly_defaults(route, older["arguments"])
        compatible.add(sha256(canonical_json(older)).hexdigest())
        omit_policy_defaults(route, value["arguments"])
        compatible.add(sha256(canonical_json(value)).hexdigest())
        omit_anomaly_defaults(route, value["arguments"])
        compatible.add(sha256(canonical_json(value)).hexdigest())
    compatible.add("v2:" + sha256(canonical_json(lane_versioned)).hexdigest())
    return (previous["digest"] in compatible
            and (route != "autonomy.change" or saved["value"].get("agent_id") == arguments["agent_id"]))


def install(gateway, target, name, category):
    operation = getattr(target, name)
    signature = inspect.signature(operation)
    namespace = next(key for key in ("orders", "policy", "mandates", "autonomy", "buyer", "webhooks", "baskets", "lookup", "connections", "recurring", "proposals", "memory") if getattr(gateway, key, None) is target) if target is not gateway else "gateway"
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
    def execute(*args, **kwargs):
        supplied_key = kwargs.pop("command_key", None)
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        arguments = bound.arguments
        principal = PRINCIPAL.get() or arguments.get("principal")
        if route == "recurring.run" and principal is None:
            from .auth import Principal
            row = gateway.recurring.get(arguments["id"])
            principal = Principal(row["agent_id"], customer_ids=(row["customer_id"],))
        identity = principal.agent_id if principal else arguments.get("agent_id") or (arguments.get("actor") or "agent:external").split(":", 1)[-1]
        scope = f"{principal.kind}:{principal.id}" if principal else f"agent:{identity}"
        key = supplied_key if supplied_key is not None else arguments.get("idempotency_key")
        digest = fingerprint(scope, route, arguments)

        tokens = CORRELATION.set(CORRELATION.get() or correlation_id()), CAUSATION.set(key)
        trace_token = TRACE.set(TRACE.get() or trace_id())
        prepared_token = None
        try:
            def preflight():
                if category != "read" and route != "recurring.run" and (principal is None or principal.kind == "agent"):
                    from .buyer import check_suspension
                    check_suspension(gateway.db, identity)
                if key is not None:
                    if not key.strip():
                        raise DomainError("IDEMPOTENCY_REQUIRED", "A non-empty idempotency key is required", 422)
                    if len(key) > 200:
                        raise DomainError("VALIDATION_ERROR", "Idempotency keys must be at most 200 characters", 422)
                    if route not in KEYED_ROUTES:
                        raise DomainError("VALIDATION_ERROR", "This read operation does not accept an idempotency key", 422)
            command_checks(gateway, route, arguments, principal, preflight)
            # Verify receivers before taking the shared command writer lock.
            prepared_credit = None
            if key is not None and route in {"webhooks.create", "webhooks.update"}:
                with gateway.db.connect() as conn:
                    previous = conn.execute("SELECT 1 FROM commands WHERE principal=? AND key=?", (scope, key)).fetchone()
                if previous is None:
                    command_checks(gateway, route, arguments, principal, lambda: charge(gateway, identity, category))
                    prepared_token = gateway.webhooks.prepare(route, arguments, principal)
                    prepared_credit = ADMISSION.set({"identity": identity, "category": category, "used": False})
            if key is None:
                if route != "recurring.run":
                    command_checks(gateway, route, arguments, principal, lambda: charge(gateway, identity, category))
                result = linked(operation(*args, **kwargs))
                from .models import Order
                if isinstance(result, Order) and result.status == "PENDING_EXECUTION":
                    gateway.orders.dispatch_pending(result.id)
                    with gateway.db.connect() as conn:
                        result = gateway.orders._order(conn, result.id)
                if route in {"orders.approve", "orders.reject"} and result.status != "PENDING_APPROVAL":
                    from .telemetry import metric
                    with gateway.db.connect() as conn:
                        reviewed_at = conn.execute("SELECT ts FROM audit WHERE order_id=? AND action IN ('APPROVE','REJECT') ORDER BY id DESC LIMIT 1", (result.id,)).fetchone()
                    if reviewed_at:
                        metric("approval.wait", max(0, (datetime.fromisoformat(reviewed_at[0]) - result.created_at).total_seconds()))
                return result
            # SQLite serialises command lookup, domain changes and saved results as one commit.
            error = None
            resumed_execution = False
            with gateway.db.connect(write=True) as conn:
                previous = conn.execute("SELECT * FROM commands WHERE principal=? AND key=?", (scope, key)).fetchone()
                legacy = None
                if not previous and name == "place_order":
                    storage_key = sha256((identity + "\x00" + key).encode()).hexdigest()
                    legacy = conn.execute("SELECT data FROM orders WHERE idempotency_key=? OR "
                        "(idempotency_key=? AND json_extract(data,'$.agent_id')=?)",
                        (storage_key, key, identity)).fetchone()
                try:
                    command_checks(gateway, route, arguments, principal,
                                   lambda: charge(gateway, identity, "read" if previous or legacy else category)
                                   if route != "recurring.run" or previous else None)
                    if previous:
                        saved = json.loads(previous["result"])
                        def check_replay():
                            if not matches_command(previous, digest, name, route, arguments, saved):
                                if previous["command"] == name and route == "autonomy.change" and "fingerprint_version" not in saved:
                                    replay_authorization(gateway, route, conn, arguments, principal, saved)
                                raise DomainError("IDEMPOTENCY_CONFLICT", "Idempotency key belongs to another quote or agent, or a changed command payload", 409)
                            replay_authorization(gateway, route, conn, arguments, principal, saved)
                        command_checks(gateway, route, arguments, principal, check_replay)
                        if saved.get("error"):
                            refusal = saved["error"]
                            error = DomainError("EVIDENCE_CHANGED", refusal["message"], refusal["status"], refusal["data"])
                        elif name == "place_order":
                            # Revalidate current authority and reconcile uncertain supplier execution.
                            was_pending = gateway.orders._order(conn, saved["value"]["id"]).status == "PENDING_EXECUTION"
                            current = linked(operation(*args, **kwargs))
                            from .models import Order
                            result = Order.model_validate(saved["value"], context={"stored": True})
                            if result.status == "PENDING_EXECUTION" or was_pending:
                                resumed_execution = True
                                result = current
                        else:
                            from . import models
                            model = getattr(models, saved["model"]) if saved["model"] else None
                            result = model.model_validate(saved["value"], context={"stored": True}) if model else saved["value"]
                    else:
                        result = linked(operation(*args, **kwargs))

                except DomainError as caught:
                    error = caught
                    if not previous and caught.code == "EVIDENCE_CHANGED":
                        conn.execute("INSERT INTO commands VALUES(?,?,?,?,?)",
                            (scope, key, name, digest, json.dumps({"error": {"message": caught.message,
                             "status": caught.status, "data": caught.data}})))
                else:
                    if not previous and error is None:
                        saved_value = json_value(result)
                        if route == "connections.create":
                            saved_value["key"] = None
                        conn.execute("INSERT INTO commands VALUES(?,?,?,?,?)",
                                     (scope, key, name, digest, json.dumps({"model": type(result).__name__ if isinstance(result, BaseModel) else None,
                                                                         "value": saved_value, "fingerprint_version": 2})))

            if error:
                raise error
            from .models import Order
            if isinstance(result, Order) and result.status == "PENDING_EXECUTION":
                gateway.orders.dispatch_pending(result.id)
                with gateway.db.connect() as conn:
                    result = gateway.orders._order(conn, result.id)
            if route in {"orders.approve", "orders.reject"} and not previous and result.status != "PENDING_APPROVAL":
                from .telemetry import metric
                with gateway.db.connect() as conn:
                    reviewed_at = conn.execute("SELECT ts FROM audit WHERE order_id=? AND action IN ('APPROVE','REJECT') ORDER BY id DESC LIMIT 1", (result.id,)).fetchone()
                if reviewed_at:
                    metric("approval.wait", max(0, (datetime.fromisoformat(reviewed_at[0]) - result.created_at).total_seconds()))
            if route == "orders.place_order" and result.status != "PENDING_EXECUTION" and gateway.db._connection.get() is None:
                with gateway.db.connect(write=True) as conn:
                    envelope = conn.execute("SELECT result FROM commands WHERE principal=? AND key=?", (scope, key)).fetchone()
                    if envelope:
                        saved = json.loads(envelope[0])
                        if saved.get("value", {}).get("status") == "PENDING_EXECUTION" or resumed_execution:
                            saved["value"] = json_value(result)
                            conn.execute("UPDATE commands SET result=? WHERE principal=? AND key=?", (json.dumps(saved), scope, key))
            return result
        except DomainError as error:
            failure_audit(gateway, route, arguments, identity, error)
            raise
        finally:
            if prepared_token is not None:
                gateway.webhooks.prepared.reset(prepared_token)
            if 'prepared_credit' in locals() and prepared_credit is not None:
                ADMISSION.reset(prepared_credit)
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
                return result
        finally:
            with gateway.activate():
                metric("request.latency", time.perf_counter() - started)
            SUPPLIER.reset(token)

    setattr(target, name, command)
