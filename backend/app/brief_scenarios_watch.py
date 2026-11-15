"""Predict shortages through existing lookup/recurring records and the signed outbox."""
import json
import threading
from datetime import datetime, timedelta
from hashlib import sha256
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request

from . import audit
from .auth import Principal, require_operator, resolve_principal
from .db import DomainError
from .foundation import EVENT_ACTIONS, PRINCIPAL
from .models import NeedSpec, SubstituteFor
from .recurring import rule_for
from .valio import RequestCounter
from .brief_scenarios import trade_off, viable_alternative

EVENT_ACTIONS["STOCKOUT_PREDICTED"] = "stockout.predicted"


class StockoutWatch:
    def __init__(self, gateway):
        self.g = gateway
        self.lock = threading.Lock()
        self.last_tick = None
        with gateway.db.connect(write=True) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS brief_stockout_inbox(id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE, customer_id TEXT, data TEXT, tenant_id TEXT NOT NULL DEFAULT 'platform')")

    def tick(self, now):
        if self.last_tick is None or (now-self.last_tick).total_seconds() >= 60:
            self.run(now)
            self.last_tick = now

    def targets(self, now):
        """Each occurrence/quote/basket is inspected once, without creating fresh open lookups."""
        end = now + timedelta(days=7)
        today = now.astimezone(ZoneInfo("Europe/Helsinki")).date()
        last_day = today + timedelta(days=7)
        for row in self.g.recurring.rows():
            if row["status"] != "ACTIVE":
                continue
            try:
                with self.g.db.connect() as conn:
                    token = self.g.recurring.authority(conn, row)
            except DomainError:
                continue  # Recurring execution records the revocation through its existing stop path.
            for occurrence in rule_for(row).between(now, end, inc=True):
                delivery = occurrence.date() + timedelta(days=row.get("delivery_offset_days", 0))
                if delivery > last_day:
                    continue
                if row["basket"]:
                    with self.g.db.connect() as conn:
                        quote = self.g.orders._quote(conn, self.g.orders.get_order(row["approved_order_id"]).quote_id)
                        basket = json.loads(conn.execute("SELECT data FROM basket_lookups WHERE id=?", (quote.need_ref,)).fetchone()[0])
                    for line in row["approved_lines"]:
                        raw = basket["line_needs"][line["product_id"]]
                        yield f"recurring:{row['id']}:{occurrence.date()}", row["agent_id"], delivery, line["product_id"], raw, token
                else:
                    for line in row["approved_lines"]:
                        yield f"recurring:{row['id']}:{occurrence.date()}", row["agent_id"], delivery, line["product_id"], row["template"], token
        with self.g.db.connect() as conn:
            quotes = conn.execute("SELECT id,json_extract(data,'$.need_ref') AS need_ref FROM quotes WHERE json_extract(data,'$.status')='OPEN'").fetchall()
            baskets = [json.loads(r[0]) for r in conn.execute("SELECT data FROM basket_lookups")]
            ordered = {r[0] for r in conn.execute("SELECT json_extract(data,'$.need_ref') FROM quotes WHERE json_extract(data,'$.status')!='OPEN'")}
        quoted = {q["need_ref"] for q in quotes}
        for row in quotes:
            with self.g.db.connect() as conn:
                try:
                    # Monitoring must authenticate the same promise as placement
                    # before using any stored fields to make supplier calls.
                    quote = self.g.orders._verified_quote(conn, row["id"], None)
                except DomainError:
                    continue
                delivery = quote.deliver_by
                if not today <= delivery <= last_day or quote.expires_at <= now:
                    continue
                lookup = self.g.orders.lookup_need(conn, quote.need_ref)
                from .baskets import bound_need
                for line in quote.lines:
                    need = bound_need(conn, lookup, line.product_id) if lookup else None
                    if need:
                        token = self.g.mandates.stored(conn, "quote", quote.id)
                        yield "quote:"+quote.id, quote.agent_id, delivery, line.product_id, need.model_dump(mode="json"), token
        for basket in baskets:
            if basket["basket_lookup_id"] in quoted | ordered:
                continue
            delivery = datetime.fromisoformat(basket["deliver_by"]).date()
            if today <= delivery <= last_day:
                for pid, raw in basket["line_needs"].items():
                    with self.g.db.connect() as conn:
                        token = self.g.mandates.stored(conn, "lookup", basket["basket_lookup_id"])
                    yield "basket:"+basket["basket_lookup_id"], basket["agent_id"], delivery, pid, raw, token

    def run(self, now=None):
        with self.g.activate():
            return self._run(now)

    def _run(self, now=None):
        now = now or audit.utcnow()
        if not self.lock.acquire(blocking=False):
            return {"checked": 0, "alerts": [], "busy": True}
        checked, alerts, errors = 0, [], []
        try:
            for source, agent, delivery, pid, raw, credential in self.targets(now):
                if delivery <= now.astimezone(ZoneInfo("Europe/Helsinki")).date():
                    continue
                try:
                    need = NeedSpec.model_validate({**raw, "deliver_by": delivery}, context={"stored": True})
                    adapter = self.g.suppliers.get(need.supplier_id)
                    with self.g.db.connect() as conn:
                        policy, _, _ = self.g.mandates.authorize(conn, credential, agent, need.customer_id,
                            products=[pid], supplier_id=need.supplier_id)
                    need, effective = self.g.policy.effective_need(need, policy)
                    counter = RequestCounter()
                    price = adapter.fetch("price", need.customer_id, delivery, [pid], counter, fresh=True)
                    stock = adapter.fetch("availability", need.customer_id, delivery, [pid], counter, fresh=True)
                    from .lookup import offer_for
                    from .brief_scenarios import annotate_timing
                    offer = offer_for(adapter.catalog.get(pid), need, price.get(pid, {}), stock.get(pid, {}), sources=effective)
                    annotate_timing([offer], need, adapter.catalog, price, stock)
                    checked += 1
                    if offer.arrives_in_time:
                        continue
                    substitutes = []
                    if need.function:
                        alternative_need = need.model_copy(update={"substitute_for": SubstituteFor(product_id=pid)})
                        alternative_need = NeedSpec.model_validate(alternative_need.model_dump(), context={"stored": True})
                        # Existing lookup enforces functional fit, standing rules and supplier rate limits.
                        alternative_need.search_terms = [need.function.product_type]
                        # Internal monitoring uses the original purchasing grant, not the scheduler caller.
                        owner = Principal(agent, customer_ids=(need.customer_id,))
                        owner = self.g.accounts.bind(owner)
                        principal_token = PRINCIPAL.set(owner)
                        try:
                            result = self.g.find_offers(alternative_need, f"agent:{agent}", mandate=credential)
                        finally:
                            PRINCIPAL.reset(principal_token)
                        for candidate in result.offers:
                            if candidate.product_id != pid and viable_alternative(candidate):
                                substitutes.append({"product_id": candidate.product_id, "requires_approval": True,
                                    "trade_off": candidate.trade_off or trade_off(pid, candidate, need, adapter.catalog, price, stock)})
                    data = {"id": str(uuid4()), "code": "STOCKOUT_PREDICTED", "customer_id": need.customer_id,
                            "agent_id": agent, "source": source, "product_id": pid, "order_date": delivery.isoformat(),
                            "predicted_at": now.isoformat(), "substitute": substitutes[0] if substitutes else None,
                            "alternatives": substitutes, "requires_approval": True,
                            "substitute_unavailable_reason": (None if substitutes else "recorded functional need required" if not need.function else "no function-compatible in-time product passes the constraints")}
                    fingerprint = sha256(json.dumps([source, pid, delivery.isoformat()], sort_keys=True).encode()).hexdigest()
                    with self.g.db.connect(write=True) as conn:
                        inserted = conn.execute("INSERT OR IGNORE INTO brief_stockout_inbox(id,fingerprint,customer_id,data) VALUES(?,?,?,?)",
                            (data["id"], fingerprint, need.customer_id, json.dumps(data))).rowcount
                        if inserted:
                            audit.append(conn, "system:stockout-watch", "STOCKOUT_PREDICTED",
                                "Stock-out predicted before the order date; buyer approval required for substitution.", data=data)
                            alerts.append(data)
                except DomainError as error:
                    errors.append({"source": source, "code": error.code})
            return {"checked": checked, "alerts": alerts, "errors": errors}
        finally:
            self.lock.release()

    def inbox(self, principal):
        if principal.kind not in {"agent", "buyer_user", "tenant_user"} and not principal.operator:
            raise DomainError("BUYER_REQUIRED", "Stock-out inbox belongs to the buyer", 403)
        with self.g.db.connect() as conn:
            return [json.loads(r["data"]) for r in conn.execute("SELECT customer_id,data FROM brief_stockout_inbox ORDER BY rowid DESC")
                    if principal.operator or r["customer_id"] in principal.customer_ids]


router = APIRouter()


@router.post("/v1/stockout-watch/run", response_model=dict)
def run_watch(request: Request, principal=Depends(require_operator)):
    request.app.state.gateway.limiter.consume(principal.id, "mutation")
    return request.app.state.gateway.stockout_watch.run()


@router.get("/v1/stockout-watch/inbox", response_model=list[dict])
def inbox(request: Request, principal=Depends(resolve_principal)):
    request.app.state.gateway.limiter.consume(principal.id, "read")
    return request.app.state.gateway.stockout_watch.inbox(principal)
