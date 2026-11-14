"""Ownership of recorded events, including pre-context historical aggregates."""
import json

from .autonomy import buyer_scope


def event_visible(gateway, conn, event, principal):
    if principal.operator:
        return True
    payload = event["payload"]
    context = payload.get("context") or {}
    customers = [event["customer_id"]] if event["customer_id"] else context.get("customer_ids", [])
    if not customers or not set(customers) <= set(principal.customer_ids):
        return False
    organisation = context.get("organisation_id") or payload.get("organisation_id")
    if organisation and organisation != principal.organisation_id:
        return False
    kind = event["aggregate_type"]
    if kind in {"proposal", "suggestion", "stockout"}:
        # These are customer-wide buyer resources, not unowned historical purchases.
        return principal.kind in {"agent", "buyer_user"}
    table = {"order": "orders", "quote": "quotes", "request": "lookups", "task": "a2a_tasks"}.get(kind)
    owners = set()
    if table:
        row = conn.execute(f"SELECT data FROM {table} WHERE id=?", (event["aggregate_id"],)).fetchone()
        obj = json.loads(row[0]) if row else {}
        owner = obj.get("agent_id") if kind != "task" else obj.get("metadata", {}).get("owner")
        if owner:
            owners.add(owner)
    elif kind in {"agent", "autonomy"}:
        owners.add(event["aggregate_id"])
    elif payload.get("agent_id"):
        owners.add(payload["agent_id"])
    elif kind == "mandate":
        from .mandates import decode
        for row in conn.execute("SELECT jwt FROM mandate_credentials"):
            claims = decode(row[0])[1]
            subject = claims["vc"]["credentialSubject"]
            if (claims["jti"] in payload.get("jti_revoked", []) and claims["iss"] == payload.get("issuer")
                    and subject["customer_id"] in customers):
                owners.add(subject["agent_id"])
    if not owners:
        # Unowned historical procurement records are operator-only. Buyer-wide
        # controls may instead carry an explicit recorded organisation.
        return bool(organisation and principal.kind in {"buyer_user", "tenant_user"})
    if event["actor"].startswith("agent:") and event["actor"][6:] not in owners:
        return False
    policy = gateway.policy.get()
    return all((owner == principal.id if principal.kind == "agent" else
                principal.kind in {"buyer_user", "tenant_user"} and buyer_scope(policy, principal, owner, customers)) for owner in owners)


def audit_visible(row, agent_id):
    """A foreign refusal linked to a target is not that target's evidence."""
    data = row["data"] if isinstance(row["data"], dict) else json.loads(row["data"])
    if data.get("error") in {"ORDER_NOT_FOUND", "QUOTE_NOT_FOUND", "LOOKUP_NOT_FOUND"}:
        return False
    actor = row["actor"]
    return not actor.startswith("agent:") or actor == f"agent:{agent_id}"
