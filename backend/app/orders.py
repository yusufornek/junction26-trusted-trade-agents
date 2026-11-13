from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from . import audit
from .catalog import hard_violations
from .db import DomainError
from .lookup import availability_for, offer_for, reserved
from .models import LiveCalls, LookupResult, NeedSpec, Order, OrderTimeline, Quantity, Quote, TimelineStep
from .valio import RequestCounter


class Orders:
    def __init__(self, db, catalog, valio, policy):
        self.db, self.catalog, self.valio, self.policy = db, catalog, valio, policy

    def _run(self, action, actor, inputs, operation):
        counter = RequestCounter()
        try:
            return operation(counter)
        except DomainError as error:
            if error.code in {"EXPIRED_QUOTE", "EXPIRED_ORDER"}:
                self.expire()
            with self.db.connect(write=True) as conn:
                audit.append(conn, actor, action + "_FAILED", error.message,
                             order_id=inputs.get("order_id"), quote_id=inputs.get("quote_id"),
                             data={"inputs": inputs, "error": error.code, "live_calls": counter.live().model_dump(), **error.data})
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

    def create_quote(self, request, agent_id="external"):
        self.expire()
        actor = f"agent:{agent_id}"
        return self._run("CREATE_QUOTE", actor, request.model_dump(mode="json"),
                         lambda counter: self._create_quote(request, actor, counter))

    def _create_quote(self, request, actor, counter):
        lookup = None
        if request.need_ref:
            with self.db.connect() as conn:
                row = conn.execute("SELECT data FROM lookups WHERE id=?", (request.need_ref,)).fetchone()
            if not row:
                raise DomainError("NEED_NOT_FOUND", "need_ref must be a lookup_id returned by this gateway", 404)
            lookup = LookupResult.model_validate_json(row["data"])
            if lookup.need.customer_id != request.customer_id or lookup.need.deliver_by != request.deliver_by:
                raise DomainError("NEED_MISMATCH", "Quote customer and delivery date must match need_ref", 422)
        products = {line.product_id: self.catalog.get(line.product_id) for line in request.lines}
        for line in request.lines:
            p = products[line.product_id]
            step = Decimal(str(p.allowed_lot_size if p.sales_unit == "KG" else 1))
            if Decimal(str(line.quantity_sales_units)) % step:
                raise DomainError("INVALID_LOT_SIZE", f"{p.id}: sales quantity must be a multiple of {step}", 422)
        ids = list(products)
        price = self.valio.fetch("price", request.customer_id, request.deliver_by, ids, counter, fresh=True)
        stock = self.valio.fetch("availability", request.customer_id, request.deliver_by, ids, counter, fresh=True)
        now = audit.utcnow()
        need = lookup.need if lookup else NeedSpec(
            customer_id=request.customer_id, intent="Explicit product selection", deliver_by=request.deliver_by,
            quantity=Quantity(value=1, unit="SALES_UNIT"))
        fit_by_id = {o.product_id: {"probability": o.fit, "question": o.fit_question or ""}
                     for o in lookup.offers if o.fit is not None} if lookup else {}
        with self.db.connect(write=True) as conn:
            lines, violations, expiries = [], [], [now + timedelta(minutes=30)]
            for line in request.lines:
                p = products[line.product_id]
                offer = offer_for(p, need, price[p.id], stock[p.id], fit_by_id.get(p.id),
                                  sales_quantity=line.quantity_sales_units, reserved_amount=reserved(conn, p.id))
                if offer.unit_price is None or any(w.code == "PRICE_ERROR" for w in offer.warnings):
                    raise DomainError("PRICE_ERROR", f"Cannot lock an invalid price for {p.id}", 502)
                if offer.currency != "EUR":
                    raise DomainError("UNSUPPORTED_CURRENCY", "The MVP policy supports EUR only", 422)
                if offer.valid_to:
                    expiries.append(offer.valid_to)
                if offer.availability.closing_time and offer.availability.closing_time > now:
                    expiries.append(offer.availability.closing_time)
                violations.extend(hard_violations(p, need.required_claims, need.exclude_allergens))
                lines.append(offer)
            quote = Quote(id=str(uuid4()), customer_id=request.customer_id, deliver_by=request.deliver_by,
                          need_ref=request.need_ref, status="OPEN", created_at=now, expires_at=min(expiries),
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

    def place_order(self, quote_id, idempotency_key, agent_id="external"):
        if not idempotency_key or not idempotency_key.strip():
            raise DomainError("IDEMPOTENCY_REQUIRED", "A non-empty idempotency key is required", 422)
        self.expire()
        inputs = {"quote_id": quote_id, "idempotency_key": idempotency_key}
        return self._run("PLACE_ORDER", f"agent:{agent_id}", inputs,
                         lambda counter: self._place_order(quote_id, idempotency_key, agent_id, counter))

    def _place_order(self, quote_id, key, agent_id, counter):
        # Serializes idempotency, reservation reads and writes across REST/MCP processes.
        with self.db.connect(write=True) as conn:
            existing = conn.execute("SELECT data FROM orders WHERE idempotency_key=?", (key,)).fetchone()
            if existing:
                order = Order.model_validate_json(existing["data"])
                if order.quote_id != quote_id or order.agent_id != agent_id:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "Idempotency key belongs to another quote or agent")
                audit.append(conn, f"agent:{agent_id}", "ORDER_REPLAY", "Idempotent retry returned the existing order.",
                             order_id=order.id, quote_id=order.quote_id,
                             data={"inputs": {"idempotency_key": key}, "live_calls": counter.live().model_dump()})
                return order
            quote = self._quote(conn, quote_id)
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
            result = self.policy.evaluate(quote.total, lines, self._violations(conn, quote))
            order = Order(id=str(uuid4()), quote_id=quote.id, agent_id=agent_id, idempotency_key=key,
                          status=result.outcome, created_at=audit.utcnow(), expires_at=quote.expires_at,
                          total=quote.total, currency=quote.currency, lines=lines, policy=result, live_calls=counter.live())
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
        violations = list(quote.hard_constraint_violations)
        if quote.need_ref:
            lookup = LookupResult.model_validate_json(conn.execute("SELECT data FROM lookups WHERE id=?", (quote.need_ref,)).fetchone()[0])
            for line in quote.lines:
                violations.extend(hard_violations(self.catalog.get(line.product_id), lookup.need.required_claims, lookup.need.exclude_allergens))
        return sorted(set(violations))

    @staticmethod
    def _refresh_stock(conn, lines, deliver_by, stock, exclude_order=None):
        refreshed = []
        stock_codes = {"PARTIAL_STOCK", "DATE_SHIFT", "CUTOFF_PASSED", "SALES_BLOCK", "UNKNOWN_PRODUCT", "AVAILABILITY_ERROR"}
        for original in lines:
            line = original.model_copy(deep=True)
            raw = stock[line.product_id]
            line.availability, warnings = availability_for(raw, deliver_by, line.units_needed,
                                                           reserved(conn, line.product_id, exclude_order))
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
                if line.availability.logistic_model == "Stock":
                    conn.execute("INSERT INTO reservations VALUES(?,?,?)", (order.id, line.product_id, line.units_needed))

    def get_quote(self, quote_id):
        self.expire()
        with self.db.connect() as conn:
            return self._quote(conn, quote_id)

    def get_order(self, order_id):
        self.expire()
        with self.db.connect() as conn:
            return self._order(conn, order_id)

    def timeline(self, order_id):
        self.expire()
        with self.db.connect() as conn:
            # Order state, quote reference and audit history share one read snapshot.
            conn.execute("BEGIN")
            order = self._order(conn, order_id)
            quote = self._quote(conn, order.quote_id)
            events = audit.read_timeline(conn, order.id, quote.id, quote.need_ref)
        stages = {"LOOKUP": "lookup", "CREATE_QUOTE": "quote", "PLACE_ORDER": "order",
                  "ORDER_REPLAY": "order", "APPROVE": "decision", "REJECT": "decision",
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

    def list_orders(self, pending_only=False):
        self.expire()
        with self.db.connect() as conn:
            sql = "SELECT data FROM orders" + (" WHERE status='PENDING_APPROVAL'" if pending_only else "") + " ORDER BY rowid DESC"
            return [Order.model_validate_json(r[0]) for r in conn.execute(sql)]

    def approve(self, order_id, approver, comment=""):
        return self._review(order_id, approver, comment, True)

    def reject(self, order_id, approver, comment=""):
        return self._review(order_id, approver, comment, False)

    def _review(self, order_id, approver, comment, approve):
        self.expire()
        if not approver.strip():
            raise DomainError("APPROVER_REQUIRED", "An approver name is required", 422)
        action = "APPROVE" if approve else "REJECT"
        return self._run(action, f"human:{approver}", {"order_id": order_id, "comment": comment},
                         lambda counter: self._review_transaction(order_id, approver, comment, approve, counter))

    def _review_transaction(self, order_id, approver, comment, approve, counter):
        with self.db.connect(write=True) as conn:
            order = self._order(conn, order_id)
            if order.status != "PENDING_APPROVAL":
                raise DomainError("INVALID_TRANSITION", "Only PENDING_APPROVAL orders can be approved or rejected")
            quote = self._quote(conn, order.quote_id)
            if approve:
                stock = self.valio.fetch("availability", quote.customer_id, quote.deliver_by,
                                         [o.product_id for o in order.lines], counter, fresh=True)
                order.lines = self._refresh_stock(conn, order.lines, quote.deliver_by, stock, order.id)
                # Human review may accept delivery warnings, but never hard violations or unverifiable stock.
                evaluation = self.policy.evaluate(order.total, order.lines, self._violations(conn, quote))
                if evaluation.outcome == "REJECTED":
                    raise DomainError("HARD_CONSTRAINT_VIOLATION", "Approval cannot override hard constraints or sales blocks",
                                      data={"rule_hits": [h.model_dump() for h in evaluation.hits]})
                if any(w.code == "AVAILABILITY_ERROR" for o in order.lines for w in o.warnings):
                    raise DomainError("AVAILABILITY_ERROR", "Availability must be verified before approval", 502)
                if order.expires_at <= audit.utcnow():
                    raise DomainError("EXPIRED_ORDER", "The approval window expired during stock verification")
            order.status = "CONFIRMED" if approve else "REJECTED"
            order.approver, order.comment = approver, comment
            self._save_order(conn, order)
            self._reserve(conn, order)
            audit.append(conn, f"human:{approver}", "APPROVE" if approve else "REJECT",
                         f"Human {approver} {'approved' if approve else 'rejected'} the order.",
                         order_id=order.id, quote_id=quote.id,
                         data={"inputs": {"comment": comment},
                               "rule_hits": [h.model_dump() for h in (evaluation.hits if approve else order.policy.hits)],
                               "fit_scores": {o.product_id: o.fit for o in order.lines},
                               "warnings": {o.product_id: [w.code for w in o.warnings] for o in order.lines},
                               "live_calls": counter.live().model_dump()})
            return order

    def cancel(self, order_id, actor="system", reason=""):
        """Core transition for a local confirmed order, before any known cutoff."""
        inputs = {"order_id": order_id, "reason": reason}
        return self._run("CANCEL", actor, inputs,
                         lambda counter: self._cancel(order_id, actor, reason, inputs, counter))

    def _cancel(self, order_id, actor, reason, inputs, counter):
        with self.db.connect(write=True) as conn:
            order = self._order(conn, order_id)
            inputs["quote_id"] = order.quote_id
            if actor.startswith("agent:") and order.agent_id != actor.removeprefix("agent:"):
                raise DomainError("NOT_ORDER_OWNER", "An agent can cancel only orders it placed", 403)
            if order.status != "CONFIRMED":
                raise DomainError("INVALID_TRANSITION", "Only CONFIRMED orders can be cancelled")
            cutoffs = [o.availability.closing_time for o in order.lines if o.availability.closing_time]
            if any(c <= audit.utcnow() for c in cutoffs):
                raise DomainError("CUTOFF_PASSED", "Cancellation cutoff has passed")
            order.status = "CANCELLED"
            order.cancelled_by = actor.split(":", 1)[1] if actor.startswith(("human:", "agent:")) else actor
            order.cancel_reason = reason
            self._save_order(conn, order)
            self._reserve(conn, order)
            audit.append(conn, actor, "CANCEL", "Confirmed order cancelled; local reservations released.",
                         order_id=order.id, quote_id=order.quote_id,
                         data={"reason": reason, "live_calls": counter.live().model_dump()})
            return order
