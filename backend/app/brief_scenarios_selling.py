"""Catalog campaigns derived privately from existing purchase memory and buyer consent."""
import json
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from . import audit
from .auth import resolve_principal
from .db import DomainError
from .models import Model, InputText, Product
from .proposals import ProposalRequest
from .purchase_memory import preferences
from .brief_scenarios import NUTRIENTS, nutrition_failures
from .catalog import constraint_failures
from .valio import RequestCounter


class SuggestCampaign(Model):
    customer_id: InputText
    campaign_id: InputText
    max_products: int = Field(default=5, ge=1, le=10)


def suggest(gateway, body, principal):
    if principal.kind != "supplier_agent":
        raise DomainError("SUPPLIER_REQUIRED", "Only a supplier agent may generate campaigns", 403)
    gateway.limiter.consume(principal.id, "mutation")
    with gateway.db.connect() as conn:
        prefs = preferences(conn, body.customer_id)
        if not prefs.get("enabled", True) or prefs.get("forgotten_at"):
            return {"delivery_count": 0, "reason": "no eligible products"}
        bought = [json.loads(r[0]) for r in conn.execute("SELECT data FROM purchase_memory WHERE customer_id=?", (body.customer_id,))]
        bought = [r for r in bought if r.get("supplier_id", "valio") == "valio"]
        seen = {r["product_id"] for r in bought}
        categories = set()
        for pid in seen:
            try:
                categories.add(gateway.catalog.get(pid).category)
            except DomainError:
                pass
        products = [Product.model_validate_json(r[0]) for r in conn.execute(
            "SELECT data FROM products WHERE category IN (SELECT value FROM json_each(?)) "
            "AND id NOT IN (SELECT value FROM json_each(?)) ORDER BY id",
            (json.dumps(sorted(c for c in categories if c)), json.dumps(sorted(seen))))]
    customer = gateway.policy.get().customers.get(body.customer_id)
    constraints = {k: getattr(customer, k) for k in NUTRIENTS if customer and getattr(customer, k) is not None}
    candidates = [p for p in products if p.id not in seen and p.id not in prefs.get("excluded_products", [])
                  and p.category in categories and gateway.proposals.relevant(p, gateway.proposals.preferences(body.customer_id))
                  and not constraint_failures(p, customer.required_claims if customer else [], customer.exclude_allergens if customer else [])
                  and not nutrition_failures(p, constraints)][:body.max_products]
    if not candidates:
        return {"delivery_count": 0, "reason": "no eligible products"}
    counter = RequestCounter()
    now = audit.utcnow()
    prices = gateway.suppliers.get("valio").fetch("price", body.customer_id, now.date()+timedelta(days=1), [p.id for p in candidates], counter)
    evidence = []
    for p in candidates:
        price = prices.get(p.id, {})
        fact = price.get("catalogPrice") or {}
        if not price.get("error") and fact.get("currency") == "EUR" and fact.get("value", 0) > 0:
            evidence.append({"product_id": p.id, "unit_price": fact["value"], "currency": "EUR", "fetched_at": now})
    if not evidence:
        return {"delivery_count": 0, "reason": "no current price evidence"}
    return gateway.proposals.submit(ProposalRequest(campaign_id=body.campaign_id, products=[e["product_id"] for e in evidence],
        rationale="New products in categories this buyer purchases, compliant with recorded constraints.",
        price_evidence=evidence, valid_until=now+timedelta(days=1), target={"customer_ids": [body.customer_id]}), principal)


router = APIRouter()


@router.post("/v1/proposals/suggest", response_model=dict)
def suggest_campaign(body: SuggestCampaign, request: Request, principal=Depends(resolve_principal)):
    return suggest(request.app.state.gateway, body, principal)
