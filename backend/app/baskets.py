"""Ingredient baskets reuse lookup screening, supplier batches and order policy."""
import json
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, Request
from pydantic import Field

from . import audit
from .auth import resolve_principal
from .db import DomainError
from .foundation import procurement_context
from .models import (Model, RequestScope, NeedSpec, LookupResult, FulfilmentPlan,
                     PlanLine, CoveredQuantity, Quantity, Warning)
from .quantities import sales_step
from .valio import RequestCounter


class IngredientQuantity(Model):
    value: float = Field(gt=0, le=1_000_000, strict=True)
    unit: Literal["G", "KG", "ML", "L", "SALES_UNIT"]


class Ingredient(Model):
    name: str = Field(min_length=1)
    quantity: IngredientQuantity
    search_terms: list[str] | None = None
    required_claims: list[str] = Field(default_factory=list)
    exclude_allergens: list[str] = Field(default_factory=list)
    substitutions_allowed: bool = False
    density_kg_per_l: float | None = Field(default=None, gt=0, strict=True)


class IngredientManifest(Model):
    version: Literal[1] = 1
    recipe_name: str = Field(min_length=1)
    servings: float = Field(gt=0, le=1_000_000, strict=True)
    base_servings: float = Field(default=1, gt=0, strict=True)
    ingredients: list[Ingredient] = Field(min_length=1, max_length=10)
    yield_note: str | None = None


class BasketRequest(RequestScope):
    manifest: IngredientManifest


class BasketResult(Model):
    basket_lookup_id: str
    agent_id: str
    context: dict
    basket: dict
    live_calls: dict[str, int]
    scaled_manifest: dict
    ingredients: list[dict]


def line_need(lookup, product_id):
    raw = (lookup.basket or {}).get("line_needs", {}).get(product_id)
    return NeedSpec.model_validate(raw, context={"stored": True}) if raw else lookup.need


def find_basket_offers(gateway, request, agent_id, mandate=None):
    from .commands import charge
    charge(gateway, agent_id, "lookup")
    try:
        return _find_basket_offers(gateway, request, agent_id, mandate)
    except DomainError as error:
        from .commands import failure_audit
        failure_audit(gateway, "gateway.find_offers", {}, agent_id, error)
        raise


def _find_basket_offers(gateway, request, agent_id, mandate=None):
    gateway.orders.expire()
    actor = f"agent:{agent_id}"
    with gateway.db.connect() as conn:
        policy, record, mandate = gateway.mandates.authorize(conn, mandate, agent_id, request.customer_id)
    prepared, counters, scaled = [], [], []
    scale = Decimal(str(request.manifest.servings)) / Decimal(str(request.manifest.base_servings))
    for ingredient in request.manifest.ingredients:
        amount = Decimal(str(ingredient.quantity.value)) * scale
        unit = ingredient.quantity.unit
        if unit in {"G", "ML"}:
            amount /= 1000
            unit = {"G": "KG", "ML": "L"}[unit]
        need = NeedSpec(customer_id=request.customer_id, deliver_by=request.deliver_by, intent=ingredient.name,
            search_terms=ingredient.search_terms, required_claims=ingredient.required_claims,
            exclude_allergens=ingredient.exclude_allergens, quantity=Quantity(value=float(amount), unit=unit))
        counter = RequestCounter()
        prepared.append(gateway.lookup.prepare(need, actor, mandate, counter))
        counters.append(counter)
        scaled.append({**ingredient.model_dump(mode="json"), "quantity": need.quantity.model_dump()})
    ids = list(dict.fromkeys(product.id for item in prepared for product in item[6]))
    batch = RequestCounter()
    evidence = {endpoint: gateway.valio.fetch(endpoint, request.customer_id, request.deliver_by, ids, batch)
                for endpoint in ("price", "availability")}
    counters[0].price, counters[0].availability = batch.price, batch.availability
    results = [gateway.lookup._find_offers(item[1], actor, mandate, counter, prepared=item, evidence=evidence)
               for item, counter in zip(prepared, counters)]
    chosen, demands, line_needs, coverage, uncovered, ingredients = {}, {}, {}, [], [], []
    for index, (ingredient, result) in enumerate(zip(request.manifest.ingredients, results)):
        need = result.need
        eligible = [o for o in result.offers if o.size_per_sales_unit and o.unit_price is not None and o.fit is not None
                    and o.fit >= policy.approval.min_fit_probability and o.currency == "EUR"
                    and not any(w.code in set(policy.reject.on) | {"SIZE_UNKNOWN", "UNIT_MISMATCH", "AVAILABILITY_ERROR", "PRICE_ERROR"} for w in o.warnings)]
        reason = "no compliant product"
        if not eligible and any(w.code == "SIZE_UNKNOWN" for o in result.offers for w in o.warnings):
            reason = "unit cannot be converted"
            # Only explicit density evidence permits conversion between mass and volume.
            density = ingredient.density_kg_per_l
            if density and need.quantity.unit in {"KG", "L"}:
                converted = need.model_copy(update={"quantity": Quantity(value=need.quantity.value / density if need.quantity.unit == "KG" else need.quantity.value * density,
                                                                        unit="L" if need.quantity.unit == "KG" else "KG")})
                counter = RequestCounter()
                item = gateway.lookup.prepare(converted, actor, mandate, counter)
                converted_result = gateway.lookup._find_offers(converted, actor, mandate, counter, prepared=item, evidence=evidence)
                eligible = [o for o in converted_result.offers if o.size_per_sales_unit and o.unit_price is not None and o.fit is not None
                            and o.fit >= policy.approval.min_fit_probability and not any(w.code in policy.reject.on for w in o.warnings)]
                if eligible:
                    result, need = converted_result, converted
        selected = eligible[0] if eligible else None
        ingredients.append({"name": ingredient.name, "offers": [o.model_dump(mode="json") for o in result.offers],
                            "chosen_line": selected.product_id if selected else None})
        if selected is None:
            uncovered.append({"ingredient_index": index, "name": ingredient.name, "reason": reason})
            coverage.append({"ingredient_index": index, "coverage": 0, "quantity": need.quantity.model_dump(), "overbuy": None})
            continue
        pid = selected.product_id
        chosen.setdefault(pid, selected.model_copy(deep=True))
        demands[pid] = demands.get(pid, Decimal(0)) + Decimal(str(need.quantity.value)) / Decimal(str(selected.size_per_sales_unit))
        current = line_needs.setdefault(pid, need.model_copy(deep=True))
        current.required_claims = sorted(set(current.required_claims + need.required_claims))
        current.exclude_allergens = sorted(set(current.exclude_allergens + need.exclude_allergens))
        coverage.append({"ingredient_index": index, "coverage": selected.availability.coverage,
                         "quantity": need.quantity.model_dump(), "purchasable_quantity": selected.units_needed,
                         "overbuy": selected.overbuy.model_dump(mode="json") if selected.overbuy else None})
        if selected.availability.coverage < 1:
            uncovered.append({"ingredient_index": index, "name": ingredient.name, "reason": "partial stock"})
    lines = []
    for pid, line in chosen.items():
        step = sales_step(line)
        from decimal import ROUND_CEILING
        units = (demands[pid] / step).to_integral_value(rounding=ROUND_CEILING) * step
        line.units_needed = float(units)
        line.total = line.purchase_total = float((units * Decimal(str(line.unit_price))).quantize(Decimal("0.01")))
        if line.availability.availability_known and units > Decimal(str(line.availability.available_amount)):
            uncovered.append({"product_id": pid, "reason": "partial stock after combining ingredients"})
        lines.append(line)
    warnings = [Warning(code="PARTIAL_STOCK", text="Ingredient basket is incomplete; review uncovered ingredients.")] if uncovered else []
    total = round(sum(line.total for line in lines), 2)
    with gateway.db.connect(write=True) as conn:
        policy, record, mandate = gateway.mandates.authorize(conn, mandate, agent_id, request.customer_id, products=chosen)
        preview = gateway.policy.evaluate(total, lines, agent_id=agent_id, customer_id=request.customer_id,
            autonomous_total=gateway.policy.autonomous_total(conn, agent_id), policy=policy, warnings=warnings)
        lookup_id = str(uuid4())
        synthetic = NeedSpec(customer_id=request.customer_id, deliver_by=request.deliver_by,
            intent=request.manifest.recipe_name, quantity=Quantity(value=1, unit="SALES_UNIT"))
        _, effective = gateway.policy.effective_need(synthetic, policy)
        basket = {"lines": [line.model_dump(mode="json") for line in lines], "total": total,
                  "coverage_by_ingredient": coverage, "uncovered": uncovered, "policy_preview": preview.model_dump(mode="json"),
                  "line_needs": {pid: need.model_dump(mode="json") for pid, need in line_needs.items()},
                  "scaled_manifest": {**request.manifest.model_dump(mode="json"), "ingredients": scaled}, "ingredients": ingredients}
        plan = FulfilmentPlan(kind="partial" if uncovered else "split", lines=[PlanLine(product_id=line.product_id, units=line.units_needed,
            covers=CoveredQuantity(value=line.units_needed, unit="SALES_UNIT")) for line in lines], total=total,
            coverage=CoveredQuantity(value=sum(line.units_needed for line in lines), unit="SALES_UNIT"),
            uncovered=CoveredQuantity(value=len(uncovered), unit="SALES_UNIT"), warnings=warnings, policy_preview=preview) if lines else None
        result = LookupResult(lookup_id=lookup_id, agent_id=agent_id, need=synthetic, effective_constraints=effective,
            offers=lines, plans=[plan] if lines else [], excluded_count=sum(r.excluded_count for r in results),
            live_calls=batch.live(), decision_calls=sum(counter.decision_calls for counter in counters), mandate=record, basket=basket,
            context=procurement_context(gateway.policy, request.customer_id, lookup_id, effective, record))
        gateway.mandates.bind(conn, "lookup", lookup_id, mandate)
        conn.execute("INSERT INTO lookups VALUES(?,?)", (lookup_id, result.model_dump_json()))
        conn.execute("INSERT INTO lookup_offers VALUES(?,?)", (lookup_id, json.dumps(basket["lines"])))
        audit.append(conn, actor, "LOOKUP", "Prepared an ingredient basket.", data={"lookup_id": lookup_id,
            "live_calls": batch.live().model_dump(), "decision_calls": result.decision_calls})
    return {"basket_lookup_id": lookup_id, "agent_id": agent_id, "context": result.context, "basket": basket,
            "live_calls": batch.live().model_dump(), "scaled_manifest": basket["scaled_manifest"], "ingredients": ingredients}


router = APIRouter()


@router.post("/v1/basket-lookups", response_model=BasketResult, summary="Find a policy-checked ingredient basket with one batched price and stock lookup")
def basket_lookup(request: Request, body: BasketRequest, principal=Depends(resolve_principal), agent_mandate: str | None = Header(default=None)):
    return find_basket_offers(request.app.state.gateway, body, principal.id, agent_mandate)
