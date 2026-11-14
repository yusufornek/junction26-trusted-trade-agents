"""History adapter and exception actions for the pure anomaly checks."""
import json
from datetime import datetime, timedelta, timezone
from math import isclose

from fastapi import APIRouter, Depends, Header, Request

from . import audit
from .anomaly import Point, category_check, price_check, quantity_check, velocity_check
from .auth import check_owner, resolve_principal
from .db import DomainError
from .models import AnomalyCheck, AnomalyFix, Model, Quote, QuoteLineRequest, QuoteRequest


CODES = {"QUANTITY_ANOMALY", "PRICE_ANOMALY", "VELOCITY_ANOMALY", "NEW_CATEGORY"}


def moment(value):
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


class FixResult(Model):
    quotes: list[Quote]
    source_quote_id: str
    fix_id: str


class Anomalies:
    def __init__(self, gateway, forecaster=None):
        self.g, self.forecaster = gateway, forecaster

    def history(self, conn, agent, customer, supplier, now, days):
        """Merge memory and pre-memory confirmed orders, once per order/product.

        Matching agent AND customer AND supplier prevents another buyer's series
        from revealing its history or diluting this buyer's guardrails.
        """
        start = now - timedelta(days=days)
        items = {}
        for row in conn.execute("SELECT data FROM purchase_memory WHERE customer_id=?", (customer,)):
            item = json.loads(row[0])
            if item.get("agent_id") != agent or item.get("supplier_id", "valio") != supplier:
                continue
            at = moment(item.get("timestamp") or item["date"])
            if not start <= at < now:
                continue
            base = (item.get("baseline") or {}).get("lines", [{}])[0]
            units = item.get("sales_units", base.get("quantity"))
            if units is None and item.get("unit") == "SALES_UNIT":
                units = item["quantity"]
            if units is None or units <= 0:
                continue  # Never mix incomparable request units.
            items[(item["order_id"], item["product_id"])] = {
                **item, "at": at, "sales_units": units,
                "unit_price": item.get("unit_price", item["total_paid"] / units)}
        for row in conn.execute("SELECT data FROM orders WHERE status='CONFIRMED' AND json_extract(data,'$.agent_id')=?", (agent,)):
            order = json.loads(row[0])
            quote_row = conn.execute("SELECT data FROM quotes WHERE id=?", (order["quote_id"],)).fetchone()
            quote = json.loads(quote_row[0]) if quote_row else {}
            if (order.get("customer_id") or quote.get("customer_id")) != customer or order.get("supplier_id", "valio") != supplier:
                continue
            at = moment(order.get("decided_at") or order["created_at"])
            if not start <= at < now:
                continue
            for line in order["lines"]:
                items.setdefault((order["id"], line["product_id"]), {
                    "order_id": order["id"], "product_id": line["product_id"], "at": at,
                    "quantity": line["units_needed"] * (line.get("size_per_sales_unit") or 1),
                    "unit": line.get("size_unit", "SALES_UNIT"),
                    "sales_units": line["units_needed"], "unit_price": line["unit_price"]})
        return list(items.values())

    def checks(self, conn, quote, *, placement=False):
        now = audit.utcnow()
        policy = self.g.policy.get()
        settings = policy.anomalies
        tenant = conn.execute("SELECT tenant_id FROM tenant_customers WHERE customer_id=?", (quote.customer_id,)).fetchone()
        preset = policy.tenant_anomaly_settings.get(tenant[0], {}) if tenant else {}
        k = preset.get("quantity_k", settings.sensitivity_k[settings.sensitivity])
        velocity_k = preset.get("velocity_k", settings.sensitivity_k[settings.sensitivity])
        price_increase_pct = preset.get("price_increase_pct", settings.price_increase_pct)
        history = self.history(conn, quote.agent_id, quote.customer_id, quote.supplier_id, now, settings.history_days)
        catalog = self.g.suppliers.get(quote.supplier_id).catalog
        def category(item):
            if item.get("category") is not None:
                return item["category"]
            try:
                return catalog.get(item["product_id"]).category
            except DomainError:
                return None
        categories = [c for item in history if (c := category(item)) is not None]
        checks = []
        for line in quote.lines:
            product = catalog.get(line.product_id)
            same = [h for h in history if h["product_id"] == line.product_id]
            # Category fallback is normalized to the bound request unit, never
            # comparing one case of one product with one case of another size.
            unit, factor = "SALES_UNIT", 1
            quantities = [Point(h["at"], h["sales_units"]) for h in same]
            if len(quantities) < 4 and line.size_per_sales_unit and line.size_unit != "SALES_UNIT":
                unit, factor = line.size_unit, line.size_per_sales_unit
                quantities = [Point(h["at"], h["quantity"]) for h in history
                              if category(h) == product.category and h.get("unit") == unit]
            result = quantity_check(quantities, line.units_needed * factor, now, k, forecaster=self.forecaster)
            fixes = []
            if result["status"] == "anomaly":
                usual = result["expected_band"]["median"] / factor
                step = line.allowed_lot_size
                usual = max(step, round(usual / step) * step)
                multipliers = set(product.units.values()) | {line.size_per_sales_unit or 1}
                if any(m > 1 and isclose(line.units_needed, usual * m, rel_tol=.03) for m in multipliers):
                    fixes.append(AnomalyFix(id=f"{line.product_id}:correct", kind="correct_quantity", product_id=line.product_id,
                        quantity_sales_units=usual, description=f"Correct possible case/unit confusion to {usual:g} sales units."))
                if line.units_needed > usual:
                    fixes.append(AnomalyFix(id=f"{line.product_id}:split", kind="split", product_id=line.product_id,
                        quantity_sales_units=usual, remainder_sales_units=line.units_needed - usual,
                        description="Quote the usual quantity now and hold the remainder for human approval."))
            checks.append(AnomalyCheck(code="QUANTITY_ANOMALY", product_id=line.product_id,
                observed=line.units_needed * factor, unit=unit, fixes=fixes, **result))
            result = price_check([Point(h["at"], h["unit_price"]) for h in same], line.unit_price, now, price_increase_pct)
            fixes = []
            if result["status"] == "anomaly" and quote.need_ref:
                lookup = self.g.orders.lookup_need(conn, quote.need_ref)
                for alternative in lookup.offers:  # Already ranked by the normal lookup.
                    if (alternative.product_id != line.product_id and alternative.purchase_total is not None
                            and alternative.purchase_total < line.total and alternative.units_needed):
                        fixes.append(AnomalyFix(id=f"{line.product_id}:cheaper:{alternative.product_id}",
                            kind="cheaper_alternative", product_id=alternative.product_id,
                            quantity_sales_units=alternative.units_needed,
                            description=f"Re-quote ranked alternative {alternative.product_id} with fresh price and stock."))
                        break
            checks.append(AnomalyCheck(code="PRICE_ANOMALY", product_id=line.product_id,
                observed=line.unit_price, unit=f"EUR/{line.sales_unit}", fixes=fixes, **result))
            checks.append(AnomalyCheck(code="NEW_CATEGORY", product_id=line.product_id,
                status=category_check(categories, product.category) if product.category else "insufficient_history",
                observed=product.category or "unknown", unit="category",
                expected_band=None,
                history_window={"start": min((h["at"].isoformat() for h in history), default=None),
                                "end": max((h["at"].isoformat() for h in history), default=None),
                                "points": len(categories), "categories": sorted(set(categories))}))
        # Count stored attempts too: pending/rejected orders must not hide a burst.
        times = {h["order_id"]: h["at"] for h in history}
        for row in conn.execute("SELECT data FROM orders WHERE json_extract(data,'$.agent_id')=?", (quote.agent_id,)):
            order = json.loads(row[0])
            if order.get("customer_id") == quote.customer_id and order.get("supplier_id", "valio") == quote.supplier_id:
                at = moment(order["created_at"])
                if now - timedelta(days=settings.history_days) <= at <= now:
                    times[order["id"]] = at
        observed = 1 + sum(now - timedelta(hours=1) < t <= now for t in times.values())
        result = velocity_check(times.values(), observed, now, velocity_k, settings.velocity_min_upper)
        pause = result.pop("pause")
        fixes = [AnomalyFix(id="velocity:pause", kind="pause", description="Ask the buyer administrator to pause this agent.",
                           action_url=f"/v1/buyer/agents/{quote.agent_id}/suspend"),
                 AnomalyFix(id="velocity:resume", kind="resume", description="After review, ask the buyer administrator to resume this agent.",
                           action_url=f"/v1/buyer/agents/{quote.agent_id}/resume")] if result["status"] == "anomaly" else []
        checks.append(AnomalyCheck(code="VELOCITY_ANOMALY", observed=observed, unit="orders/hour", fixes=fixes, **result))
        # When no safe product/quantity correction exists, the human can pause
        # buying while reviewing the ordinary approval journey. Never invent a
        # cheaper product or silently switch a category to manufacture a fix.
        for check in checks:
            if check.status == "anomaly" and not check.fixes:
                check.fixes = [AnomalyFix(id=f"{check.product_id or 'agent'}:{check.code}:pause", kind="pause",
                    description="Pause purchasing while the buyer reviews this unusual order.",
                    action_url=f"/v1/buyer/agents/{quote.agent_id}/suspend")]
        if placement and pause:
            self.pause(conn, quote, checks[-1])
        return checks

    def pause(self, conn, quote, check):
        data = {"agent_id": quote.agent_id, "customer_id": quote.customer_id, "customer_ids": [quote.customer_id],
                "suspended": True, "reason": "VELOCITY_ANOMALY: rolling-hour count reached twice the expected band",
                "actor": "system:anomalies", "at": audit.utcnow().isoformat(), "source": "anomaly",
                "anomaly": check.model_dump(mode="json"), "recipient": "buyer_admin"}
        conn.execute("INSERT INTO buyer_suspensions VALUES(?,?) ON CONFLICT(agent_id) DO UPDATE SET data=excluded.data",
                     (quote.agent_id, json.dumps(data)))
        audit.append(conn, "system:anomalies", "AGENT_SUSPENDED", data["reason"], quote_id=quote.id, data=data)
        audit.append(conn, "system:anomalies", "AGENT_ANOMALY_ALERT", "Review the paused agent's order burst.", quote_id=quote.id, data=data)

    def warnings(self, checks):
        from .models import Warning
        return [Warning(code=c.code, text=(c.explanation_text + " " if c.explanation_text else "") +
                        f"{c.code}: observed {c.observed} {c.unit}; expected {c.expected_band}; history {c.history_window}.")
                for c in checks if c.status == "anomaly"]

    def apply_fix(self, quote_id, fix_id, agent_id="external", *, operator=False, mandate=None, principal=None):
        # Fresh quote creation uses all normal scope, mandate, constraint, price,
        # stock and approval checks. Nothing here places or approves an order.
        from .buyer import check_suspension
        with self.g.db.connect(write=True) as conn:
            reviewer = operator or (principal is not None and principal.kind in {"buyer_user", "tenant_user"})
            quote = self.g.orders._verified_quote(conn, quote_id, None if reviewer else agent_id)
            if reviewer:
                from .autonomy import authorize_review
                authorize_review(self.g.policy.get(), principal, quote)
            agent_id = quote.agent_id
            check_suspension(self.g.db, agent_id)
            self.g.mandates.authorize(conn, mandate, agent_id, quote.customer_id,
                bound=("quote", quote.id), products=[l.product_id for l in quote.lines], supplier_id=quote.supplier_id)
            pending = conn.execute("SELECT id FROM orders WHERE quote_id=? AND status='PENDING_APPROVAL'", (quote.id,)).fetchone()
            if quote.expires_at <= audit.utcnow() or not (quote.status == "OPEN" or quote.status == "ACCEPTED" and pending):
                raise DomainError("EXPIRED_QUOTE", "Create a fresh quote before applying an anomaly fix")
            old_order = self.g.orders._order(conn, pending[0]) if pending else None
            evidence = old_order.anomalies if old_order else quote.anomalies
            match = next(((c, f) for c in evidence for f in c.fixes if f.id == fix_id), None)
            if match is None:
                raise DomainError("FIX_NOT_FOUND", "Proposed fix was not found", 404)
            check, fix = match
            if fix.kind in {"pause", "resume"}:
                raise DomainError("BUYER_ADMIN_REQUIRED", "Use the buyer pause/resume endpoint after human review", 403)
            if old_order:
                old_order.status = "CANCELLED"
                old_order.order_revision += 1
                old_order.cancel_reason = "Superseded by anomaly fix " + fix.id
                old_order.cancelled_by = principal.id if principal else agent_id
                self.g.orders._sign_receipt(old_order, quote, "operator" if reviewer else "agent")
                self.g.orders._save_order(conn, old_order)
                self.g.orders._reserve(conn, old_order)
                audit.append(conn, f"human:{principal.id}" if reviewer and principal else f"agent:{agent_id}",
                             "CANCEL", old_order.cancel_reason, order_id=old_order.id, quote_id=quote.id,
                             data={"source": "anomaly_fix", "fix_id": fix.id})
            def requote(quantity, remainder=False):
                lines = [QuoteLineRequest(product_id=fix.product_id if l.product_id == check.product_id else l.product_id,
                    quantity_sales_units=quantity if l.product_id == check.product_id else l.units_needed)
                    for l in quote.lines if not remainder or l.product_id == check.product_id]
                new = self.g.orders.create_quote(QuoteRequest(customer_id=quote.customer_id, deliver_by=quote.deliver_by,
                    need_ref=quote.need_ref, lines=lines), agent_id, mandate=mandate)
                if (quote.context or {}).get("recurring_drift"):
                    # A quantity/price fix cannot clear an independent recurring
                    # baseline review that was attached to the source promise.
                    new.context["recurring_drift"] = quote.context["recurring_drift"]
                if remainder:
                    # A remainder can itself be a usual-sized purchase. Hold it
                    # once, with an explicit reason, without duplicate cards.
                    target = next(l for l in new.lines if l.product_id == check.product_id)
                    if check.unit != "SALES_UNIT" and (not target.size_per_sales_unit or target.size_unit != check.unit):
                        raise DomainError("FIX_NOT_FOUND", "Fresh pack content cannot support this split; refresh the lookup", 409)
                    observed = quantity * (target.size_per_sales_unit if check.unit != "SALES_UNIT" else 1)
                    forced = check.model_copy(update={"observed": observed,
                        "explanation_text": "This is the remainder of a split unusual purchase and requires human approval.",
                        "fixes": [AnomalyFix(id="split:pause", kind="pause", description="Pause while the buyer reviews the remainder.",
                            action_url=f"/v1/buyer/agents/{agent_id}/suspend")]})
                    new.anomalies = [c for c in new.anomalies if not (c.code == forced.code and c.product_id == forced.product_id)] + [forced]
                    new.warnings = [w for w in new.warnings if w.code not in CODES] + self.warnings(new.anomalies)
                    new.context["anomaly_split_remainder"] = True
                new.signature = self.g.signing.sign_quote(new)
                self.g.orders._save_quote(conn, new)
                return new
            quotes = [requote(fix.quantity_sales_units)]
            if fix.kind == "cheaper_alternative" and quotes[0].total >= quote.total:
                raise DomainError("FIX_NOT_FOUND", "The freshly priced alternative is no longer cheaper; refresh the lookup", 409)
            if fix.kind == "split":
                quotes.append(requote(fix.remainder_sales_units, True))
            # Supersede the unsafe promise so it cannot accidentally be placed too.
            quote.status, quote.expiration_reason = "EXPIRED", "anomaly_fix_applied"
            self.g.orders._save_quote(conn, quote)
            audit.append(conn, f"human:{principal.id}" if reviewer and principal else f"agent:{agent_id}", "ANOMALY_FIX_APPLIED", fix.description, quote_id=quote.id,
                         data={"customer_id": quote.customer_id, "fix": fix.model_dump(mode="json"),
                               "new_quote_ids": [q.id for q in quotes]})
            return FixResult(quotes=quotes, source_quote_id=quote.id, fix_id=fix.id)


router = APIRouter()


@router.post("/v1/quotes/{quote_id}/anomaly-fixes/{fix_id}", response_model=FixResult,
             summary="Apply a proposed anomaly fix by creating fresh quotes; never places or approves")
def apply_fix(quote_id: str, fix_id: str, request: Request, principal=Depends(resolve_principal),
              mandate: str | None = Header(default=None, alias="Agent-Mandate")):
    return request.app.state.gateway.anomalies.apply_fix(quote_id, fix_id, principal.agent_id,
                                                       operator=principal.operator, mandate=mandate, principal=principal)
