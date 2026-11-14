from datetime import timedelta
from decimal import Decimal
import json
from uuid import uuid4

from . import audit
from .auth import check_owner
from .catalog import hard_violations
from .db import DomainError
from .lookup import availability_for, offer_for, reserved
from .models import LiveCalls, LookupResult, OfferCard, Order, OrderTimeline, Quote, QuoteLineRequest, TimelineStep
from .valio import RequestCounter


def evidence_changes(before, after):
    """Compare decision facts only: timestamps, wording and excess stock are not evidence changes."""
    changes = []

    def codes(old, new, label, noun):
        changes.extend(f"{label}new {noun} {code}" for code in sorted(new - old))
        changes.extend(f"{label}removed {noun} {code}" for code in sorted(old - new))

    old_lines = {line.product_id: line for line in before.lines}
    new_lines = {line.product_id: line for line in after.lines}
    for product_id in sorted(old_lines.keys() | new_lines.keys()):
        old, new = old_lines.get(product_id), new_lines.get(product_id)
        if old is None or new is None:
            changes.append(f"{product_id}: {'added' if old is None else 'removed'} order line")
            continue
        if old.units_needed != new.units_needed:
            changes.append(f"{product_id}: quantity {old.units_needed} → {new.units_needed} sales units")
        old_covered = min(old.units_needed, old.availability.available_amount)
        new_covered = min(new.units_needed, new.availability.available_amount)
        if old_covered != new_covered:
            changes.append(f"{product_id}: covered {old_covered:g} → {new_covered:g} of {new.units_needed:g}")
        if old.availability.available_date != new.availability.available_date:
            changes.append(f"{product_id}: delivery date {old.availability.available_date} → {new.availability.available_date}")
        codes({w.code for w in old.warnings}, {w.code for w in new.warnings}, f"{product_id}: ", "warning")
    codes({w.code for w in before.warnings}, {w.code for w in after.warnings}, "Order: ", "warning")
    codes({h.rule for h in before.policy.hits}, {h.rule for h in after.policy.hits}, "Order: ", "policy rule")
    return changes


class Orders:
    def __init__(self, db, catalog, valio, policy):
        self.db, self.catalog, self.valio, self.policy = db, catalog, valio, policy

    def _run(self, action, actor, inputs, operation):
        counter = RequestCounter()
        try:
            return operation(counter)
        except DomainError as error:
            # A changed revision is already persisted and audited atomically, with its actual calls.
            # Do not add a second failed event that would double-count the same refresh in the timeline.
            if error.code == "EVIDENCE_CHANGED" and error.data.get("changes"):
                raise
            if error.code in {"EXPIRED_QUOTE", "EXPIRED_ORDER"}:
                self.expire()
            with self.db.connect(write=True) as conn:
                audit.append(conn, actor, action + "_FAILED", error.message,
                             order_id=inputs.get("order_id"), quote_id=inputs.get("quote_id"),
                             data={"inputs": inputs, "error": error.code, "live_calls": counter.live().model_dump(),
                                   **({"stated_name": inputs["stated_name"]} if "stated_name" in inputs else {}), **error.data})
            raise

    @staticmethod
    def _quote(conn, quote_id):
        row = conn.execute("SELECT data FROM quotes WHERE id=?", (quote_id,)).fetchone()
        if not row:
            raise DomainError("QUOTE_NOT_FOUND", "Quote was not found", 404)
        return Quote.model_validate_json(row["data"])

    @staticmethod
    def _order(conn, order_id):
        row = conn.execute("SELECT data FROM orders WHERE id=?", (order_id,)).fetchone()
        if not row:
            raise DomainError("ORDER_NOT_FOUND", "Order was not found", 404)
        return Order.model_validate_json(row["data"])

    @staticmethod
    def _save_quote(conn, quote):
        conn.execute("UPDATE quotes SET status=?,expires_at=?,data=? WHERE id=?",
                     (quote.status, quote.expires_at.isoformat(), quote.model_dump_json(), quote.id))

    @staticmethod
    def _save_order(conn, order):
        conn.execute("UPDATE orders SET status=?,data=? WHERE id=?", (order.status, order.model_dump_json(), order.id))

    def expire(self):
        now = audit.utcnow()
        with self.db.connect(write=True) as conn:
            for row in conn.execute("SELECT data FROM quotes WHERE status='OPEN'").fetchall():
                quote = Quote.model_validate_json(row["data"])
                if quote.expires_at <= now:
                    quote.status = "EXPIRED"
                    self._save_quote(conn, quote)
                    audit.append(conn, "system", "QUOTE_EXPIRED", "The fixed-price quote expired.", quote_id=quote.id)
            for row in conn.execute("SELECT data FROM orders WHERE status='PENDING_APPROVAL'").fetchall():
                order = Order.model_validate_json(row["data"])
                if order.expires_at <= now:
                    order.status = "EXPIRED"
                    self._save_order(conn, order)
                    conn.execute("DELETE FROM reservations WHERE order_id=?", (order.id,))
                    audit.append(conn, "system", "ORDER_EXPIRED", "Approval deadline passed; local reservations released.",
                                 order_id=order.id, quote_id=order.quote_id)

    def create_quote(self, request, agent_id="external", *, operator=False):
        self.expire()
        actor = f"agent:{agent_id}"
        return self._run("CREATE_QUOTE", actor, request.model_dump(mode="json"),
                         lambda counter: self._create_quote(request, actor, counter, operator))

    def _create_quote(self, request, actor, counter, operator=False):
        agent_id = actor.removeprefix("agent:")
        if agent_id not in self.policy.get().agents:
            self.policy.check_customer(agent_id, request.customer_id)
        if not request.need_ref:
            self.policy.check_customer(agent_id, request.customer_id)
            raise DomainError("NEED_REF_REQUIRED", "Explicit lines require need_ref to a stored request", 422)
        lookup = None
        if request.need_ref:
            with self.db.connect() as conn:
                row = conn.execute("SELECT data FROM lookups WHERE id=?", (request.need_ref,)).fetchone()
            if not row:
                raise DomainError("NEED_NOT_FOUND", "need_ref must be a lookup_id returned by this gateway", 404)
            lookup = LookupResult.model_validate_json(row["data"], context={"stored": True})
            check_owner(lookup, None if operator else agent_id, "LOOKUP_NOT_FOUND")
            self.policy.check_customer(agent_id, request.customer_id)
            if lookup.need.customer_id != request.customer_id or lookup.need.deliver_by != request.deliver_by:
                raise DomainError("NEED_MISMATCH", "Quote customer and delivery date must match need_ref", 422)
        plan = None
        requested_lines = request.lines
        if request.plan_ref:
            if request.plan_ref.plan_index >= len(lookup.plans):
                raise DomainError("INVALID_PLAN_INDEX", "plan_index is outside the stored lookup's plans", 422)
            plan = lookup.plans[request.plan_ref.plan_index]
            requested_lines = [QuoteLineRequest(product_id=line.product_id, quantity_sales_units=line.units)
                               for line in plan.lines]
        products = {line.product_id: self.catalog.get(line.product_id) for line in requested_lines}
        for line in requested_lines:
            p = products[line.product_id]
            step = Decimal(str(p.allowed_lot_size if p.sales_unit == "KG" else 1))
            if Decimal(str(line.quantity_sales_units)) % step:
                raise DomainError("INVALID_LOT_SIZE", f"{p.id}: sales quantity must be a multiple of {step}", 422)
        ids = list(products)
        price = self.valio.fetch("price", request.customer_id, request.deliver_by, ids, counter, fresh=True)
        stock = self.valio.fetch("availability", request.customer_id, request.deliver_by, ids, counter, fresh=True)
        now = audit.utcnow()
        need = lookup.need
        with self.db.connect() as conn:
            evidence = conn.execute("SELECT data FROM lookup_offers WHERE lookup_id=?", (lookup.lookup_id,)).fetchone()
        all_offers = [OfferCard.model_validate(o) for o in json.loads(evidence[0])] if evidence else lookup.offers
        fit_by_id = {o.product_id: {"probability": o.fit, "question": o.fit_question or ""}
                     for o in all_offers if o.fit is not None}
        with self.db.connect(write=True) as conn:
            # Recheck after taking the same lock used by policy updates.
            self.policy.check_customer(actor.removeprefix("agent:"), request.customer_id)
            need, effective = self.policy.effective_need(lookup.need)
            lines, violations, expiries = [], [], [now + timedelta(minutes=30)]
            for line in requested_lines:
                p = products[line.product_id]
                offer = offer_for(p, need, price[p.id], stock[p.id], fit_by_id.get(p.id),
                                  sales_quantity=line.quantity_sales_units, reserved_amount=reserved(conn, p.id), sources=effective)
                if offer.unit_price is None or any(w.code == "PRICE_ERROR" for w in offer.warnings):
                    raise DomainError("PRICE_ERROR", f"Cannot lock an invalid price for {p.id}", 502)
                if offer.currency != "EUR":
                    raise DomainError("UNSUPPORTED_CURRENCY", "The MVP policy supports EUR only", 422)
                if offer.valid_to:
                    expiries.append(offer.valid_to)
                if offer.availability.closing_time and offer.availability.closing_time > now:
                    expiries.append(offer.availability.closing_time)
                violations.extend(hard_violations(p, need.required_claims, need.exclude_allergens, effective))
                lines.append(offer)
            quote = Quote(id=str(uuid4()), agent_id=actor.removeprefix("agent:"), customer_id=request.customer_id, deliver_by=request.deliver_by,
                          need_ref=request.need_ref, plan_ref=request.plan_ref,
                          warnings=[w for w in plan.warnings if w.code == "PARTIAL_STOCK"] if plan else [],
                          status="OPEN", created_at=now, expires_at=min(expiries),
                          lines=lines, total=round(sum(o.total for o in lines), 2), currency="EUR",
                          hard_constraint_violations=violations, live_calls=counter.live())
            if quote.expires_at <= audit.utcnow():
                raise DomainError("EXPIRED_QUOTE", "Price validity ended during quote creation; re-quote")
            conn.execute("INSERT INTO quotes VALUES(?,?,?,?)", (quote.id, quote.status, quote.expires_at.isoformat(), quote.model_dump_json()))
            audit.append(conn, actor, "CREATE_QUOTE", f"Locked {len(lines)} fixed-price lines for {quote.total:.2f} EUR.",
                         quote_id=quote.id, data={"inputs": request.model_dump(mode="json"),
                         "live_calls": counter.live().model_dump(), "fit_scores": {o.product_id: o.fit for o in lines},
                         "hard_constraint_violations": violations, "warnings": {o.product_id: [w.code for w in o.warnings] for o in lines}})
        return quote

    def place_order(self, quote_id, idempotency_key, agent_id="external", *, operator=False):
        if not idempotency_key or not idempotency_key.strip():
            raise DomainError("IDEMPOTENCY_REQUIRED", "A non-empty idempotency key is required", 422)
        self.expire()
        inputs = {"quote_id": quote_id, "idempotency_key": idempotency_key}
        return self._run("PLACE_ORDER", f"agent:{agent_id}", inputs,
                         lambda counter: self._place_order(quote_id, idempotency_key, agent_id, counter, operator))

    def _place_order(self, quote_id, key, agent_id, counter, operator=False):
        # Serializes idempotency, reservation reads and writes across REST/MCP processes.
        with self.db.connect(write=True) as conn:
            policy = self.policy.get()
            existing = conn.execute("SELECT data FROM orders WHERE idempotency_key=?", (key,)).fetchone()
            if not existing and agent_id not in policy.agents:
                raise DomainError("AGENT_NOT_REGISTERED", f"Agent {agent_id} has no mandate; ordering requires a mandate", 403)
            quote = self._quote(conn, quote_id)
            check_owner(quote, None if operator else agent_id, "QUOTE_NOT_FOUND")
            if existing:
                order = Order.model_validate_json(existing["data"])
                if order.quote_id != quote_id or order.agent_id != agent_id:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "Idempotency key belongs to another quote or agent")
                audit.append(conn, f"agent:{agent_id}", "ORDER_REPLAY", "Idempotent retry returned the existing order.",
                             order_id=order.id, quote_id=order.quote_id,
                             data={"inputs": {"idempotency_key": key}, "live_calls": counter.live().model_dump()})
                return order
            self.policy.check_customer(agent_id, quote.customer_id, policy)
            if quote.status == "EXPIRED" or quote.expires_at <= audit.utcnow():
                raise DomainError("EXPIRED_QUOTE", "Quote expired; create a new quote before ordering")
            if quote.status != "OPEN":
                raise DomainError("QUOTE_ALREADY_ACCEPTED", "This quote already has an order")
            stock = self.valio.fetch("availability", quote.customer_id, quote.deliver_by,
                                     [o.product_id for o in quote.lines], counter, fresh=True)
            if quote.expires_at <= audit.utcnow():
                raise DomainError("EXPIRED_QUOTE", "Quote expired while checking stock; create a new quote")
            lines = self._refresh_stock(conn, quote.lines, quote.deliver_by, stock)
            # Re-read catalog hard facts as well as preserving original quote violations.
            now = audit.utcnow()
            result = self.policy.evaluate_order(conn, quote.total, lines, self._violations(conn, quote),
                                                agent_id=agent_id, customer_id=quote.customer_id,
                                                policy=policy, warnings=quote.warnings)
            order = Order(id=str(uuid4()), quote_id=quote.id, agent_id=agent_id, idempotency_key=key,
                          status=result.outcome, created_at=now, expires_at=quote.expires_at,
                          total=quote.total, currency=quote.currency, lines=lines, warnings=quote.warnings,
                          policy=result, live_calls=counter.live())
            conn.execute("INSERT INTO orders VALUES(?,?,?,?,?)", (order.id, quote.id, key, order.status, order.model_dump_json()))
            quote.status = "ACCEPTED"
            self._save_quote(conn, quote)
            self._reserve(conn, order)
            audit.append(conn, f"agent:{agent_id}", "PLACE_ORDER", f"Order {order.status} at the locked total of {order.total:.2f} EUR.",
                         order_id=order.id, quote_id=quote.id, data={"inputs": {"idempotency_key": key},
                         "outcome": order.status,
                         "rule_hits": [h.model_dump() for h in result.hits], "fit_scores": {o.product_id: o.fit for o in lines},
                         "live_calls": counter.live().model_dump()})
            return order

    def _violations(self, conn, quote):
        violations = [] if quote.need_ref else list(quote.hard_constraint_violations)
        if quote.need_ref:
            lookup = LookupResult.model_validate_json(conn.execute("SELECT data FROM lookups WHERE id=?", (quote.need_ref,)).fetchone()[0], context={"stored": True})
            need, effective = self.policy.effective_need(lookup.need)
            for line in quote.lines:
                violations.extend(hard_violations(self.catalog.get(line.product_id), need.required_claims, need.exclude_allergens, effective))
        return sorted(set(violations))

    @staticmethod
    def _refresh_stock(conn, lines, deliver_by, stock, exclude_order=None):
        refreshed = []
        stock_codes = {"PARTIAL_STOCK", "DATE_SHIFT", "CUTOFF_PASSED", "CUTOFF_UNKNOWN", "SALES_BLOCK", "UNKNOWN_PRODUCT",
                       "AVAILABILITY_ERROR", "UNIT_MISMATCH", "STOCK_MODEL_UNKNOWN", "NO_STOCK"}
        for original in lines:
            line = original.model_copy(deep=True)
            raw = stock[line.product_id]
            line.availability, warnings = availability_for(raw, deliver_by, line.units_needed,
                                                           reserved(conn, line.product_id, exclude_order), line.sales_unit)
            line.warnings = [w for w in line.warnings if w.code not in stock_codes] + warnings
            from .lookup import timestamp
            line.provenance["availability"].fetched_at = timestamp(raw.get("_fetched_at")) or audit.utcnow()
            line.provenance["availability"].valid_to = line.availability.closing_time
            refreshed.append(line)
        return refreshed

    @staticmethod
    def _reserve(conn, order):
        conn.execute("DELETE FROM reservations WHERE order_id=?", (order.id,))
        if order.status in {"CONFIRMED", "PENDING_APPROVAL"}:
            for line in order.lines:
                if line.availability.logistic_model != "Terminal":
                    conn.execute("INSERT INTO reservations VALUES(?,?,?)", (order.id, line.product_id, line.units_needed))

    def get_quote(self, quote_id, agent_id=None):
        self.expire()
        with self.db.connect() as conn:
            return check_owner(self._quote(conn, quote_id), agent_id, "QUOTE_NOT_FOUND")

    def get_order(self, order_id, agent_id=None):
        self.expire()
        with self.db.connect() as conn:
            return check_owner(self._order(conn, order_id), agent_id, "ORDER_NOT_FOUND")

    def timeline(self, order_id, agent_id=None):
        self.expire()
        with self.db.connect() as conn:
            # Order state, quote reference and audit history share one read snapshot.
            conn.execute("BEGIN")
            order = self._order(conn, order_id)
            check_owner(order, agent_id, "ORDER_NOT_FOUND")
            quote = self._quote(conn, order.quote_id)
            events = audit.read_timeline(conn, order.id, quote.id, quote.need_ref)
        stages = {"LOOKUP": "lookup", "CREATE_QUOTE": "quote", "PLACE_ORDER": "order",
                  "ORDER_REPLAY": "order", "APPROVE": "decision", "REJECT": "decision",
                  "EVIDENCE_CHANGED": "decision",
                  "CANCEL": "cancellation"}
        steps = [TimelineStep(
            ts=event.ts, actor=event.actor, action=event.action,
            step=stages.get(event.action.removesuffix("_FAILED"), "system"),
            summary=event.summary, data=event.data,
            live_calls=LiveCalls.model_validate(event.data.get("live_calls") or {}),
            decision_calls=event.data.get("decision_calls") or 0,
        ) for event in events]
        return OrderTimeline(
            order_id=order.id, quote_id=quote.id, lookup_id=quote.need_ref,
            agent_id=order.agent_id, status=order.status, steps=steps,
            total_live_calls=LiveCalls(price=sum(step.live_calls.price for step in steps),
                                       availability=sum(step.live_calls.availability for step in steps)),
            total_decision_calls=sum(step.decision_calls for step in steps),
        )

    def list_orders(self, pending_only=False, agent_id=None):
        self.expire()
        with self.db.connect() as conn:
            sql = "SELECT data FROM orders" + (" WHERE status='PENDING_APPROVAL'" if pending_only else "") + " ORDER BY rowid DESC"
            orders = [Order.model_validate_json(r[0]) for r in conn.execute(sql)]
            return [order for order in orders if agent_id is None or order.agent_id == agent_id]

    def approve(self, order_id, approver, comment="", evidence_revision=None, *, actor=None):
        return self._review(order_id, approver, comment, True, evidence_revision, actor=actor)

    def reject(self, order_id, approver, comment="", *, actor=None):
        return self._review(order_id, approver, comment, False, actor=actor)

    def _review(self, order_id, approver, comment, approve, evidence_revision=None, *, actor=None):
        self.expire()
        if not approver.strip():
            raise DomainError("APPROVER_REQUIRED", "An approver name is required", 422)
        action = "APPROVE" if approve else "REJECT"
        inputs = {"order_id": order_id, "comment": comment, "stated_name": approver}
        if approve and evidence_revision is not None:
            inputs["evidence_revision"] = evidence_revision
        return self._run(action, actor or f"human:{approver}", inputs,
                         lambda counter: self._review_transaction(order_id, approver, comment, approve, counter,
                                                                  evidence_revision, actor=actor))

    def _review_transaction(self, order_id, approver, comment, approve, counter, evidence_revision=None, *, actor=None):
        changes = []
        with self.db.connect(write=True) as conn:
            order = self._order(conn, order_id)
            if order.status != "PENDING_APPROVAL":
                raise DomainError("INVALID_TRANSITION", "Only PENDING_APPROVAL orders can be approved or rejected")
            if approve and evidence_revision is not None and evidence_revision != order.evidence_revision:
                raise DomainError("EVIDENCE_CHANGED", "Review the current order evidence before approving.",
                                  data={"evidence_revision": order.evidence_revision, "changes": []})
            quote = self._quote(conn, order.quote_id)
            if approve:
                previous = order.model_copy(deep=True)
                stock = self.valio.fetch("availability", quote.customer_id, quote.deliver_by,
                                         [o.product_id for o in order.lines], counter, fresh=True)
                order.lines = self._refresh_stock(conn, order.lines, quote.deliver_by, stock, order.id)
                # Human review may accept delivery warnings, but never hard violations or unverifiable stock.
                # Evaluate as the placing agent so the audit keeps the mandate rules the human signs off
                # (per-order and daily limits); only REJECTED blocks an approval.
                evaluation = self.policy.evaluate_order(conn, order.total, order.lines, self._violations(conn, quote),
                                                        agent_id=order.agent_id, customer_id=quote.customer_id,
                                                        warnings=quote.warnings)
                if evaluation.outcome == "REJECTED":
                    if any(hit.rule == "unregistered_agent" for hit in evaluation.hits):
                        raise DomainError("AGENT_NOT_REGISTERED", "Approval requires a mandate for the placing agent", 403,
                                          data={"rule_hits": [h.model_dump() for h in evaluation.hits]})
                    raise DomainError("HARD_CONSTRAINT_VIOLATION", "Approval cannot override hard constraints or sales blocks",
                                      data={"rule_hits": [h.model_dump() for h in evaluation.hits]})
                if any(w.code == "AVAILABILITY_ERROR" for o in order.lines for w in o.warnings):
                    raise DomainError("AVAILABILITY_ERROR", "Availability must be verified before approval", 502)
                if order.expires_at <= audit.utcnow():
                    raise DomainError("EXPIRED_ORDER", "The approval window expired during stock verification")
                order.policy = evaluation
                order.warnings = quote.warnings
                changes = evidence_changes(previous, order)
                if changes:
                    order.evidence_revision += 1
            if not changes:
                order.status = "CONFIRMED" if approve else "REJECTED"
                order.approver, order.comment = approver, comment
            self._save_order(conn, order)
            self._reserve(conn, order)
            data = {"inputs": {"comment": comment, "evidence_revision": evidence_revision}, "stated_name": approver,
                    "evidence_revision": order.evidence_revision,
                    "rule_hits": [h.model_dump() for h in order.policy.hits],
                    "fit_scores": {o.product_id: o.fit for o in order.lines},
                    "warnings": {o.product_id: [w.code for w in o.warnings] for o in order.lines},
                    "order_warnings": [w.code for w in order.warnings],
                    "live_calls": counter.live().model_dump()}
            if changes:
                audit.append(conn, actor or f"human:{approver}", "EVIDENCE_CHANGED",
                             "Order evidence changed; review before approving: " + "; ".join(changes),
                             order_id=order.id, quote_id=quote.id, data={**data, "changes": changes})
            else:
                audit.append(conn, actor or f"human:{approver}", "APPROVE" if approve else "REJECT",
                             f"Human {approver} {'approved' if approve else 'rejected'} the order.",
                             order_id=order.id, quote_id=quote.id, data=data)
        # Leave the writer transaction successfully so the pending revision and its audit survive the 409.
        if changes:
            raise DomainError("EVIDENCE_CHANGED", "Order evidence changed; review the refreshed order before approving.",
                              data={"evidence_revision": order.evidence_revision, "changes": changes})
        return order

    def cancel(self, order_id, actor="system", reason="", *, stated_name=None):
        """Core transition for a local confirmed order, before any known cutoff."""
        inputs = {"order_id": order_id, "reason": reason}
        if stated_name is not None:
            inputs["stated_name"] = stated_name
        return self._run("CANCEL", actor, inputs,
                         lambda counter: self._cancel(order_id, actor, reason, inputs, counter))

    def _cancel(self, order_id, actor, reason, inputs, counter):
        with self.db.connect(write=True) as conn:
            order = self._order(conn, order_id)
            inputs["quote_id"] = order.quote_id
            if actor.startswith("agent:") and order.agent_id != actor.removeprefix("agent:"):
                raise DomainError("ORDER_NOT_FOUND", "Order was not found", 404)
            if order.status != "CONFIRMED":
                raise DomainError("INVALID_TRANSITION", "Only CONFIRMED orders can be cancelled")
            cutoffs = [o.availability.closing_time for o in order.lines if o.availability.closing_time]
            if any(c <= audit.utcnow() for c in cutoffs):
                raise DomainError("CUTOFF_PASSED", "Cancellation cutoff has passed")
            order.status = "CANCELLED"
            order.cancelled_by = inputs.get("stated_name") or (actor.split(":", 1)[1] if actor.startswith(("human:", "agent:")) else actor)
            order.cancel_reason = reason
            self._save_order(conn, order)
            self._reserve(conn, order)
            audit.append(conn, actor, "CANCEL", "Confirmed order cancelled; local reservations released.",
                         order_id=order.id, quote_id=order.quote_id,
                         data={"reason": reason, "live_calls": counter.live().model_dump(),
                               **({"stated_name": inputs["stated_name"]} if "stated_name" in inputs else {})})
            return order
