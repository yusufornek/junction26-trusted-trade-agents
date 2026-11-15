from datetime import timedelta
from decimal import Decimal
import json
from uuid import uuid4, uuid5, NAMESPACE_URL
from types import SimpleNamespace
from cryptography.exceptions import InvalidSignature

from . import audit
from .mandates import evidence as mandate_evidence
from .auth import check_owner
from .catalog import hard_violations
from .db import DomainError
from .lookup import availability_for, offer_for, reserved, warning
from .models import LiveCalls, LookupResult, NeedSpec, Quantity, OfferCard, Order, OrderTimeline, Quote, QuoteLineRequest, TimelineStep
from .valio import RequestCounter
from .quantities import sales_step
from .signing import receipt_payload, signature_digest, verify_quote
from .foundation import CORRELATION, TASK, procurement_context, trace_id
from .runtime import ENVIRONMENT
from .suppliers import reservation_key
from .telemetry import placement_context, metric


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
        old_covered = (min(old.units_needed, old.availability.available_amount) if old.availability.availability_known
                       else old.units_needed * old.availability.coverage)
        new_covered = (min(new.units_needed, new.availability.available_amount) if new.availability.availability_known
                       else new.units_needed * new.availability.coverage)
        if old_covered != new_covered:
            changes.append(f"{product_id}: covered {old_covered:g} → {new_covered:g} of {new.units_needed:g}")
        if old.availability.available_date != new.availability.available_date:
            changes.append(f"{product_id}: delivery date {old.availability.available_date} → {new.availability.available_date}")
        codes({w.code for w in old.warnings}, {w.code for w in new.warnings}, f"{product_id}: ", "warning")
    codes({w.code for w in before.warnings}, {w.code for w in after.warnings}, "Order: ", "warning")
    codes({h.rule for h in before.policy.hits}, {h.rule for h in after.policy.hits}, "Order: ", "policy rule")
    return changes


class Orders:
    def __init__(self, db, catalog, valio, policy, signing, mandates):
        self.db, self.catalog, self.valio, self.policy = db, catalog, valio, policy
        self.signing = signing
        self.mandates = mandates

    def _sign_receipt(self, order, quote, kind):
        order.customer_id = quote.customer_id
        order.quote_signature_digest = signature_digest(quote.signature) if quote.signature else None
        order.decided_at = audit.utcnow()
        order.decided_by_kind = kind
        order.receipt = self.signing.sign(receipt_payload(order))
        order.links.update(receipt_id=order.id, quote_id=quote.id, request_id=quote.need_ref,
                           order_id=order.id, trace_id=trace_id(),
                           audit_checkpoint=f"/v1/orders/{order.id}/timeline", task_id=TASK.get() or order.links.get("task_id"))

    def _verified_quote(self, conn, quote_id, agent_id):
        row = conn.execute("SELECT data FROM quotes WHERE id=?", (quote_id,)).fetchone()
        if row is None:
            raise DomainError("QUOTE_NOT_FOUND", "Quote was not found", 404)
        raw = json.loads(row[0])
        check_owner(SimpleNamespace(agent_id=raw.get("agent_id")), agent_id, "QUOTE_NOT_FOUND")
        try:
            verify_quote(raw, self.signing.jwks())
            quote = Quote.model_validate(raw)
            if (quote.expires_at.utcoffset() is None or quote.currency != "EUR" or
                    any(line.currency != quote.currency for line in quote.lines)):
                raise ValueError("Inconsistent quote promise")
            if quote.need_ref:
                bound = self.lookup_need(conn, quote.need_ref)
                if bound.need.customer_id != quote.customer_id or bound.need.deliver_by != quote.deliver_by or bound.need.supplier_id != quote.supplier_id:
                    raise ValueError("Inconsistent need binding")
            # Historical spec 10 promises remain usable; _violations still
            # enforces current customer rules even when need_ref is absent.
            return quote
        except (InvalidSignature, ValueError, TypeError, KeyError, AttributeError):
            raise DomainError("QUOTE_SIGNATURE_INVALID", "Stored quote signature does not match its promise") from None

    @staticmethod
    def lookup_need(conn, need_ref):
        row = conn.execute("SELECT data FROM lookups WHERE id=?", (need_ref,)).fetchone()
        if row is None:
            raise ValueError("Missing need binding")
        return LookupResult.model_validate_json(row[0], context={"stored": True})

    def _run(self, action, actor, inputs, operation):
        counter = RequestCounter()
        try:
            return operation(counter)
        except DomainError as error:
            # A changed revision is already persisted and audited atomically, with its actual calls.
            # Do not add a second failed event that would double-count the same refresh in the timeline.
            if error.code == "EVIDENCE_CHANGED" and error.data.get("changes"):
                error.audited = True
                raise
            if error.code in {"EXPIRED_QUOTE", "EXPIRED_ORDER"}:
                self.expire()
            with self.db.connect(write=True) as conn:
                audit.append(conn, actor, action + "_FAILED", error.message,
                             order_id=inputs.get("order_id"), quote_id=inputs.get("quote_id"),
                             data={"inputs": inputs, "error": error.code, "live_calls": counter.live().model_dump(),
                                   **({"stated_name": inputs["stated_name"]} if "stated_name" in inputs else {}), **error.data})
                error.audited = True
                if any(h.get("rule") == "hard_constraint_violation" for h in error.data.get("rule_hits", [])):
                    if inputs.get("order_id"):
                        order = self._order(conn, inputs["order_id"])
                        self._suggest_review(conn, order, "hard_constraint_refusal")
            raise

    def _suggest_review(self, conn, order, reason):
        policy = self.policy.get()
        token = self.mandates.stored(conn, "order", order.id)
        if token is not None:
            try:
                # The order snapshot records placement-time effective authority. Recompute
                # from its original signed claim and today's policy/grant instead.
                policy, _, _ = self.mandates.authorize(conn, None, order.agent_id, order.customer_id,
                    products=[line.product_id for line in order.lines], bound=("order", order.id), policy=policy, supplier_id=order.supplier_id)
            except DomainError:
                # Expired/revoked authority must not prevent the human from rejecting.
                return
        row = policy.agents.get(order.agent_id)
        level = (row.autonomy or policy.autonomy.default) if row else policy.autonomy.default
        if token is None and order.mandate is not None:
            # Older records without a bound credential remain loadable and conservative.
            if order.mandate.autonomy == "approval_required":
                return
            if row is None:
                level = order.mandate.autonomy or policy.autonomy.default
                grant = conn.execute("SELECT level FROM agent_autonomy WHERE agent_id=?", (order.agent_id,)).fetchone()
                if grant and grant[0] == "approval_required":
                    return
        if level == "autonomous_within_limits":
            from .foundation import emit
            cause = conn.execute("SELECT id,ts FROM audit ORDER BY id DESC LIMIT 1").fetchone()
            # The refusal/rejection audit entry is the event's checkpoint. Keep one audit
            # decision in the timeline while publishing a second event from the same cause.
            emit(conn, "AUTONOMY_REVIEW_SUGGESTED", "system", cause["ts"], cause["id"],
                 {"agent_id": order.agent_id, "customer_id": order.customer_id, "reason": reason},
                 order.id, order.quote_id)

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
                try:
                    quote = Quote.model_validate_json(row["data"])
                except ValueError:
                    # Malformed signed fields must reach placement's explicit
                    # signature refusal rather than crashing its expiry sweep.
                    continue
                if quote.expires_at.utcoffset() is None:
                    continue  # Historical records still load; placement audits signature refusal.
                if quote.expires_at <= now:
                    metric("expiries")
                    quote.status = "EXPIRED"
                    self._save_quote(conn, quote)
                    audit.append(conn, "system", "QUOTE_EXPIRED", "The fixed-price quote expired.", quote_id=quote.id)
            for row in conn.execute("SELECT data FROM orders WHERE status='PENDING_APPROVAL'").fetchall():
                order = Order.model_validate_json(row["data"])
                if order.expires_at <= now:
                    metric("expiries")
                    order.status = "EXPIRED"
                    order.order_revision += 1
                    self._sign_receipt(order, self._quote(conn, order.quote_id), "system")
                    self._save_order(conn, order)
                    conn.execute("DELETE FROM reservations WHERE order_id=?", (order.id,))
                    audit.append(conn, "system", "ORDER_EXPIRED", "Approval deadline passed; local reservations released.",
                                 order_id=order.id, quote_id=order.quote_id)

    def create_quote(self, request, agent_id="external", *, operator=False, mandate=None):
        self.expire()
        actor = f"agent:{agent_id}"
        return self._run("CREATE_QUOTE", actor, request.model_dump(mode="json"),
                         lambda counter: self._create_quote(request, actor, counter, operator, mandate))

    def _create_quote(self, request, actor, counter, operator=False, mandate=None):
        agent_id = actor.removeprefix("agent:")
        with self.db.connect() as conn:
            if mandate is not None:
                row = conn.execute("SELECT data FROM lookups WHERE id=?", (request.need_ref,)).fetchone()
                supplier = json.loads(row[0]).get("need", {}).get("supplier_id", "valio") if row else "valio"
                self.mandates.authorize(conn, mandate, agent_id, request.customer_id, supplier_id=supplier)
            if mandate is None and self.mandates.stored(conn, "lookup", request.need_ref) is None and agent_id not in self.policy.get().agents:
                self.policy.check_customer(agent_id, request.customer_id)
            if not request.need_ref:
                self.mandates.authorize(conn, mandate, agent_id, request.customer_id)
                raise DomainError("NEED_REF_REQUIRED", "Explicit lines require need_ref to a stored request", 422)
        lookup = None
        if request.need_ref:
            with self.db.connect() as conn:
                row = conn.execute("SELECT data FROM lookups WHERE id=?", (request.need_ref,)).fetchone()
            if not row:
                raise DomainError("NEED_NOT_FOUND", "need_ref must be a lookup_id returned by this gateway", 404)
            lookup = LookupResult.model_validate_json(row["data"], context={"stored": True})
            check_owner(lookup, None if operator else agent_id, "LOOKUP_NOT_FOUND")
            with self.db.connect() as conn:
                policy, record, mandate = self.mandates.authorize(conn, mandate, agent_id, request.customer_id,
                    bound=("lookup", request.need_ref), supplier_id=lookup.need.supplier_id)
            supplier_id = lookup.need.supplier_id
            adapter = self.suppliers.get(supplier_id)
            catalog = adapter.catalog
            self.policy.check_supplier(agent_id, supplier_id)
            if lookup.need.customer_id != request.customer_id or lookup.need.deliver_by != request.deliver_by:
                raise DomainError("NEED_MISMATCH", "Quote customer and delivery date must match need_ref", 422)
        plan = None
        requested_lines = request.lines
        from .baskets import stored_basket, bound_need
        basket = None
        with self.db.connect() as conn:
            basket = stored_basket(conn, lookup.lookup_id)
        if request.basket_ref:
            if basket is None:
                if not lookup.basket or not lookup.plans:
                    raise DomainError("BASKET_NOT_FOUND", "Basket lookup was not found", 404)
                plan = lookup.plans[0]
                requested_lines = [QuoteLineRequest(product_id=line.product_id, quantity_sales_units=line.units)
                                   for line in plan.lines]
            else:
                requested_lines = [QuoteLineRequest(product_id=line.product_id, quantity_sales_units=line.units_needed)
                                   for line in basket.basket.lines]
            if not requested_lines:
                raise DomainError("EMPTY_BASKET", "No purchasable basket lines; revise uncovered ingredients", 422)
        if request.plan_ref:
            if request.plan_ref.plan_index >= len(lookup.plans):
                raise DomainError("INVALID_PLAN_INDEX", "plan_index is outside the stored lookup's plans", 422)
            plan = lookup.plans[request.plan_ref.plan_index]
            requested_lines = [QuoteLineRequest(product_id=line.product_id, quantity_sales_units=line.units)
                               for line in plan.lines]
        products = {line.product_id: adapter.catalog.get(line.product_id) for line in requested_lines}
        with self.db.connect() as conn:
            policy, _, _ = self.mandates.authorize(conn, mandate, agent_id, request.customer_id, products=products, supplier_id=supplier_id)
            if governance := getattr(self.policy, "governance", None):
                governance.check_operation(conn, agent_id, "quote", request.customer_id, request.deliver_by,
                    products=products.values(), effective=policy._governance)
        for line in requested_lines:
            p = products[line.product_id]
            step = sales_step(p)
            if line.quantity_sales_units < p.minimum_order_quantity or Decimal(str(line.quantity_sales_units)) % step:
                raise DomainError("INVALID_LOT_SIZE", f"{p.id}: sales quantity must be at least {p.minimum_order_quantity:g} and a multiple of {step}", 422)
        from .brief_scenarios import quote_baselines
        ids = list(dict.fromkeys([*products, *quote_baselines(self.db, lookup, products)]))
        if lookup.need.substitute_for and lookup.need.substitute_for.product_id not in ids:
            ids.append(lookup.need.substitute_for.product_id)
        price = adapter.fetch("price", request.customer_id, request.deliver_by, ids, counter, fresh=True)
        stock = adapter.fetch("availability", request.customer_id, request.deliver_by, ids, counter, fresh=True)

        now = audit.utcnow()
        need = lookup.need
        with self.db.connect() as conn:
            evidence = conn.execute("SELECT data FROM lookup_offers WHERE lookup_id=?", (lookup.lookup_id,)).fetchone()
        all_offers = [OfferCard.model_validate(o) for o in json.loads(evidence[0])] if evidence else lookup.offers
        fit_by_id = {o.product_id: {"probability": o.fit, "question": o.fit_question or ""}
                     for o in all_offers if o.fit is not None}
        with self.db.connect(write=True) as conn:
            # Recheck after taking the same lock used by policy updates.
            policy, record, mandate = self.mandates.authorize(conn, mandate, agent_id, request.customer_id, products=products, supplier_id=supplier_id)
            governance = getattr(self.policy, "governance", None)
            if governance:
                governance.check_operation(conn, agent_id, "quote", request.customer_id, request.deliver_by, products=products.values(), effective=policy._governance)
            need, effective = self.policy.effective_need(lookup.need, policy)
            lines, violations, expiries = [], [], [now + timedelta(minutes=30)]
            for line in requested_lines:
                p = products[line.product_id]
                need, effective = self.policy.effective_need(bound_need(conn, lookup, p.id), policy)
                offer = offer_for(p, need, price[p.id], stock[p.id], fit_by_id.get(p.id),
                                  sales_quantity=line.quantity_sales_units, reserved_amount=reserved(conn, p.id, supplier_id=supplier_id), sources=effective)
                from .substitution import annotate, functional_categories
                annotate(offer, need, catalog, policy, price)
                from .brief_scenarios import quote_annotations
                quote_annotations(offer, need, catalog, price, stock)
                categories = functional_categories(catalog, need)
                if categories is not None and p.category not in categories:
                    violations.append(f"{p.id}: functional product type does not match (customer_policy)")

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
            quote = Quote(supplier_id=supplier_id, environment=ENVIRONMENT.get(), synthetic=adapter.synthetic, mandate=record, id=str(uuid4()), agent_id=actor.removeprefix("agent:"), customer_id=request.customer_id, deliver_by=request.deliver_by,
                          need_ref=request.need_ref, plan_ref=request.plan_ref,
                          basket_ref=request.basket_ref or ({"basket_lookup_id": lookup.lookup_id} if basket else None),
                          clinical_review=policy.customers[request.customer_id].clinical_review if request.customer_id in policy.customers else None,
                          warnings=([w for w in plan.warnings if w.code == "PARTIAL_STOCK"] if plan else []) +
                                   ([warning("PARTIAL_BASKET")] if basket and basket.basket.uncovered else []) +
                                   ([warning("CLINICAL_REVIEW")] if policy.customers.get(request.customer_id) and policy.customers[request.customer_id].clinical_review == "required" else []),
                          status="OPEN", created_at=now, expires_at=min(expiries),
                          lines=lines, total=round(sum(o.total for o in lines), 2), currency="EUR",
                          hard_constraint_violations=violations, live_calls=counter.live())
            if quote.expires_at <= audit.utcnow():
                raise DomainError("EXPIRED_QUOTE", "Price validity ended during quote creation; re-quote")
            if governance:
                governance.snapshot(conn, policy._governance)
                quote.policy_version_id = policy._governance.version_id
            quote.context = procurement_context(self.policy, quote.customer_id, quote.need_ref, effective, record, supplier_id=supplier_id)
            if (lookup.context or {}).get("proposal_ref"):
                quote.context["proposal_ref"] = lookup.context["proposal_ref"]
            if quote.policy_version_id:
                quote.context["governance_policy_version_id"] = quote.policy_version_id
            # Preserve the exact catalog evidence approved with this signed quote.
            quote.context["product_evidence"] = {p.id: {
                "brand": p.brand, "claims": sorted(p.claims), "allergens": p.allergen_status,
                "allergen_declaration": p.has_allergen_declaration} for p in products.values()}
            from .recurring import physical_pack
            for p in products.values():
                quote.context["product_evidence"][p.id]["physical_pack"] = physical_pack(p, price[p.id])

            if basket or lookup.basket:
                quote.context["atomicity"] = "all-or-none gateway simulation; no supplier transmission"
            quote.correlation_id = CORRELATION.get()
            quote.links = {"request_id": quote.need_ref, "quote_id": quote.id,
                           "trace_id": trace_id(), "event_id": str(uuid4()), "task_id": TASK.get()}
            quote.anomalies = self.anomalies.checks(conn, quote)
            quote.intent_id, quote.intent_version = request.intent_id, request.intent_version
            quote.warnings.extend(self.anomalies.warnings(quote.anomalies))
            quote.signature = self.signing.sign_quote(quote)
            self.mandates.bind(conn, "quote", quote.id, mandate)
            conn.execute("INSERT INTO quotes VALUES(?,?,?,?)", (quote.id, quote.status, quote.expires_at.isoformat(), quote.model_dump_json()))
            if any(c.status == "anomaly" for c in quote.anomalies):
                audit.append(conn, actor, "ANOMALY_DETECTED", "Quote requires anomaly review.", quote_id=quote.id,
                             data={"anomalies": [c.model_dump(mode="json") for c in quote.anomalies]})
            audit.append(conn, actor, "CREATE_QUOTE", f"Locked {len(lines)} fixed-price lines for {quote.total:.2f} EUR.",
                         quote_id=quote.id, data={"inputs": request.model_dump(mode="json"),
                         "live_calls": counter.live().model_dump(), **mandate_evidence(record), "fit_scores": {o.product_id: o.fit for o in lines},
                         "hard_constraint_violations": violations, "warnings": {o.product_id: [w.code for w in o.warnings] for o in lines}})
        return quote

    def place_order(self, quote_id, idempotency_key, agent_id="external", *, operator=False, mandate=None):
        if not idempotency_key or not idempotency_key.strip():
            raise DomainError("IDEMPOTENCY_REQUIRED", "A non-empty idempotency key is required", 422)
        self.expire()
        inputs = {"quote_id": quote_id, "idempotency_key": idempotency_key}
        return self._run("PLACE_ORDER", f"agent:{agent_id}", inputs,
                         lambda counter: self._place_order(quote_id, idempotency_key, agent_id, counter, operator, mandate))

    def _place_order(self, quote_id, key, agent_id, counter, operator=False, mandate=None):
        from .foundation import PRINCIPAL
        from hashlib import sha256
        storage_key = sha256((agent_id + "\x00" + key).encode()).hexdigest()
        # Serializes idempotency, reservation reads and writes across REST/MCP processes.
        with self.db.connect(write=True) as conn:
            self.gateway.connection_trust.require_order(PRINCIPAL.get(), conn)
            policy = self.policy.get()
            existing = conn.execute("SELECT data FROM orders WHERE idempotency_key=? OR "
                                    "(idempotency_key=? AND json_extract(data,'$.agent_id')=?)",
                                    (storage_key, key, agent_id)).fetchone()
            if existing:
                order = Order.model_validate_json(existing["data"])
                quote = check_owner(self._quote(conn, quote_id), None if operator else agent_id, "QUOTE_NOT_FOUND")
                if order.quote_id != quote_id or order.agent_id != agent_id:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "Idempotency key belongs to another quote or agent")
                # Replay commits no new purchase. Historical signatures/retired keys
                # must not prevent it, but current buyer authority still applies.
                bound = ("order", order.id) if self.mandates.stored(conn, "order", order.id) else ("quote", quote_id)
                if mandate is not None or self.mandates.stored(conn, *bound) is not None:
                    _, _, used_mandate = self.mandates.authorize(conn, mandate, agent_id, order.customer_id or quote.customer_id,
                        products=[line.product_id for line in order.lines], bound=bound, policy=policy, supplier_id=order.supplier_id)
                    self.mandates.used(conn, used_mandate)
                elif agent_id in policy.agents:
                    # Historical orders may predate the shared command table. Apply
                    # current customer scope even on that original replay path.
                    self.policy.check_customer(agent_id, order.customer_id or quote.customer_id, policy)
                self.policy.check_supplier(agent_id, order.supplier_id, policy)
                if order.status == "PENDING_EXECUTION":
                    self._execute(conn, order, quote)

                audit.append(conn, f"agent:{agent_id}", "ORDER_REPLAY", "Idempotent retry returned the existing order.",
                             order_id=order.id, quote_id=order.quote_id,
                             data={"inputs": {"idempotency_key": key}, "live_calls": counter.live().model_dump(), **mandate_evidence(order.mandate)})
                return order
            stored_mandate = self.mandates.stored(conn, "quote", quote_id)
            if not existing and mandate is None and stored_mandate is None and agent_id not in policy.agents:
                raise DomainError("AGENT_NOT_REGISTERED", f"Agent {agent_id} has no mandate; ordering requires a mandate", 403)
            quote = self._verified_quote(conn, quote_id, None if operator else agent_id)
            if mandate is not None or stored_mandate is not None:
                policy, record, mandate = self.mandates.authorize(conn, mandate, agent_id, quote.customer_id,
                    products=[o.product_id for o in quote.lines], bound=("quote", quote_id), policy=policy, supplier_id=quote.supplier_id)
            else:
                record = None
            governance = getattr(self.policy, "governance", None)
            if governance:
                if policy._governance is None:
                    policy = governance.project(agent_id, quote.customer_id, policy, conn=conn)
                governance.check_operation(conn, agent_id, "order", quote.customer_id, quote.deliver_by,
                    products=[self.suppliers.get(quote.supplier_id).catalog.get(o.product_id) for o in quote.lines], effective=policy._governance)
            self.policy.check_customer(agent_id, quote.customer_id, policy)
            self.policy.check_supplier(agent_id, quote.supplier_id, policy)
            from .guardrail_profiles import require_constraints
            require_constraints(conn, quote.customer_id)
            if quote.status == "EXPIRED" or quote.expires_at <= audit.utcnow():
                raise DomainError("EXPIRED_QUOTE", "Quote expired: " + (quote.expiration_reason or "validity ended") + "; create a new quote before ordering", data={"reason": quote.expiration_reason or "validity_ended"})
            if quote.status != "OPEN":
                raise DomainError("QUOTE_ALREADY_ACCEPTED", "This quote already has an order")
            stock = self.suppliers.get(quote.supplier_id).fetch("availability", quote.customer_id, quote.deliver_by,
                                     [o.product_id for o in quote.lines], counter, fresh=True)
            if quote.expires_at <= audit.utcnow():
                raise DomainError("EXPIRED_QUOTE", "Quote expired while checking stock; create a new quote")
            lines = self._refresh_stock(conn, quote.lines, quote.deliver_by, stock)
            # Re-read catalog hard facts as well as preserving original quote violations.
            now = audit.utcnow()
            if mandate is not None:
                policy, record, _ = self.mandates.authorize(conn, mandate, agent_id, quote.customer_id,
                    products=[o.product_id for o in lines], policy=policy, supplier_id=quote.supplier_id)
            from .anomaly_service import CODES
            checks = self.anomalies.checks(conn, quote.model_copy(update={"lines": lines, "agent_id": agent_id}), placement=True)
            if (quote.context or {}).get("anomaly_split_remainder"):
                for held in quote.anomalies:
                    if held.code != "QUANTITY_ANOMALY" or held.status != "anomaly":
                        continue
                    if not any(c.code == held.code and c.product_id == held.product_id and c.status == "anomaly" for c in checks):
                        checks = [c for c in checks if not (c.code == held.code and c.product_id == held.product_id)]
                        checks.append(held)
            warnings = [w for w in quote.warnings if w.code not in CODES] + self.anomalies.warnings(checks)
            result = self.policy.evaluate_order(conn, quote.total, lines, self._violations(conn, quote),
                                                agent_id=agent_id, customer_id=quote.customer_id,
                                                policy=policy, warnings=warnings)
            if quote.basket_ref and any(not line.availability.enough for line in lines):
                from .models import RuleHit
                result.hits.append(RuleHit(rule="basket_stock_unavailable", detail="All basket lines must be available before confirmation."))
                if result.outcome == "CONFIRMED":
                    result.outcome = "PENDING_APPROVAL"
            # Only the signed, stored quote can carry recurring comparison evidence.
            comparison = self._recurring_comparison(quote, lines)
            drift = comparison["diff"] if comparison else (quote.context or {}).get("recurring_drift", [])
            if drift:
                from .models import RuleHit
                result.hits.extend(RuleHit(rule="recurring_drift", detail=d) for d in drift)
                if result.outcome == "CONFIRMED":
                    result.outcome = "PENDING_APPROVAL"
            order = Order(supplier_id=quote.supplier_id, environment=quote.environment, synthetic=quote.synthetic,
                          mandate=record, id=str(uuid5(NAMESPACE_URL, agent_id + "\x00" + key + "\x00" + quote.id)) if quote.supplier_id != "valio" else str(uuid4()), quote_id=quote.id, agent_id=agent_id, idempotency_key=key,
                          basket_ref=quote.basket_ref,
                          atomicity=getattr(self.valio, "atomicity", "all-or-none local simulation; no supplier order transmitted") if quote.basket_ref else None,
                          clinical_review=quote.clinical_review or (policy.customers[quote.customer_id].clinical_review if quote.customer_id in policy.customers else None),

                          status=result.outcome, created_at=now, expires_at=quote.expires_at,
                          total=quote.total, currency=quote.currency, lines=lines, warnings=warnings, anomalies=checks,
                          policy=result, live_calls=counter.live())
            order.context = dict(quote.context) if quote.context else None
            if governance:
                governance.snapshot(conn, policy._governance)
                order.policy_version_id = policy._governance.version_id
            order.intent_id, order.intent_version = quote.intent_id, quote.intent_version
            if order.context and quote.need_ref:
                _, effective = self.policy.effective_need(self.lookup_need(conn, quote.need_ref).need)
                order.context = procurement_context(self.policy, quote.customer_id, quote.need_ref, effective, record, supplier_id=quote.supplier_id)
                if quote.basket_ref:
                    order.context["atomicity"] = "all-or-none gateway simulation; no supplier transmission"
            if comparison:
                order.context["recurring_comparison"] = comparison
            order.correlation_id = CORRELATION.get() or quote.correlation_id
            if order.context and order.policy_version_id:
                order.context["governance_policy_version_id"] = order.policy_version_id
            if saved_context := placement_context():
                order.links["placement_span"] = saved_context
            from .models import Execution
            self.gateway.connection_trust.attach_order(order)
            executor = self.suppliers.get(order.supplier_id).execution
            order.execution = (executor.submit(order) if executor.mode == "simulated"
                               else Execution(mode="supplier_backed", status="not_submitted"))
            self._sign_receipt(order, quote, "operator" if operator else "agent")
            self.mandates.bind(conn, "order", order.id, mandate)
            conn.execute("INSERT INTO orders VALUES(?,?,?,?,?)", (order.id, quote.id, storage_key, order.status, order.model_dump_json()))
            quote.status = "ACCEPTED"
            self._save_quote(conn, quote)
            self._reserve(conn, order)
            if order.status == "CONFIRMED" and executor.mode == "supplier_backed":
                self._execute(conn, order, quote)
            audit.append(conn, f"agent:{agent_id}", "PLACE_ORDER", f"Order {order.status} at the locked total of {order.total:.2f} EUR.",
                         order_id=order.id, quote_id=quote.id, data={"inputs": {"idempotency_key": key},
                         "outcome": order.status, **mandate_evidence(record),
                         "rule_hits": [h.model_dump() for h in result.hits], "fit_scores": {o.product_id: o.fit for o in lines},
                          "anomalies": [c.model_dump(mode="json") for c in checks],
                         "live_calls": counter.live().model_dump()})
            if order.status == "REJECTED":
                self._suggest_review(conn, order, "hard_constraint_refusal" if any(
                    h.rule == "hard_constraint_violation" for h in result.hits) else "policy_rejection")
            return order

    @staticmethod
    def _recurring_comparison(quote, lines):
        context = (quote.context or {}).get("recurring_comparison")
        if context is None:
            return None
        from .recurring import snapshot, compare, Deviations
        current = snapshot(None, quote.model_copy(update={"lines": lines}))
        return {**context, "current": current,
                "diff": compare(current, context["previous"], context["baseline"],
                                Deviations.model_validate(context["deviations"]))}

    def _execute(self, conn, order, quote):
        """Commit an immutable purchase intent before any outbound side effect."""
        from .models import Execution
        from .foundation import PRINCIPAL
        principal = PRINCIPAL.get()
        credential = principal.public() if principal and principal.id == order.agent_id else None
        if credential and self.gateway.settings.keys:
            credential["fingerprint"] = self.gateway.settings.keys.fingerprint(principal)
        payload = {"order": order.model_dump(mode="json"), "credential": credential}
        conn.execute("INSERT OR IGNORE INTO supplier_execution_outbox(order_id,payload) VALUES(?,?)",
                     (order.id, json.dumps(payload)))
        conn.execute("UPDATE supplier_execution_outbox SET status='pending' WHERE order_id=? AND status='blocked'", (order.id,))
        order.status = "PENDING_EXECUTION"
        order.execution = Execution(mode="supplier_backed", status="unknown")
        self._sign_receipt(order, quote, order.decided_by_kind or "agent")
        self._save_order(conn, order)
        self._reserve(conn, order)

    def _dispatch_authority(self, conn, order, quote, credential):
        from .connection_trust_transports import dispatch_authority
        dispatch_authority(self.gateway, conn, order, credential)
        from .buyer import check_suspension
        check_suspension(self.db, order.agent_id)
        if credential and credential.get("credential_ref"):
            ref = credential["credential_ref"]
            if ref.startswith("agent:") or ref.startswith("stdio:"):
                from .auth import Principal
                current = self.gateway.settings.keys.fingerprint(Principal(order.agent_id))
                if current is None or credential.get("fingerprint") not in {None, current}:
                    raise DomainError("UNAUTHENTICATED", "Purchasing credential was removed or rotated", 401)
            elif ref.startswith("operator:"):
                from .auth import Principal
                keys = self.gateway.settings.keys
                current = keys.fingerprint(Principal(order.agent_id, operator=True))
                if current is None or keys.operator_agent_id != order.agent_id or credential.get("fingerprint") not in {None, current}:
                    raise DomainError("UNAUTHENTICATED", "Purchasing credential was removed or rotated", 401)
            elif ref not in {"internal", "operator"}:
                row = conn.execute("SELECT revoked_at FROM connections WHERE connection_id=? AND agent_id=?", (ref.removeprefix("connection:"), order.agent_id)).fetchone()
                if not row or row[0]:
                    raise DomainError("UNAUTHENTICATED", "Purchasing credential was revoked", 401)
        if credential is None:
            keys = self.gateway.settings.keys
            if not (keys and keys.has_agent(order.agent_id)) and not conn.execute(
                    "SELECT 1 FROM connections WHERE agent_id=? AND revoked_at IS NULL", (order.agent_id,)).fetchone():
                raise DomainError("UNAUTHENTICATED", "A current purchasing credential is required for supplier dispatch", 401)
        policy, record, _ = self.mandates.authorize(conn, None, order.agent_id, order.customer_id,
            products=[line.product_id for line in order.lines], bound=("order", order.id), supplier_id=order.supplier_id)
        evaluation = (self.gateway.intents.evaluate(conn, quote, order.agent_id, policy, order) if order.intent_id else
            self.policy.evaluate_order(conn, order.total, order.lines, self._violations(conn, quote),
                agent_id=order.agent_id, customer_id=order.customer_id, warnings=order.warnings, policy=policy))
        reviewed = {(hit.rule, hit.detail) for hit in order.policy.hits}
        new_hits = {(hit.rule, hit.detail) for hit in evaluation.hits} - reviewed
        if evaluation.outcome == "REJECTED" or (not order.approver and evaluation.outcome != "CONFIRMED") or new_hits:
            raise DomainError("INVALID_TRANSITION", "Current purchasing policy requires a new human review before supplier dispatch", 409,
                              {"rule_hits": [hit.model_dump() for hit in evaluation.hits]})

    def dispatch_pending(self, order_id=None):
        """Recover committed intents, including accepted purchases with lost local acknowledgements.

        A read-only supplier reconciliation never creates a purchase. Only a currently
        authorised intent can POST, always using its immutable UUID and payload.
        """
        if self.db._connection.get() is not None:
            return  # The outer command/task transaction has not committed its intent yet.
        with self.db.connect() as conn:
            rows = conn.execute("SELECT order_id FROM supplier_execution_outbox WHERE status='pending'" +
                (" AND order_id=?" if order_id else ""), (order_id,) if order_id else ()).fetchall()
        for row in rows:
            error = None
            with self.db.connect(write=True) as conn:
                intent = conn.execute("SELECT * FROM supplier_execution_outbox WHERE order_id=? AND status='pending'", (row[0],)).fetchone()
                if intent is None:
                    continue
                payload = json.loads(intent["payload"])
                order = self._order(conn, row[0])
                if order.status != "PENDING_EXECUTION":
                    continue
                immutable = Order.model_validate(payload["order"])
                quote = self._quote(conn, order.quote_id)
                execution = self.suppliers.get(order.supplier_id).execution
                try:
                    result = execution.reconcile(immutable)
                    reconciled = result is not None
                    if result is None:
                        self._dispatch_authority(conn, immutable, quote, payload.get("credential"))
                        result = execution.submit(immutable)
                except DomainError as caught:
                    if caught.code == "SUPPLIER_UNAVAILABLE":
                        continue  # The durable intent remains retryable, with no fresh stock evaluation.
                    audit.append(conn, "agent:" + order.agent_id, "SUPPLIER_DISPATCH_FAILED", caught.message,
                        order_id=order.id, quote_id=order.quote_id, data={"error": caught.code, **caught.data})
                    if caught.code != "UPSTREAM_BUSY":
                        conn.execute("UPDATE supplier_execution_outbox SET status='blocked' WHERE order_id=?", (order.id,))
                    caught.audited = True
                    error = caught
                else:
                    order.execution = result
                    order.status = "CONFIRMED" if result.status == "accepted" else "REJECTED"
                    if result.status == "accepted":
                        order.commitment = "supplier-acknowledged"
                    order.order_revision += 1
                    self._sign_receipt(order, quote, order.decided_by_kind or "agent")
                    self._save_order(conn, order)
                    self._reserve(conn, order)
                    conn.execute("UPDATE supplier_execution_outbox SET status='done' WHERE order_id=?", (order.id,))
                    audit.append(conn, "supplier:" + order.supplier_id,
                        "SUPPLIER_ACKNOWLEDGED" if order.status == "CONFIRMED" else "SUPPLIER_REJECTED",
                        "Reconciled the supplier acknowledgement.", order_id=order.id, quote_id=order.quote_id,
                        data={"execution": result.model_dump(mode="json"), "outcome": order.status})
                    if reconciled:
                        # Recovery supersedes an obsolete placement response, even when
                        # approval began execution or a background worker found the ack.
                        current = self._order(conn, order.id).model_dump(mode="json")
                        for command in conn.execute("SELECT principal,key,result FROM commands WHERE command='place_order' AND json_extract(result,'$.value.id')=?", (order.id,)).fetchall():
                            saved = json.loads(command["result"])
                            if saved.get("value", {}).get("status") in {"PENDING_EXECUTION", "PENDING_APPROVAL"}:
                                saved["value"] = current
                                conn.execute("UPDATE commands SET result=? WHERE principal=? AND key=?",
                                             (json.dumps(saved), command["principal"], command["key"]))
            if error and order_id:
                raise error

    def _violations(self, conn, quote):
        violations = [] if quote.need_ref else list(quote.hard_constraint_violations)
        if proposal_id := (quote.context or {}).get("proposal_ref"):
            from .proposals import Proposals, ProposalPreferences
            row = conn.execute("SELECT data FROM proposals WHERE id=? AND customer_id=?", (proposal_id, quote.customer_id)).fetchone()
            proposal = json.loads(row[0]) if row else {}
            pref_row = conn.execute("SELECT data FROM proposal_preferences WHERE customer_id=?", (quote.customer_id,)).fetchone()
            prefs = ProposalPreferences.model_validate_json(pref_row[0]) if pref_row else ProposalPreferences()
            campaign_row = conn.execute("SELECT data FROM proposal_campaigns WHERE id=?", (proposal.get("campaign_id"),)).fetchone()
            campaign = json.loads(campaign_row[0]) if campaign_row else {}
            from .lookup import timestamp
            valid_until = timestamp(proposal.get("valid_until"))
            catalog = self.suppliers.get(quote.supplier_id).catalog
            if (not valid_until or valid_until <= audit.utcnow() or not prefs.channels
                    or campaign.get("campaign_id") in prefs.muted_campaigns or campaign.get("fingerprint") in prefs.muted_campaigns
                    or any(not Proposals.relevant(catalog.get(line.product_id), prefs) for line in quote.lines)):
                violations.append("Supplier proposal consent expired or was revoked (customer_policy)")
        if quote.need_ref:
            lookup = LookupResult.model_validate_json(conn.execute("SELECT data FROM lookups WHERE id=?", (quote.need_ref,)).fetchone()[0], context={"stored": True})
            need = lookup.need
        else:
            # Legacy quotes have no stored request, but still belong to a customer. Retain their
            # recorded request violations and apply that customer's current standing constraints.
            need = NeedSpec.model_construct(customer_id=quote.customer_id, deliver_by=quote.deliver_by,
                                            intent="Legacy quote", quantity=Quantity(value=1, unit="SALES_UNIT"))
        need, effective = self.policy.effective_need(need)
        for line in quote.lines:
            if quote.need_ref:
                from .baskets import bound_need
                need, effective = self.policy.effective_need(bound_need(conn, lookup, line.product_id))
            product = self.suppliers.get(quote.supplier_id).catalog.get(line.product_id)
            violations.extend(hard_violations(product, need.required_claims, need.exclude_allergens, effective))
            from .substitution import functional_categories
            categories = functional_categories(self.suppliers.get(quote.supplier_id).catalog, need)
            if categories is not None and product.category not in categories:
                violations.append(f"{product.id}: functional product type does not match (request)")
            if need.function and (line.fit is None or line.fit < self.policy.get().approval.min_fit_probability):
                violations.append(f"{product.id}: functional fit is not proven (request)")

        return sorted(set(violations))

    @staticmethod
    def _refresh_stock(conn, lines, deliver_by, stock, exclude_order=None):
        refreshed = []
        stock_codes = {"PARTIAL_STOCK", "DATE_SHIFT", "CUTOFF_PASSED", "CUTOFF_UNKNOWN", "SALES_BLOCK", "UNKNOWN_PRODUCT",
                       "NOT_AVAILABLE", "AVAILABILITY_UNCONFIRMED", "AVAILABILITY_ERROR", "UNIT_MISMATCH", "STOCK_MODEL_UNKNOWN", "NO_STOCK"}
        for original in lines:
            line = original.model_copy(deep=True)
            raw = stock[line.product_id]
            line.availability, warnings = availability_for(raw, deliver_by, line.units_needed,
                                                           reserved(conn, line.product_id, exclude_order, line.supplier_id), line.sales_unit)
            line.warnings = [w for w in line.warnings if w.code not in stock_codes] + warnings
            from .brief_scenarios import refresh_timing
            refresh_timing(line, raw, deliver_by)
            from .lookup import timestamp
            line.provenance["availability"].fetched_at = timestamp(raw.get("_fetched_at")) or audit.utcnow()
            line.provenance["availability"].valid_to = line.availability.closing_time
            refreshed.append(line)
        return refreshed

    @staticmethod
    def _reserve(conn, order):
        conn.execute("DELETE FROM reservations WHERE order_id=?", (order.id,))
        if order.basket_ref and any(not line.availability.enough for line in order.lines):
            return  # An incomplete basket never holds a subset of its lines.
        if order.execution and order.execution.mode == "supplier_backed" and order.execution.status == "accepted":
            return  # The acknowledging supplier already removed these units from its inventory.
        if order.status in {"CONFIRMED", "PENDING_APPROVAL", "PENDING_EXECUTION"}:

            for line in order.lines:
                if line.availability.availability_known:
                    conn.execute("INSERT INTO reservations VALUES(?,?,?)", (order.id, reservation_key(order.supplier_id, line.product_id), line.units_needed))

    def revalidate_quote_replay(self, quote_id, agent_id, *, operator=False, mandate=None):
        def validate(counter):
            with self.db.connect() as conn:
                quote = check_owner(self._quote(conn, quote_id), None if operator else agent_id, "QUOTE_NOT_FOUND")
                _, _, used = self.mandates.authorize(conn, mandate, agent_id, quote.customer_id,
                    products=[line.product_id for line in quote.lines], bound=("quote", quote_id),
                    supplier_id=quote.supplier_id)
                if used is not None:
                    self.mandates.used(conn, used)
        self._run("CREATE_QUOTE", f"agent:{agent_id}", {"quote_id": quote_id}, validate)

    def get_quote(self, quote_id, agent_id=None):
        return self._get_quote(quote_id, agent_id)

    def _get_quote(self, quote_id, agent_id=None):
        self.expire()
        with self.db.connect() as conn:
            quote = check_owner(self._quote(conn, quote_id), agent_id, "QUOTE_NOT_FOUND")
            from .foundation import forward_links
            quote.links.update(forward_links(conn, quote_id=quote.id, agent_id=agent_id))
            return quote

    def get_order(self, order_id, agent_id=None):
        self.expire()
        with self.db.connect() as conn:
            order = check_owner(self._order(conn, order_id), agent_id, "ORDER_NOT_FOUND")
            events = conn.execute("SELECT id,audit_id,json_extract(payload,'$.links.trace_id') AS trace_id FROM events WHERE aggregate_type='order' AND aggregate_id=? ORDER BY aggregate_revision", (order.id,)).fetchall()
            order.links.update(event_ids=[r["id"] for r in events],
                               audit_checkpoints=[r["audit_id"] for r in events],
                               trace_ids=list(dict.fromkeys(r["trace_id"] for r in events)))
            return order

    def get_order_receipt(self, order_id, agent_id=None):
        # Use the same owned order resource as spec 10's offline receipt verifier.
        order = Orders.get_order(self, order_id, agent_id)
        if order.status == "PENDING_APPROVAL":
            raise DomainError("NOT_DECIDED", "The order is waiting for human approval", 409,
                              data={"order_id": order.id, "order_revision": order.order_revision,
                                    "poll_after_seconds": order.poll_after_seconds})
        return order

    def get_order_ubl(self, order_id, document="order", agent_id=None):
        from .ubl import export
        self.expire()
        with self.db.connect() as conn:
            if not conn.in_transaction:
                conn.execute("BEGIN")
            order = check_owner(self._order(conn, order_id), agent_id, "ORDER_NOT_FOUND")
            quote = self._quote(conn, order.quote_id)
            return export(order, quote, document)

    def timeline(self, order_id, agent_id=None):
        self.expire()
        with self.db.connect() as conn:
            # Order state, quote reference and audit history share one read snapshot.
            if not conn.in_transaction:
                conn.execute("BEGIN")
            order = self._order(conn, order_id)
            check_owner(order, agent_id, "ORDER_NOT_FOUND")
            quote = self._quote(conn, order.quote_id)
            events = audit.read_timeline(conn, order.id, quote.id, quote.need_ref)
            from .visibility import audit_visible
            events = [event for event in events if audit_visible(event.model_dump(), order.agent_id)]
        stages = {"LOOKUP": "lookup", "CREATE_QUOTE": "quote", "PLACE_ORDER": "order",
                  "ORDER_REPLAY": "order", "APPROVE": "decision", "REJECT": "decision",
                  "EVIDENCE_CHANGED": "decision",
                  "CANCEL": "cancellation"}
        steps = [TimelineStep(
            ts=event.ts, actor=event.actor, action=event.action, hash=event.hash,
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
        from .auth import customer_in_scope
        self.expire()
        with self.db.connect() as conn:
            sql = "SELECT data FROM orders" + (" WHERE status='PENDING_APPROVAL'" if pending_only else "") + " ORDER BY rowid DESC"
            orders = [Order.model_validate_json(r[0]) for r in conn.execute(sql)]
            return [order for order in orders if agent_id is None or (
                order.agent_id == agent_id and customer_in_scope(agent_id, order.customer_id))]

    def approve(self, order_id, approver, comment="", evidence_revision=None, *, actor=None):
        return self._review(order_id, approver, comment, True, evidence_revision, actor=actor)

    def reject(self, order_id, approver, comment="", *, actor=None):
        return self._review(order_id, approver, comment, False, actor=actor)

    def _review(self, order_id, approver, comment, approve, evidence_revision=None, *, actor=None):
        from .foundation import PRINCIPAL
        principal = PRINCIPAL.get()
        if principal and principal.kind in {"buyer_user", "tenant_user"}:
            actor = f"human:buyer_approver:{principal.credential_ref}"
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
            from .autonomy import authorize_review
            from .foundation import PRINCIPAL
            principal = PRINCIPAL.get()
            authorize_review(self.policy.get(), principal, order)
            from .guardrail_profiles import authorize_approval, require_constraints
            self_approval = False
            if approve:
                self_approval = authorize_approval(conn, principal, order)
                require_constraints(conn, order.customer_id)
            if approve and hasattr(self.policy, "governance"):
                self.policy.governance.authorize_approval(conn, order, principal)
            profile = self.policy.get().customers.get(order.customer_id)
            if approve and (order.clinical_review == "required" or (profile and profile.clinical_review == "required")):
                if not principal or principal.kind not in {"buyer_user", "tenant_user"} or principal.role not in {"buyer_approver", "approver", "tenant_admin"}:
                    raise DomainError("BUYER_APPROVER_REQUIRED", "Clinical review requires a buyer_approver credential", 403)
            if order.status != "PENDING_APPROVAL":
                raise DomainError("INVALID_TRANSITION", "Only PENDING_APPROVAL orders can be approved or rejected")
            if approve and evidence_revision is not None and evidence_revision != order.evidence_revision:
                raise DomainError("EVIDENCE_CHANGED", "Review the current order evidence before approving.",
                                  data={"evidence_revision": order.evidence_revision, "changes": []})
            quote = self._quote(conn, order.quote_id)
            if approve:
                policy, record = self.policy.get(), None
                policy_snapshot = policy
                if self.mandates.stored(conn, "order", order_id) is not None:
                    policy, record, _ = self.mandates.authorize(conn, None, order.agent_id, quote.customer_id,
                        products=[o.product_id for o in order.lines], bound=("order", order_id), supplier_id=order.supplier_id)
                previous = order.model_copy(deep=True)
                order.mandate = record
                stock = self.suppliers.get(quote.supplier_id).fetch("availability", quote.customer_id, quote.deliver_by,
                                         [o.product_id for o in order.lines], counter, fresh=True)
                order.lines = self._refresh_stock(conn, order.lines, quote.deliver_by, stock, order.id)
                # Human review may accept delivery warnings, but never hard violations or unverifiable stock.
                # Evaluate as the placing agent so the audit keeps the mandate rules the human signs off
                # (per-order and daily limits); only REJECTED blocks an approval.
                if order.mandate is not None:
                    policy, order.mandate, _ = self.mandates.authorize(conn, None, order.agent_id, quote.customer_id,
                        products=[o.product_id for o in order.lines], bound=("order", order_id), supplier_id=order.supplier_id)
                from .auth import Principal
                authority = (previous.context or {}).get("connection_authority") or {}
                # Legacy calls lack a connection snapshot; keep their recorded continuity requirement.
                placing_principal = (Principal(order.agent_id, **{
                    k: v for k, v in authority.items() if k in Principal.__dataclass_fields__ and k != "agent_id"})
                    if authority or any(hit.rule == "connection_continuity" for hit in previous.policy.hits) else principal)
                evaluation_token = PRINCIPAL.set(placing_principal)
                try:
                    evaluation = self.policy.evaluate_order(conn, order.total, order.lines, self._violations(conn, quote),
                                                            agent_id=order.agent_id, customer_id=quote.customer_id,
                                                            warnings=order.warnings, policy=policy)
                finally:
                    PRINCIPAL.reset(evaluation_token)
                if order.basket_ref and any(not line.availability.enough for line in order.lines):
                    from .models import RuleHit
                    evaluation.hits.append(RuleHit(rule="basket_stock_unavailable", detail="All basket lines must be available before confirmation."))
                    if evaluation.outcome == "CONFIRMED":
                        evaluation.outcome = "PENDING_APPROVAL"
                comparison = self._recurring_comparison(quote, order.lines)
                drift = comparison["diff"] if comparison else (quote.context or {}).get("recurring_drift", [])
                if drift:
                    from .models import RuleHit
                    evaluation.hits.extend(RuleHit(rule="recurring_drift", detail=d)
                                           for d in drift)
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
                if quote.need_ref:
                    _, effective = self.policy.effective_need(self.lookup_need(conn, quote.need_ref).need, policy=policy)
                else:
                    legacy_need = NeedSpec.model_construct(customer_id=quote.customer_id, deliver_by=quote.deliver_by,
                        supplier_id=order.supplier_id, intent="Legacy quote", quantity=Quantity(value=1, unit="SALES_UNIT"))
                    _, effective = self.policy.effective_need(legacy_need, policy=policy)
                order.context = procurement_context(self.policy, quote.customer_id, quote.need_ref, effective,
                    order.mandate, supplier_id=order.supplier_id, snapshot=policy_snapshot)
                if authority:
                    order.context["connection_authority"] = authority
                if comparison:
                    order.context["recurring_comparison"] = comparison
                if quote.basket_ref or (self.lookup_need(conn, quote.need_ref).basket if quote.need_ref else None):
                    order.context["atomicity"] = "all-or-none gateway simulation; no supplier transmission"
                # Placement-time anomaly evidence remains reviewable after stock refresh.
                from .anomaly_service import CODES
                order.warnings = [w for w in quote.warnings if w.code not in CODES] + self.anomalies.warnings(order.anomalies)
                changes = evidence_changes(previous, order)
                if not changes and order.basket_ref and any(not line.availability.enough for line in order.lines):
                    raise DomainError("BASKET_STOCK_UNAVAILABLE", "All basket lines must be available before approval", 409)
                if changes:
                    order.evidence_revision += 1
            order.order_revision += 1
            if not changes:
                order.status = "CONFIRMED" if approve else "REJECTED"
                order.approver, order.comment = approver, comment
                order.approver_user_id = principal.user_id if principal else None
                order.self_approved = bool(approve and self_approval)
                order.approver_role = principal.role if principal else None
                order.approval_side = "buyer" if principal and principal.kind in {"buyer_user", "tenant_user"} else "valio_operator"
                if approve and self.suppliers.get(order.supplier_id).execution.mode == "supplier_backed":
                    self._execute(conn, order, quote)
                self._sign_receipt(order, quote, "operator")
            self._save_order(conn, order)
            self._reserve(conn, order)
            if approve and order.mandate is not None:
                self.mandates.used(conn, self.mandates.stored(conn, "order", order_id))
            data = {"inputs": {"comment": comment, "evidence_revision": evidence_revision}, "stated_name": approver,
                    "self_approved": order.self_approved,
                    "approval_warnings": ["SELF_APPROVAL_ALLOWED"] if order.self_approved else [],
                    "organisation_id": principal.organisation_id if principal else None,
                    "evidence_revision": order.evidence_revision, "approval_side": order.approval_side, "user_id": principal.user_id if principal else None, "role": principal.role if principal else None,
                    **mandate_evidence(order.mandate),
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
                if not approve:
                    self._suggest_review(conn, order, "human_rejection")
        # Leave the writer transaction successfully so the pending revision and its audit survive the 409.
        if changes:
            raise DomainError("EVIDENCE_CHANGED", "Order evidence changed; review the refreshed order before approving.",
                              data={"evidence_revision": order.evidence_revision, "changes": changes})
        return order

    def cancel(self, order_id, actor="system", reason="", *, stated_name=None, mandate=None):
        """Core transition for a local confirmed order, before any known cutoff."""
        inputs = {"order_id": order_id, "reason": reason}
        if stated_name is not None:
            inputs["stated_name"] = stated_name
        return self._run("CANCEL", actor, inputs,
                         lambda counter: self._cancel(order_id, actor, reason, inputs, counter, mandate))

    def _cancel(self, order_id, actor, reason, inputs, counter, mandate=None):
        with self.db.connect(write=True) as conn:
            order = self._order(conn, order_id)
            inputs["quote_id"] = order.quote_id
            if actor.startswith("agent:") and order.agent_id != actor.removeprefix("agent:"):
                raise DomainError("ORDER_NOT_FOUND", "Order was not found", 404)
            if actor.startswith("agent:"):
                self.mandates.authorize(conn, mandate, actor.removeprefix("agent:"), order.customer_id,
                    products=[line.product_id for line in order.lines], bound=("order", order.id), supplier_id=order.supplier_id)
            if order.status != "CONFIRMED":
                raise DomainError("INVALID_TRANSITION", "Only CONFIRMED orders can be cancelled")
            cutoffs = [o.availability.closing_time for o in order.lines if o.availability.closing_time]
            if any(c <= audit.utcnow() for c in cutoffs):
                raise DomainError("CUTOFF_PASSED", "Cancellation cutoff has passed")
            if order.execution and order.execution.mode == "supplier_backed" and order.execution.supplier_ref:
                order.execution = self.suppliers.get(order.supplier_id).execution.cancel(order.execution.supplier_ref)
                if order.execution.status != "cancelled":
                    raise DomainError("SUPPLIER_UNAVAILABLE", "Supplier did not acknowledge cancellation", 502)
            order.status = "CANCELLED"
            order.order_revision += 1
            order.cancelled_by = inputs.get("stated_name") or (actor.split(":", 1)[1] if actor.startswith(("human:", "agent:")) else actor)
            order.cancel_reason = reason
            self._sign_receipt(order, self._quote(conn, order.quote_id),
                               "agent" if actor.startswith("agent:") else "operator" if actor.startswith("human:") else "system")
            self._save_order(conn, order)
            self._reserve(conn, order)
            audit.append(conn, actor, "CANCEL", "Confirmed order cancelled; local reservations released.",
                         order_id=order.id, quote_id=order.quote_id,
                         data={"reason": reason, "live_calls": counter.live().model_dump(),
                               **({"stated_name": inputs["stated_name"]} if "stated_name" in inputs else {})})
            return order
