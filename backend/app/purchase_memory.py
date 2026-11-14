"""Customer-owned purchase rhythms. Suggestions never commit a purchase."""
import calendar
import json
from copy import deepcopy
from datetime import date, datetime, timedelta
from dataclasses import replace
from hashlib import sha256
from statistics import median
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import Field, ValidationError, field_validator

from . import audit
from .auth import Principal, resolve_principal
from .buyer import check_suspension
from .db import DomainError
from .foundation import PRINCIPAL
from .models import InputText, InputTexts, LookupResult, Model, NeedSpec, Quote
from .recurring import Deviations, snapshot
from .mandates import decode


class MemoryPreferences(Model):
    enabled: bool = True
    excluded_products: InputTexts = Field(default_factory=list)
    suggestion_horizon: int = Field(default=14, ge=1, le=365, strict=True)


class SuggestionAction(Model):
    buyer_agent_id: InputText | None = None


class MakeRecurring(SuggestionAction):
    timezone: InputText = "Europe/Helsinki"
    permitted_deviations: Deviations = Field(default_factory=Deviations)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        from zoneinfo import ZoneInfoNotFoundError
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError("Use a valid IANA timezone") from error
        return value


class Snooze(Model):
    days: int = Field(ge=1, le=365, strict=True)


def preferences(conn, customer):
    row = conn.execute("SELECT data FROM memory_preferences WHERE customer_id=?", (customer,)).fetchone()
    return json.loads(row[0]) if row else MemoryPreferences().model_dump()


def remember_order(conn, order_id):
    """Capture once at confirmation, in the same transaction as the decision and audit.

    Quantities are purchased quantities expressed in the bound request's unit,
    including pack rounding. Separate unit series never mix litres and kilograms.
    """
    row = conn.execute("SELECT data FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        return
    order = json.loads(row[0])
    if order["status"] == "CANCELLED":
        conn.execute("DELETE FROM purchase_memory WHERE json_extract(data,'$.order_id')=?", (order_id,))
        return
    if order["status"] != "CONFIRMED":
        return
    customer = order.get("customer_id") or (order.get("context") or {}).get("customer_id")
    prefs = preferences(conn, customer)
    if not prefs.get("enabled", True) or prefs.get("forgotten_at"):
        return
    raw_quote = conn.execute("SELECT data FROM quotes WHERE id=?", (order["quote_id"],)).fetchone()
    if not raw_quote:
        return
    quote = Quote.model_validate_json(raw_quote[0])
    customer = quote.customer_id
    lookup = conn.execute("SELECT data FROM lookups WHERE id=?", (quote.need_ref,)).fetchone()
    basket = conn.execute("SELECT data FROM basket_lookups WHERE id=?", (quote.need_ref,)).fetchone()
    needs = json.loads(basket[0]).get("line_needs", {}) if basket else {}
    common = json.loads(lookup[0]).get("need", {}) if lookup else {}
    baseline = snapshot(None, quote)
    for index, line in enumerate(quote.lines):
        if line.product_id in prefs.get("excluded_products", []):
            continue
        need = deepcopy(needs.get(line.product_id, common))
        unit = need.get("quantity", {}).get("unit", "SALES_UNIT")
        if unit != "SALES_UNIT" and (line.size_unit != unit or not line.size_per_sales_unit):
            continue  # Missing conversion must never become a fabricated quantity.
        quantity = line.units_needed * (line.size_per_sales_unit if unit != "SALES_UNIT" else 1)
        if not need:
            need = {"customer_id": customer, "intent": line.name, "quantity": {"value": quantity, "unit": unit}}
        item = {"order_id": order_id, "customer_id": customer, "product_id": line.product_id,
                "name": line.name, "date": (order.get("decided_at") or order["created_at"])[:10],
                "quantity": quantity, "unit": unit, "total_paid": line.total,
                "currency": quote.currency, "agent_id": order["agent_id"], "need": need,
                "environment": (quote.context or {}).get("environment", "live"),
                "baseline": {"lines": [baseline["lines"][index]], "basket_cost": line.total}}
        conn.execute("INSERT OR IGNORE INTO purchase_memory VALUES(?,?,?)",
                     (f"{order_id}:{line.product_id}", customer, json.dumps(item)))


def detect_patterns(purchases):
    groups = {}
    for item in purchases:
        groups.setdefault((item["customer_id"], item["product_id"], item["unit"]), []).append(item)
    result = []
    for (customer, product, unit), items in sorted(groups.items()):
        items = sorted(items, key=lambda item: (item["date"], item["order_id"]))
        if len(items) < 3:
            continue
        dates = [date.fromisoformat(item["date"]) for item in items]
        intervals = [(b - a).days for a, b in zip(dates, dates[1:])]
        quantities = [item["quantity"] for item in items]
        interval, quantity = median(intervals), median(quantities)
        spread, quantity_spread = max(intervals) - min(intervals), max(quantities) - min(quantities)
        months = [d.year * 12 + d.month for d in dates]
        monthly = len({d.day for d in dates}) == 1 and all(b - a == 1 for a, b in zip(months, months[1:]))
        if monthly:
            year, month = (dates[-1].year + 1, 1) if dates[-1].month == 12 else (dates[-1].year, dates[-1].month + 1)
            predicted = date(year, month, min(dates[-1].day, calendar.monthrange(year, month)[1]))
            rrule = f"FREQ=MONTHLY;BYMONTHDAY={dates[-1].day}"
        else:
            predicted = dates[-1] + timedelta(days=max(1, round(interval)))
            rrule = f"FREQ=DAILY;INTERVAL={max(1, round(interval))}"
        irregular = interval <= 0 or spread > max(3, interval * .5) or quantity_spread > quantity * .5
        confidence = "low" if irregular else "high" if len(items) >= 6 and spread <= max(3, interval * .1) and quantity_spread <= quantity * .1 else "medium"
        explanation = {"low": "Irregular cadence or quantity", "high": "Stable cadence and quantity across at least six orders",
                       "medium": "Limited history or moderate variation"}[confidence]
        reason = (f"{explanation}: {len(items)} confirmed orders; interval spread {spread:g} days (low-confidence threshold "
                  f"{max(3, interval * .5):g}); quantity spread {quantity_spread:g} {unit} "
                  f"(low-confidence threshold {quantity * .5:g}).")
        latest = items[-1]
        # A new purchase produces a new suggestion; dismissing this need does not dismiss all future needs.
        identifier = sha256(json.dumps([customer, product, unit, [i["order_id"] for i in items]]).encode()).hexdigest()[:32]
        result.append({"id": identifier, "customer_id": customer, "product_id": product,
            "name": latest["name"], "unit": unit, "order_count": len(items),
            "median_interval_days": interval, "interval_spread_days": spread,
            "typical_quantity": quantity, "quantity_spread": quantity_spread,
            "suggested_quantity": {"value": quantity, "unit": unit}, "last_order_date": dates[-1].isoformat(),
            "predicted_next_need_date": predicted.isoformat(), "confidence": confidence, "confidence_reason": reason,
            "cadence_rrule": rrule, "environment": latest["environment"],
            "evidence": [{k: i[k] for k in ("date", "quantity", "unit", "total_paid", "currency", "agent_id", "order_id")} for i in items],
            "sentence": f"You buy about {quantity:g} {unit} of {latest['name']} every {'month' if monthly else f'{interval:g} days'}; last ordered {dates[-1].isoformat()}, next needed around {predicted.isoformat()}. Shall I prepare it?",
            "_latest": latest})
    return result


class PurchaseMemory:
    def __init__(self, gateway):
        self.g = gateway
        # One migration backfill. Disabled-period orders are never backfilled on restart.
        with self.g.db.connect(write=True) as conn:
            if not conn.execute("SELECT 1 FROM memory_meta WHERE id='backfilled'").fetchone():
                for row in conn.execute("SELECT id FROM orders WHERE status='CONFIRMED'").fetchall():
                    remember_order(conn, row[0])
                conn.execute("INSERT INTO memory_meta VALUES('backfilled')")

    def scoped(self, principal, mandate=None):
        if principal.operator:
            return principal
        if principal.kind not in {"agent", "buyer_user"}:
            raise DomainError("BUYER_REQUIRED", "Purchase memory requires a buyer credential", 403)
        if principal.kind == "agent":
            check_suspension(self.g.db, principal.id)
            principal = self.g.proposals.effective_principal(principal, mandate)
            if mandate:
                customer = decode(mandate)[1]["vc"]["credentialSubject"]["customer_id"]
                principal = replace(principal, customer_ids=(customer,))
            else:
                grant = self.g.policy.get().agents.get(principal.id)
                allowed = tuple(c for c in principal.customer_ids if grant and c in grant.customer_ids)
                if not grant or principal.customer_ids and not allowed:
                    raise DomainError("CUSTOMER_NOT_IN_MANDATE", "Current agent authority excludes these customers", 403)
                principal = replace(principal, customer_ids=allowed)
        return principal

    def patterns(self, principal, mandate=None):
        principal = self.scoped(principal, mandate)
        with self.g.db.connect() as conn:
            items = []
            for row in conn.execute("SELECT * FROM purchase_memory ORDER BY id"):
                if not principal.operator and row["customer_id"] not in principal.customer_ids:
                    continue
                prefs = preferences(conn, row["customer_id"])
                item = json.loads(row["data"])
                if mandate and principal.kind == "agent":
                    categories = decode(mandate)[1]["vc"]["credentialSubject"].get("categories")
                    if categories is not None and self.g.catalog.get(item["product_id"]).category not in categories:
                        continue
                if prefs.get("enabled", True) and not prefs.get("forgotten_at") and item["product_id"] not in prefs.get("excluded_products", []):
                    items.append(item)
        return detect_patterns(items)

    def hidden(self, conn, pattern):
        row = conn.execute("SELECT data FROM memory_actions WHERE id=?", (pattern["id"],)).fetchone()
        action = json.loads(row[0]) if row else {}
        return (action.get("dismissed", False) or action.get("recurring_id") is not None or
                bool(action.get("snoozed_until") and datetime.fromisoformat(action["snoozed_until"]) > audit.utcnow()))

    @staticmethod
    def public(pattern):
        return {k: v for k, v in pattern.items() if not k.startswith("_")}

    def event(self, conn, pattern, principal, action, **extra):
        audit.append(conn, f"{principal.kind}:{principal.id}", "SUGGESTION_" + action.upper(),
            f"Replenishment suggestion {action.replace('_', ' ')}.",
            data={"id": pattern["id"], "customer_id": pattern["customer_id"],
                  "product_id": pattern["product_id"], "environment": pattern["environment"], **extra})

    def suggestions(self, principal, horizon=None, mandate=None):
        patterns = self.patterns(principal, mandate)
        today = audit.utcnow().astimezone(ZoneInfo("Europe/Helsinki")).date()
        shown = []
        with self.g.db.connect(write=True) as conn:
            for pattern in patterns:
                days = horizon if horizon is not None else preferences(conn, pattern["customer_id"])["suggestion_horizon"]
                predicted = date.fromisoformat(pattern["predicted_next_need_date"])
                if not today <= predicted <= today + timedelta(days=days) or self.hidden(conn, pattern):
                    continue
                shown.append(self.public(pattern))
                self.event(conn, pattern, principal, "shown")
        return shown

    def get(self, id, principal, mandate=None):
        pattern = next((p for p in self.patterns(principal, mandate) if p["id"] == id), None)
        with self.g.db.connect() as conn:
            if pattern is None or self.hidden(conn, pattern):
                raise DomainError("SUGGESTION_NOT_FOUND", "Suggestion was not found or is hidden", 404)
        return pattern

    def purchasing_agent(self, principal, customer, requested=None):
        if principal.kind == "agent":
            if requested and requested != principal.id:
                raise DomainError("BUYER_AGENT_REQUIRED", "Use your own agent identity", 403)
            return principal.id
        if not principal.operator and principal.role not in {"buyer_admin", "buyer_approver"}:
            raise DomainError("BUYER_APPROVER_REQUIRED", "A buyer approver is required", 403)
        identity = self.g.policy.get().identities.get(requested)
        if not requested or not principal.operator and (not identity or identity.kind != "agent" or identity.organisation_id != principal.organisation_id):
            raise DomainError("BUYER_AGENT_REQUIRED", "Name a purchasing agent in your organisation", 403)
        return requested

    def need(self, pattern):
        data = deepcopy(pattern["_latest"]["need"])
        data["quantity"] = pattern["suggested_quantity"]
        today = audit.utcnow().astimezone(ZoneInfo("Europe/Helsinki")).date()
        predicted = date.fromisoformat(pattern["predicted_next_need_date"])
        data["deliver_by"] = max(today, predicted).isoformat()
        try:
            return NeedSpec.model_validate(data)
        except ValidationError as error:
            raise DomainError("VALIDATION_ERROR", "Suggested date or quantity is outside the normal lookup window; prepare nearer the need date", 422) from error

    def act(self, id, action, principal, body=None, mandate=None):
        with self.g.db.connect(write=True) as conn:
            pattern = self.get(id, principal, mandate)
            if principal.kind == "buyer_user" and principal.role == "buyer_viewer":
                raise DomainError("BUYER_APPROVER_REQUIRED", "A buyer approver is required", 403)
            result = self.public(pattern)
            if action in {"prepared", "made_recurring"}:
                agent = self.purchasing_agent(principal, pattern["customer_id"], body.buyer_agent_id if body else None)
                check_suspension(self.g.db, agent)
                self.g.mandates.authorize(conn, mandate, agent, pattern["customer_id"], products=[pattern["product_id"]])
                if action == "prepared":
                    context = PRINCIPAL.set(Principal(agent, customer_ids=(pattern["customer_id"],)))
                    try:
                        result = self.g.find_offers(self.need(pattern), f"agent:{agent}", mandate=mandate)
                        result.context["suggestion_ref"] = {"id": id, "product_id": pattern["product_id"]}
                        conn.execute("UPDATE lookups SET data=? WHERE id=?", (result.model_dump_json(), result.lookup_id))
                    finally:
                        PRINCIPAL.reset(context)
                else:
                    result = self.g.recurring.from_suggestion(pattern, self.need(pattern), agent, body, mandate)
                    conn.execute("INSERT INTO memory_actions VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                                 (id, json.dumps({"recurring_id": result["id"]})))
            else:
                state = {"dismissed": True} if action == "dismissed" else {"snoozed_until": (audit.utcnow() + timedelta(days=body.days)).isoformat()}
                conn.execute("INSERT INTO memory_actions VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data", (id, json.dumps(state)))
                result = {**result, **state}
            self.event(conn, pattern, principal, action)
            return result

    def get_preferences(self, principal, customer=None):
        customer = self.g.proposals.scope(principal, customer, admin=True)
        with self.g.db.connect() as conn:
            return {"customer_id": customer, **preferences(conn, customer)}

    def put_preferences(self, body, principal, customer=None, *, forget=False):
        customer = self.g.proposals.scope(principal, customer, admin=True)
        with self.g.db.connect(write=True) as conn:
            prior = preferences(conn, customer)
            data = body.model_dump() if body else prior
            if forget or prior.get("forgotten_at"):
                data["forgotten_at"] = prior.get("forgotten_at") or audit.utcnow().isoformat()
            conn.execute("INSERT INTO memory_preferences VALUES(?,?) ON CONFLICT(customer_id) DO UPDATE SET data=excluded.data", (customer, json.dumps(data)))
            audit.append(conn, f"human:buyer_admin:{principal.credential_ref}", "MEMORY_FORGET_REQUESTED" if forget else "MEMORY_PREFERENCES",
                         "Buyer requested purchase memory forgetting." if forget else "Buyer updated purchase memory preferences.",
                         data={"customer_id": customer, **data})
        return {"customer_id": customer, **data}


router = APIRouter(tags=["Purchase memory"])


@router.get("/v1/suggestions", response_model=list[dict], summary="Customer-scoped purchase patterns predicted within the horizon; showing is audited")
def suggestions(request: Request, horizon: int | None = Query(default=None, ge=1, le=365),
                principal: Principal = Depends(resolve_principal), agent_mandate: str | None = Header(default=None)):
    return request.app.state.gateway.memory.suggestions(principal, horizon, agent_mandate)


@router.post("/v1/suggestions/{id}/prepare", response_model=LookupResult, summary="Run a normal lookup with plans; creates no quote or order")
def prepare(id: str, request: Request, body: SuggestionAction = SuggestionAction(),
            principal: Principal = Depends(resolve_principal), agent_mandate: str | None = Header(default=None),
            idempotency_key: str | None = Header(default=None, min_length=1, max_length=200)):
    return request.app.state.gateway.memory.act(id, "prepared", principal, body, agent_mandate, command_key=idempotency_key)


@router.post("/v1/suggestions/{id}/make-recurring", response_model=dict, summary="Create a mandate-authorized recurring template using historical cadence and quantity, without buying")
def make_recurring(id: str, request: Request, body: MakeRecurring = MakeRecurring(),
                   principal: Principal = Depends(resolve_principal), agent_mandate: str | None = Header(default=None),
            idempotency_key: str | None = Header(default=None, min_length=1, max_length=200)):
    return request.app.state.gateway.memory.act(id, "made_recurring", principal, body, agent_mandate, command_key=idempotency_key)


@router.post("/v1/suggestions/{id}/dismiss", response_model=dict)
def dismiss(id: str, request: Request, principal: Principal = Depends(resolve_principal), agent_mandate: str | None = Header(default=None),
            idempotency_key: str | None = Header(default=None, min_length=1, max_length=200)):
    return request.app.state.gateway.memory.act(id, "dismissed", principal, mandate=agent_mandate, command_key=idempotency_key)


@router.post("/v1/suggestions/{id}/snooze", response_model=dict)
def snooze(id: str, body: Snooze, request: Request, principal: Principal = Depends(resolve_principal), agent_mandate: str | None = Header(default=None),
            idempotency_key: str | None = Header(default=None, min_length=1, max_length=200)):
    return request.app.state.gateway.memory.act(id, "snoozed", principal, body, agent_mandate, command_key=idempotency_key)


@router.get("/v1/buyer/memory-preferences", response_model=dict)
def get_preferences(request: Request, customer_id: str | None = None, principal: Principal = Depends(resolve_principal)):
    return request.app.state.gateway.memory.get_preferences(principal, customer_id)


@router.put("/v1/buyer/memory-preferences", response_model=dict)
def put_preferences(body: MemoryPreferences, request: Request, customer_id: str | None = None, principal: Principal = Depends(resolve_principal),
                    idempotency_key: str | None = Header(default=None, min_length=1, max_length=200)):
    return request.app.state.gateway.memory.put_preferences(body, principal, customer_id, command_key=idempotency_key)


@router.delete("/v1/buyer/memory", response_model=dict, summary="Record a forgetting request and hide this customer's memory permanently from suggestions")
def forget(request: Request, customer_id: str | None = None, principal: Principal = Depends(resolve_principal),
           idempotency_key: str | None = Header(default=None, min_length=1, max_length=200)):
    return request.app.state.gateway.memory.put_preferences(None, principal, customer_id, forget=True, command_key=idempotency_key)
