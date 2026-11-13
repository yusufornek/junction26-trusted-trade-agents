"""Read-only operator metrics from persisted orders, lookup evidence and audit events."""

import json
from collections import Counter
from datetime import timezone
from decimal import Decimal

from .models import AuditEntry, LookupResult, Order, Stats

ORDER_STATUSES = ("CONFIRMED", "PENDING_APPROVAL", "REJECTED", "EXPIRED", "CANCELLED")
PLACEMENT_STATUSES = ORDER_STATUSES[:3]


def read_stats(db, since=None):
    if since is not None:
        since = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since.astimezone(timezone.utc)
    with db.connect() as conn:
        # All reads share a SQLite snapshot, including the lookup bodies behind audit references.
        conn.execute("BEGIN")
        events = [AuditEntry(**{**dict(row), "data": json.loads(row["data"])})
                  for row in conn.execute("SELECT * FROM audit ORDER BY id")]
        events = [event for event in events if since is None or event.ts >= since]
        orders = [Order.model_validate_json(row[0]) for row in conn.execute("SELECT data FROM orders")]
        orders = [order for order in orders if since is None or order.created_at >= since]
        lookup_events = [event for event in events if event.action == "LOOKUP"]
        warnings = Counter()
        for event in lookup_events:
            row = conn.execute("SELECT data FROM lookups WHERE id=?", (event.data.get("lookup_id"),)).fetchone()
            if row:
                lookup = LookupResult.model_validate_json(row[0])
                warnings.update(w.code for offer in lookup.offers for w in offer.warnings)

    statuses = Counter(order.status for order in orders)
    placements, rules = Counter(), Counter()
    for event in events:
        if event.action != "PLACE_ORDER":
            continue
        outcome = event.data.get("outcome")
        if outcome is None:
            # Pre-control-plane audit rows recorded the placement status in this stable summary.
            outcome = next((status for status in PLACEMENT_STATUSES
                            if event.summary.startswith(f"Order {status} at ")), None)
        if outcome in PLACEMENT_STATUSES:
            placements[outcome] += 1
        rules.update(hit["rule"] for hit in event.data.get("rule_hits", []))

    count = len(lookup_events)
    agents = sorted({event.actor for event in events if event.actor.startswith("agent:")})
    agent_orders = Counter(f"agent:{order.agent_id}" for order in orders)
    spend = sum((Decimal(str(order.total)) for order in orders
                 if order.status == "CONFIRMED" and order.currency == "EUR"), Decimal(0))
    return Stats(
        since=since, lookups=count,
        orders_by_status={status: statuses[status] for status in ORDER_STATUSES},
        placement_outcomes={status: placements[status] for status in PLACEMENT_STATUSES},
        human_decisions={"approved": sum(e.action == "APPROVE" and e.actor.startswith("human:") for e in events),
                         "rejected": sum(e.action == "REJECT" and e.actor.startswith("human:") for e in events)},
        confirmed_spend_total_eur=float(spend),
        average_live_calls_per_lookup={endpoint: sum(e.data.get("live_calls", {}).get(endpoint, 0)
                                                     for e in lookup_events) / count if count else 0
                                       for endpoint in ("price", "availability")},
        total_decisions_calls=sum(e.data.get("decision_calls", 0) for e in events),
        top_warning_codes=[{"code": code, "count": n} for code, n in sorted(warnings.items(), key=lambda x: (-x[1], x[0]))[:5]],
        top_rule_hits=[{"rule": rule, "count": n} for rule, n in sorted(rules.items(), key=lambda x: (-x[1], x[0]))[:5]],
        agents_seen=[{"actor": actor, "order_count": agent_orders[actor]} for actor in agents],
    )
