"""Shared procurement evidence for the brief's recipe, deadline and safety scenarios."""
from datetime import timedelta
from fractions import Fraction

from .db import DomainError


NUTRIENTS = {"max_salt_g": ("salt_g", False), "max_sugar_g": ("sugar_g", False),
             "max_saturated_fat_g": ("saturated_fat_g", False), "min_protein_g": ("protein_g", True)}


def viable_alternative(offer):
    blocked = {"PRICE_ERROR", "AVAILABILITY_ERROR", "SALES_BLOCK", "SIZE_UNKNOWN", "UNIT_MISMATCH"}
    return offer.arrives_in_time and offer.unit_price is not None and not any(w.code in blocked for w in offer.warnings)


def nutrition_facts(synkka):
    """Only explicit per-100g/ml gram facts qualify; no inference from claims."""
    facts = dict(synkka.get("nutritionPer100") or {})
    aliases = {"SALT": "salt_g", "SUGARS": "sugar_g", "SUGAR": "sugar_g",
               "SATURATED_FAT": "saturated_fat_g", "SATURATEDFAT": "saturated_fat_g", "PROTEIN": "protein_g"}
    aliases.update(SALTEQ="salt_g", FASAT="saturated_fat_g", **{"SUGAR-": "sugar_g", "PRO-": "protein_g"})
    for item in synkka.get("nutritionalContent") or []:
        key = aliases.get(str(item.get("id", "")).upper())
        try:
            basis = float(item.get("basisQuantity"))
            value = float(item.get("value"))
            factor = {"GRM": 1, "G": 1, "MGM": .001, "MC": .000001}.get(item.get("unit"))
            # Approximate/less-than labels are not exact lower-bound evidence for protein.
            precision = item.get("unitPrecision")
            if key and basis > 0 and factor and item.get("basisQuantityUnit") in {"GRM", "MLT"} and precision not in {"LESS_THAN", "GREATER_THAN"}:
                facts[key] = value * factor * 100 / basis
        except (ValueError, TypeError):
            pass
    for group in synkka.get("classifications") or []:
        if group.get("name") != "nutritionFact":
            continue
        for item in group.get("values") or []:
            key = aliases.get(str(item.get("id", "")).upper())
            basis = str(item.get("basis", group.get("basis", ""))).lower().replace(" ", "")
            unit = str(item.get("unit", "")).upper()
            if key and basis in {"100g", "100ml"} and unit in {"G", "GRM"}:
                facts[key] = item.get("value")
    result = {}
    for key, value in facts.items():
        try:
            n = float(value)
            if n >= 0 and n < float("inf"):
                result[key] = n
        except (TypeError, ValueError):
            pass
    return result


def nutrition_failures(product, limits):
    failures = []
    for rule, (nutrient, minimum) in NUTRIENTS.items():
        limit = (limits or {}).get(rule)
        if limit is None:
            continue
        value = product.nutrition_per_100.get(nutrient)
        if value is None:
            failures.append(("nutrient_limits", rule, "missing nutrition data per 100 g / 100 ml"))
        elif (value < limit if minimum else value > limit):
            failures.append(("nutrient_limits", rule, f"{nutrient} {value:g} g violates limit {limit:g} g per 100 g / 100 ml"))
    return failures


def filter_nutrition(pool, effective, removed):
    kept = []
    for product in pool:
        failures = nutrition_failures(product, effective.nutrient_limits)
        for _, rule, reason in failures:
            key = f"nutrient_limits.{rule} (customer_policy; {reason})"
            removed[key] = removed.get(key, 0) + 1
        if not failures:
            kept.append(product)
    return kept


def stock_to_buy(ingredient, needed_value=None):
    """Exact base-unit deficit; scaling applies to need, never physical on-hand inventory."""
    q = ingredient.quantity
    scales = {"G": ("KG", Fraction(1, 1000)), "ML": ("L", Fraction(1, 1000))}
    unit, scale = scales.get(q.unit, (q.unit, Fraction(1)))
    needed = (Fraction(str(q.value)) if needed_value is None else needed_value) * scale
    hand = ingredient.on_hand
    available = Fraction(0)
    if hand:
        hand_unit, hand_scale = scales.get(hand.unit, (hand.unit, Fraction(1)))
        available = Fraction(str(hand.value)) * hand_scale
        if hand_unit != unit:
            density = ingredient.density_kg_per_l
            if not density or {hand_unit, unit} != {"KG", "L"}:
                raise DomainError("ON_HAND_UNIT_MISMATCH", "On-hand units require compatible units or explicit density", 422)
            available = available * Fraction(str(density)) if unit == "KG" else available / Fraction(str(density))
    return max(Fraction(0), needed - available), unit, needed, available


def trade_off(original, alternative, need, catalog, prices, stock=None):
    """One comparison function for substitutions, deadline alternatives and stock-out alerts."""
    from .lookup import reconcile_size, day
    from .catalog import number
    source = catalog.get(original)
    target = catalog.get(alternative.product_id)
    baseline = prices.get(original, {})
    size = reconcile_size(source, baseline, need.quantity.unit)[0]
    price = number((baseline.get("catalogPrice") or {}).get("value"))
    currency = (baseline.get("catalogPrice") or {}).get("currency") or baseline.get("_currency")
    old_cost = need.quantity.value * price / size if price is not None and price > 0 and size and not baseline.get("error") and currency == alternative.currency else None
    per_required_unit = alternative.unit_price if need.quantity.unit == "SALES_UNIT" else alternative.price_per_l_or_kg
    new_cost = need.quantity.value * per_required_unit if per_required_unit is not None else None
    dates = [day(x.get("availableDate")) for x in (stock or {}).get(original, {}).get("scheduleLines", [])]
    dates = [d for d in dates if d]
    old_date = min(dates) if dates else None
    new_date = alternative.availability.available_date
    return {"price_difference_per_required_quantity": round(new_cost - old_cost, 6) if old_cost is not None and new_cost is not None else None,
            "required_quantity": need.quantity.model_dump(), "currency": alternative.currency,
            "pack_change": {"from": size, "to": alternative.size_per_sales_unit},
            "unit_change": {"from": source.sales_unit, "to": alternative.sales_unit},
            "allergen_differences": {"removed": sorted(set(source.allergens_contains) - set(target.allergens_contains)),
                                     "added": sorted(set(target.allergens_contains) - set(source.allergens_contains)),
                                     "from": source.allergen_status, "to": target.allergen_status},
            "claim_differences": {"removed": sorted(set(source.claims) - set(target.claims)), "added": sorted(set(target.claims) - set(source.claims))},
            "delivery_date_difference": {"from": old_date.isoformat() if old_date else None,
                                         "to": new_date.isoformat() if new_date else None,
                                         "days": (new_date-old_date).days if new_date and old_date else None},
            "explanation": f"{target.name_en} meets the recorded function and constraints; compare price, pack and delivery evidence before approving."}


def annotate_timing(offers, need, catalog, prices, stock):
    from .lookup import timestamp, day
    from . import audit
    for offer in offers:
        raw = stock.get(offer.product_id, {})
        now = timestamp(raw.get("_fetched_at")) or audit.utcnow()
        now += timedelta(seconds=raw.get("_cache_age", 0))
        cutoff = offer.availability.closing_time
        possible_dates = [day(x.get("availableDate")) for x in raw.get("scheduleLines", [])
                          if not x.get("salesBlock") and (raw.get("_stock_semantics") == "unlimited" or
                          isinstance(x.get("availableAmount"), (float, int)) and x["availableAmount"] > 0)]
        possible_dates = [d for d in possible_dates if d]
        offer.arrives_in_time = bool(offer.availability.enough and (cutoff is None or cutoff >= now))
        if offer.arrives_in_time:
            offer.cannot_source_in_time = None
        if raw.get("salesBlock") or raw.get("error") or cutoff and cutoff < now:
            possible_dates = []
        if not offer.arrives_in_time:
            offer.cannot_source_in_time = {"earliest_possible_date": min(possible_dates).isoformat() if possible_dates else None,
                "in_time_alternatives": [], "reason": "cut-off missed; next supplier date unknown" if cutoff and cutoff < now else "insufficient stock by deadline"}
    for offer in offers:
        if offer.arrives_in_time:
            continue
        alternatives = []
        if need.function:
            for candidate in offers:
                if candidate.product_id != offer.product_id and viable_alternative(candidate):
                    alternatives.append({"product_id": candidate.product_id, "requires_approval": True,
                        "trade_off": trade_off(offer.product_id, candidate, need, catalog, prices, stock)})
        offer.cannot_source_in_time["in_time_alternatives"] = alternatives


def annotate_substitution(offer, need, catalog, prices, stock=None):
    if offer.substitution:
        offer.trade_off = trade_off(offer.substitution.for_product, offer, need, catalog, prices, stock)


def quote_baselines(db, lookup, product_ids):
    from .baskets import bound_need
    ids = []
    with db.connect() as conn:
        for pid in product_ids:
            need = bound_need(conn, lookup, pid)
            if need.substitute_for:
                ids.append(need.substitute_for.product_id)
    return ids


def quote_annotations(offer, need, catalog, prices, stock):
    annotate_substitution(offer, need, catalog, prices, stock)
    annotate_timing([offer], need, catalog, prices, stock)


def basket_line_timing(line, need, entries, ingredients, catalog, prices, stock, reservations):
    """Recheck the combined demand of shared SKUs against already-batched alternatives."""
    from .lookup import offer_for
    alternatives = []
    if need.function:
        eligible = None
        evidence = {}
        for index, _, _ in entries:
            offers = {o.product_id: o for o in ingredients[index]["offers"]}
            eligible = set(offers) if eligible is None else eligible.intersection(offers)
            evidence[index] = offers
        for pid in sorted(eligible or []):
            if pid == line.product_id:
                continue
            fits = [evidence[index][pid].fit for index, _, _ in entries]
            fit = {"probability": min(fits), "question": "All ingredient functional needs passed"} if all(f is not None for f in fits) else None
            alternatives.append(offer_for(catalog.get(pid), need, prices[pid], stock[pid], fit,
                reserved_amount=reservations.get(pid, 0)))
    annotate_timing([line, *alternatives], need, catalog, prices, stock)


def refresh_timing(line, stock, deliver_by):
    # Older signed quote shapes must remain untouched on read/refresh.
    if line.arrives_in_time is not None:
        from types import SimpleNamespace
        annotate_timing([line], SimpleNamespace(function=None, deliver_by=deliver_by), None, {}, {line.product_id: stock})


def enrich_coverage(coverages, manifest):
    for coverage, ingredient in zip(coverages, manifest.ingredients):
        deficit, unit, needed, hand = stock_to_buy(ingredient)
        total_coverage = min(1.0, float((min(hand, needed) + deficit * Fraction(str(coverage["covered"]))) / needed))
        coverage.update(covered=total_coverage, coverage=total_coverage, needed={"value": float(needed), "unit": unit},
                        on_hand={"value": float(hand), "unit": unit},
                        to_buy={"value": float(deficit), "unit": unit})
    if manifest.focus_ingredient:
        coverages.sort(key=lambda c: c["name"].casefold() != manifest.focus_ingredient.casefold())

