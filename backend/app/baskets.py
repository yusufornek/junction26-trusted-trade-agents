"""Agent-decomposed recipes become one evidence-bound, batched procurement request."""
import json
from decimal import Decimal
from uuid import uuid4

from . import audit
from .auth import check_owner
from .db import DomainError
from .lookup import broadened_terms, offer_for, rank, reconcile_size, reserved, warning
from .mandates import decode
from .models import (BasketLookupRequest, BasketLookupResult, IngredientManifest, LookupResult, NeedSpec, Quantity,
                     OfferCard, QuoteLineRequest)
from .quantities import pack_quantity, sales_step
from .valio import RequestCounter


def stored_basket(conn, lookup_id):
    row = conn.execute("SELECT data FROM basket_lookups WHERE id=?", (lookup_id,)).fetchone()
    return BasketLookupResult.model_validate_json(row[0], context={"stored": True}) if row else None


def bound_need(conn, lookup, product_id):
    basket = stored_basket(conn, lookup.lookup_id)
    if basket:
        if product_id not in basket.line_needs:
            raise DomainError("BASKET_LINE_MISMATCH", "Product is not a selected line of the stored basket", 422)
        return basket.line_needs[product_id]
    return line_need(lookup, product_id)


def line_need(lookup, product_id):
    """Read ingredient bindings from historical lane-D lookup records as well."""
    raw = (lookup.basket or {}).get("line_needs", {}).get(product_id)
    if lookup.basket and raw is None:
        raise DomainError("BASKET_LINE_MISMATCH", "Product is not a selected line of the stored basket", 422)
    return NeedSpec.model_validate(raw, context={"stored": True}) if raw else lookup.need


# Compatibility entry points share the same command and ownership boundary.
BasketRequest = BasketLookupRequest
BasketResult = BasketLookupResult


def find_basket_offers(gateway, request, agent_id, mandate=None):
    return gateway.baskets.find_basket_offers(request, actor=f"agent:{agent_id}", mandate=mandate).model_dump(mode="json")


class Baskets:
    def __init__(self, gateway):
        self.gateway = gateway
        self.db = gateway.db

    def get(self, lookup_id, agent_id=None):
        with self.db.connect() as conn:
            result = stored_basket(conn, lookup_id)
        if result is None:
            raise DomainError("BASKET_NOT_FOUND", "Basket lookup was not found", 404)
        return check_owner(result, agent_id, "BASKET_NOT_FOUND")

    def find_basket_offers(self, request, actor="agent:external", *, mandate=None):
        return self._run(request, actor, lambda counter: self._find(request, actor, mandate, counter))

    def _run(self, request, actor, operation):
        counter = RequestCounter()
        try:
            return operation(counter)
        except DomainError as error:
            with self.db.connect(write=True) as conn:
                audit.append(conn, actor, "BASKET_LOOKUP_FAILED", error.message,
                             data={"inputs": request.model_dump(mode="json"), "error": error.code,
                                    "live_calls": counter.live().model_dump(), "decision_calls": counter.decision_calls,
                                    **error.data})
            raise

    def _find(self, request, actor, mandate, counter):
        g = self.gateway
        agent_id = actor.removeprefix("agent:")
        with self.db.connect() as conn:
            policy, record, mandate = g.mandates.authorize(conn, mandate, agent_id, request.customer_id)
            g.governance.check_operation(conn, agent_id, "lookup", request.customer_id, request.deliver_by, effective=policy._governance)
        categories = decode(mandate)[1]["vc"]["credentialSubject"].get("categories") if mandate else None
        categories = g.governance.categories(policy._governance, categories)
        factor = Decimal(str(request.servings or request.manifest.servings)) / Decimal(str(request.manifest.base_servings or request.manifest.servings))
        data = request.manifest.model_dump()
        data["servings"] = request.servings or request.manifest.servings
        if data.get("base_servings") is not None:
            data["base_servings"] = data["servings"]
        for ingredient in data["ingredients"]:
            ingredient["quantity"]["value"] = float(Decimal(str(ingredient["quantity"]["value"])) * factor)
        scaled = IngredientManifest.model_validate(data)
        needs, sources, pools, fits, exclusions = [], [], [], [], []
        products = {}
        for ingredient in scaled.ingredients:
            q = ingredient.quantity
            unit = {"G": "KG", "ML": "L"}.get(q.unit, q.unit)
            value = q.value / 1000 if q.unit in {"G", "ML"} else q.value
            need = NeedSpec(customer_id=request.customer_id, deliver_by=request.deliver_by,
                            intent=ingredient.name, search_terms=ingredient.search_terms,
                            required_claims=ingredient.required_claims, exclude_allergens=ingredient.exclude_allergens,
                            quantity=Quantity(value=value, unit=unit))
            need, effective = g.policy.effective_need(need, policy)
            removed = {}
            pool, _ = g.catalog.search(" ".join(need.search_terms) if need.search_terms else need.intent,
                need.required_claims, need.exclude_allergens, policy.shortlist.catalog_pool,
                max_limit=policy.shortlist.catalog_pool, removed_by=removed, sources=effective, categories=categories)
            if not pool and not need.search_terms and ingredient.substitutions_allowed:
                pool, _ = g.catalog.search(" ".join(broadened_terms(need.intent)), need.required_claims,
                    need.exclude_allergens, policy.shortlist.catalog_pool, max_limit=policy.shortlist.catalog_pool,
                    mode="or", removed_by=removed, sources=effective, categories=categories)
            fit = g.decisions.ask(need, pool, counter) or {}
            # Keep unavailable fit evidence visible for human review, as in ordinary lookup.
            pool.sort(key=lambda p: -fit.get(p.id, {}).get("probability", -1))
            pool = pool[:policy.shortlist.live_max]
            products.update({p.id: p for p in pool})
            needs.append(need)
            sources.append(effective)
            pools.append(pool)
            fits.append(fit)
            exclusions.append(removed)
        ids = list(products)
        price = g.valio.fetch("price", request.customer_id, request.deliver_by, ids, counter)
        stock = g.valio.fetch("availability", request.customer_id, request.deliver_by, ids, counter)
        with self.db.connect() as conn:
            reservations = {pid: reserved(conn, pid) for pid in ids}
            autonomous_total = g.policy.autonomous_total(conn, agent_id)
        results, selected, uncovered, allocations = [], {}, [], {}
        for i, (ingredient, need, pool, fit, effective) in enumerate(zip(scaled.ingredients, needs, pools, fits, sources)):
            offers, converted = [], {}
            unit_failure = False
            for p in pool:
                candidate_need = need
                if not reconcile_size(p, price.get(p.id, {}), need.quantity.unit)[0]:
                    other = {"KG": "L", "L": "KG"}.get(need.quantity.unit)
                    density = ingredient.density_kg_per_l or p.density_kg_per_l
                    if other and reconcile_size(p, price.get(p.id, {}), other)[0]:
                        if density is None:
                            unit_failure = True
                            continue
                        quantity = need.quantity.value / density if other == "L" else need.quantity.value * density
                        candidate_need = need.model_copy(update={"quantity": Quantity(value=quantity, unit=other)})
                    else:
                        unit_failure = True
                        continue
                offer = offer_for(p, candidate_need, price.get(p.id, {"error": True}),
                    stock.get(p.id, {"error": True}), fit.get(p.id), reserved_amount=reservations[p.id], sources=effective)
                offers.append(offer)
                converted[p.id] = candidate_need
            rank(offers, policy.ranking_weights.model_dump(), policy.approval.min_fit_probability,
                 catalog_stats=g.catalog.metadata("ranking"), reject_codes=policy.reject.on)
            invalid = set(policy.reject.on) | {"PRICE_ERROR", "AVAILABILITY_ERROR", "SIZE_UNKNOWN", "UNIT_MISMATCH"}
            chosen = next((o for o in offers if not any(w.code in invalid for w in o.warnings)), None)
            results.append({"ingredient_index": i, "name": ingredient.name, "offers": offers,
                            "chosen_line": chosen.product_id if chosen else None, "exclusion_counts": exclusions[i]})
            if chosen is None:
                uncovered.append({"ingredient_index": i, "name": ingredient.name,
                                  "reason": "unit cannot be converted" if unit_failure else "no compliant product"})
                continue
            pid = chosen.product_id
            selected.setdefault(pid, []).append((i, converted[pid], chosen))
        lines, line_needs, coverages = [], {}, []
        for pid, entries in selected.items():
            # Sum exact fractions of sales packs BEFORE rounding, including cross-dimensional ingredients.
            packs = sum((Decimal(str(n.quantity.value)) / Decimal(str(o.size_per_sales_unit))
                         for _, n, o in entries), Decimal(0))
            lots = float(pack_quantity(1, requested=packs, step=sales_step(products[pid])))
            first = entries[0][1]
            first_size = entries[0][2].size_per_sales_unit
            merged = first.model_copy(update={
                "quantity": Quantity(value=float(packs * Decimal(str(first_size))), unit=first.quantity.unit),
                "required_claims": sorted({c for _, n, _ in entries for c in n.required_claims}),
                "exclude_allergens": sorted({c for _, n, _ in entries for c in n.exclude_allergens})})
            # Store only request constraints; standing rules are re-unioned at every later step.
            raw_merged = merged.model_copy(update={
                "required_claims": sorted({c for i, _, _ in entries for c in scaled.ingredients[i].required_claims}),
                "exclude_allergens": sorted({c for i, _, _ in entries for c in scaled.ingredients[i].exclude_allergens})})
            effective_need, effective = g.policy.effective_need(raw_merged, policy)
            # A shared SKU must prove the union of ALL ingredient constraints.
            fit = {"probability": min(o.fit for _, _, o in entries if o.fit is not None), "question": entries[0][2].fit_question} if all(o.fit is not None for _, _, o in entries) else None
            line = offer_for(products[pid], effective_need, price[pid], stock[pid], fit, sales_quantity=lots,
                             reserved_amount=reservations[pid], sources=effective)
            lines.append(line)
            line_needs[pid] = raw_merged
            for i, n, o in entries:
                fraction = line.availability.coverage
                allocations[i] = (pid, fraction)
                results[i].update(purchasable_quantity=lots, overbuy=line.overbuy, coverage=fraction)
                if fraction < 1:
                    uncovered.append({"ingredient_index": i, "name": scaled.ingredients[i].name, "reason": "insufficient on-time stock"})
        for i, ingredient in enumerate(scaled.ingredients):
            pid, fraction = allocations.get(i, (None, 0))
            coverages.append({"ingredient_index": i, "name": ingredient.name, "requested": ingredient.quantity,
                              "covered": fraction, "product_id": pid, "coverage": fraction,
                              "quantity": ingredient.quantity, "purchasable_quantity": results[i].get("purchasable_quantity"),
                              "overbuy": results[i].get("overbuy")})
        total = float(sum((Decimal(str(o.total)) for o in lines), Decimal(0)))
        warnings = [warning("PARTIAL_BASKET")] if uncovered else []
        preview = g.policy.evaluate(total, lines, agent_id=agent_id, customer_id=request.customer_id,
                                    autonomous_total=autonomous_total, policy=policy, warnings=warnings)
        lookup_id = str(uuid4())
        result = BasketLookupResult(basket_lookup_id=lookup_id, agent_id=agent_id, customer_id=request.customer_id,
            deliver_by=request.deliver_by, scaled_manifest=scaled, ingredients=results, line_needs=line_needs,
            basket={"lines": lines, "total": total, "coverage_by_ingredient": coverages, "uncovered": uncovered,
                    "policy_preview": preview}, live_calls=counter.live(), decision_calls=counter.decision_calls)
        # The common lookup binding keeps signatures, mandate revocation, timelines and evidence exports reusable.
        aggregate_need = NeedSpec(customer_id=request.customer_id, deliver_by=request.deliver_by,
            intent=scaled.recipe_name, quantity={"value": 1, "unit": "SALES_UNIT"})
        aggregate = LookupResult(lookup_id=lookup_id, agent_id=agent_id, mandate=record, need=aggregate_need,
            offers=lines, excluded_count=0, live_calls=counter.live(), decision_calls=counter.decision_calls, warnings=warnings)
        from .foundation import procurement_context, CORRELATION
        with self.db.connect(write=True) as conn:
            _, aggregate.mandate, _ = g.mandates.authorize(conn, mandate, agent_id, request.customer_id, products=ids)
            current = g.governance.check_operation(conn, agent_id, "lookup", request.customer_id, request.deliver_by, products=[products[pid] for pid in ids])
            if current.version_id != policy._governance.version_id:
                raise DomainError("POLICY_CHANGED", "Policy changed while looking up this basket; submit a fresh lookup", 409)
            g.governance.snapshot(conn, current)
            aggregate.policy_version_id = result.policy_version_id = current.version_id
            _, effective = g.policy.effective_need(aggregate_need, policy)
            aggregate.context = procurement_context(g.policy, request.customer_id, lookup_id, effective, record)
            aggregate.context["governance_policy_version_id"] = current.version_id
            result.context = aggregate.context
            aggregate.correlation_id = CORRELATION.get()
            g.mandates.bind(conn, "lookup", lookup_id, mandate)
            conn.execute("INSERT INTO lookups VALUES(?,?)", (lookup_id, aggregate.model_dump_json()))
            conn.execute("INSERT INTO lookup_offers VALUES(?,?)", (lookup_id, json.dumps([o.model_dump(mode="json") for o in lines])))
            conn.execute("INSERT INTO basket_lookups VALUES(?,?)", (lookup_id, result.model_dump_json()))
            audit.append(conn, actor, "LOOKUP", f"Recipe basket: {len(lines)} distinct SKUs; {len(uncovered)} uncovered ingredients.",
                         data={"lookup_id": lookup_id, "inputs": request.model_dump(mode="json"),
                               "live_calls": counter.live().model_dump(), "decision_calls": counter.decision_calls})
        return result
