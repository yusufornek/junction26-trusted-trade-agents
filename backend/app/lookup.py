import re
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_CEILING
from uuid import uuid4

from . import audit
from .catalog import number
from .db import DomainError
from .models import Availability, LookupResult, OfferCard, Provenance, Warning
from .valio import RequestCounter

WARNING_TEXT = {
    "PARTIAL_STOCK": "On-time available stock covers only part of the requested quantity.",
    "DATE_SHIFT": "The upstream API returned a delivery date after the requested deadline.",
    "CUTOFF_PASSED": "Upstream closingTime is before timeNow; the cutoff meaning needs human review.",
    "SALES_BLOCK": "The product or a schedule line is blocked for sale.",
    "UNKNOWN_PRODUCT": "No schedule lines were returned; the upstream API does not flag unknown IDs.",
    "PRICE_ERROR": "A valid net catalog price could not be obtained.",
    "SIZE_UNKNOWN": "Sales-unit content cannot be converted to the requested unit.",
    "NO_FIT_SCORE": "Decisions fit probability is unavailable; a human must review suitability.",
    "AVAILABILITY_ERROR": "Availability could not be verified with the upstream API.",
}


def warning(code):
    return Warning(code=code, text=WARNING_TEXT[code])


def timestamp(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def day(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def round_quantity(quantity, product):
    step = Decimal(str(product.allowed_lot_size if product.sales_unit == "KG" else 1))
    return float((Decimal(str(quantity)) / step).to_integral_value(rounding=ROUND_CEILING) * step)


def size_for(product, price, requested_unit):
    if requested_unit == "SALES_UNIT":
        return 1.0, "sales_unit"
    net = number((price.get("catalogPrice") or {}).get("value"))
    comparison = number((price.get("comparisonPrice") or {}).get("value"))
    if not price.get("error") and price.get("comparisonUnit") == requested_unit and net and net > 0 and comparison and comparison > 0:
        return net / comparison, "price_ratio"
    if product.sales_unit == requested_unit == "KG":
        return 1.0, "base_unit"
    multiplier = product.units.get(product.sales_unit, 1 if product.sales_unit == product.base_unit else None)
    if multiplier is None or multiplier <= 0:
        return None, None
    content = product.base_volume_l if requested_unit == "L" else product.base_net_weight_kg
    if content and content > 0:
        return content * multiplier, "catalog"
    # Prefer English, then Finnish names. Match 10 l, 5 dl, 1L, 500 g, 1,5 kg.
    pattern = r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s*(kg|dl|cl|ml|l|g)\b"
    for name in (product.name_en, product.name_fi):
        for match in re.finditer(pattern, name, re.IGNORECASE):
            value, unit = float(match[1].replace(",", ".")), match[2].lower()
            factors = {"l": 1, "dl": 0.1, "cl": 0.01, "ml": 0.001} if requested_unit == "L" else {"kg": 1, "g": 0.001}
            if unit in factors and value > 0:
                return value * factors[unit] * multiplier, "name_parse"
    return None, None


def reserved(conn, product_id, exclude_order=None):
    return conn.execute(
        "SELECT coalesce(sum(quantity),0) FROM reservations WHERE product_id=? AND order_id!=?",
        (product_id, exclude_order or "")).fetchone()[0]


def availability_for(raw, deliver_by, quantity, reserved_amount=0):
    codes = []
    now = timestamp(raw.get("_fetched_at")) or audit.utcnow()
    cutoff = timestamp(raw.get("closingTime"))
    lines = raw.get("scheduleLines") or []
    blocked = raw.get("salesBlock") or any(line.get("salesBlock") for line in lines)
    if blocked:
        codes.append("SALES_BLOCK")
    if raw.get("error"):
        codes.append("AVAILABILITY_ERROR")
    elif not lines:
        codes.append("UNKNOWN_PRODUCT")
    dates = [d for line in lines if not line.get("salesBlock") and (d := day(line.get("availableDate")))]
    if any(d > deliver_by for d in dates):
        codes.append("DATE_SHIFT")
    if cutoff and cutoff < now:
        codes.append("CUTOFF_PASSED")
    amount = 0.0 if raw.get("salesBlock") or raw.get("error") else sum(
        max(0, number(line.get("availableAmount")) or 0) for line in lines
        if not line.get("salesBlock") and (d := day(line.get("availableDate"))) and d <= deliver_by)
    if raw.get("logisticModel") == "Stock":
        amount = max(0, amount - reserved_amount)
    coverage = min(1.0, amount / quantity) if quantity and quantity > 0 else 0.0
    if quantity and amount < quantity:
        codes.append("PARTIAL_STOCK")
    return Availability(available_amount=amount, logistic_model=raw.get("logisticModel"),
                        closing_time=cutoff, available_date=min(dates) if dates else None,
                        enough=quantity is not None and amount >= quantity, coverage=coverage), [warning(c) for c in codes]


def offer_for(product, need, price, stock, fit=None, *, sales_quantity=None, reserved_amount=0):
    size, source = size_for(product, price, need.quantity.unit)
    quantity = sales_quantity if sales_quantity is not None else (
        round_quantity(need.quantity.value / size, product) if size else None)
    available, warnings = availability_for(stock, need.deliver_by, quantity, reserved_amount)
    catalog = price.get("catalogPrice") or {}
    net = number(catalog.get("value"))
    valid_from, valid_to = timestamp(catalog.get("validFrom")), timestamp(catalog.get("validTo"))
    fetched = timestamp(price.get("_fetched_at")) or audit.utcnow()
    if price.get("error") or net is None or net < 0 or valid_to is None or (valid_from and valid_from > fetched) or (valid_to and valid_to <= fetched):
        warnings.append(warning("PRICE_ERROR"))
        net = None
    if not size:
        warnings.append(warning("SIZE_UNKNOWN"))
    if not fit:
        warnings.append(warning("NO_FIT_SCORE"))
    matched = [f"{c} from catalog nutritionalClaim" for c in need.required_claims if c in product.claims]
    matched += [f"{a} absent from catalog allergen CONTAINS" for a in need.exclude_allergens if a not in product.allergens_contains]
    return OfferCard(
        product_id=product.id, name=product.name_en, sales_unit=product.sales_unit,
        size_per_sales_unit=size, size_unit=need.quantity.unit, size_source=source, units_needed=quantity,
        unit_price=net, unit_price_with_tax=number(catalog.get("valueWithTax")),
        price_per_l_or_kg=net / size if net is not None and size and need.quantity.unit != "SALES_UNIT" else None,
        total=round(net * quantity, 2) if net is not None and quantity is not None else None,
        currency=catalog.get("currency") or price.get("_currency", "EUR"), valid_from=valid_from, valid_to=valid_to,
        net_price_found=bool(price.get("netPriceFound")), availability=available, warnings=warnings,
        delivery_reliability=product.delivery_reliability, co2_kg_per_kg=product.co2_kg_per_kg,
        labels=product.labels, matched_because=matched,
        fit=fit["probability"] if fit else None, fit_question=fit["question"] if fit else None,
        provenance={"price": Provenance(source="valio:/api/price", fetched_at=fetched, valid_to=valid_to),
                    "availability": Provenance(source="valio:/api/availability",
                                               fetched_at=timestamp(stock.get("_fetched_at")) or audit.utcnow(), valid_to=available.closing_time)},
    )


def rank(offers, weights):
    metrics = {"price": lambda o: o.price_per_l_or_kg if o.size_unit != "SALES_UNIT" else o.unit_price,
               "coverage": lambda o: o.availability.coverage, "reliability": lambda o: o.delivery_reliability,
               "co2": lambda o: o.co2_kg_per_kg, "fit": lambda o: o.fit}
    for key, value in metrics.items():
        known = [value(o) for o in offers if value(o) is not None]
        low, high = (min(known), max(known)) if known else (0, 0)
        for offer in offers:
            v = value(offer)
            normalized = 0 if v is None else (1 if high == low else (v - low) / (high - low))
            if key in {"price", "co2"} and v is not None and high != low:
                normalized = 1 - normalized
            offer.score += weights[key] * normalized
    offers.sort(key=lambda o: (-o.score, o.product_id))
    for offer, label in zip(offers, ("best", "better", "good")):
        offer.rank_label = label


class Lookup:
    def __init__(self, db, catalog, valio, decisions, policy):
        self.db, self.catalog, self.valio, self.decisions, self.policy = db, catalog, valio, decisions, policy

    def find_offers(self, need, actor="agent:external"):
        query = " ".join(need.search_terms) if need.search_terms else need.intent
        candidates, excluded = self.catalog.search(query, need.required_claims, need.exclude_allergens, need.max_candidates)
        counter = RequestCounter()
        ids = [p.id for p in candidates]
        price, stock, global_warnings = {}, {}, []
        for endpoint, target in (("price", price), ("availability", stock)):
            try:
                target.update(self.valio.fetch(endpoint, need.customer_id, need.deliver_by, ids, counter))
            except DomainError:
                global_warnings.append(warning("PRICE_ERROR" if endpoint == "price" else "AVAILABILITY_ERROR"))
        fits = self.decisions.ask(need, candidates, counter)
        if candidates and (not fits or len(fits) != len(candidates)):
            global_warnings.append(warning("NO_FIT_SCORE"))
        with self.db.connect(write=True) as conn:
            offers = [offer_for(p, need, price.get(p.id, {"error": True}), stock.get(p.id, {"error": True}),
                                (fits or {}).get(p.id), reserved_amount=reserved(conn, p.id)) for p in candidates]
            rank(offers, self.policy.weights)
            result = LookupResult(lookup_id=str(uuid4()), need=need, offers=offers, excluded_count=excluded,
                                  live_calls=counter.live(), decision_calls=counter.decision_calls, warnings=global_warnings)
            conn.execute("INSERT INTO lookups VALUES(?,?)", (result.lookup_id, result.model_dump_json()))
            audit.append(conn, actor, "LOOKUP", f"Shortlisted {len(offers)} offers and excluded {excluded} hard-filter failures.",
                         data={"inputs": need.model_dump(mode="json"), "lookup_id": result.lookup_id,
                               "fit_scores": {o.product_id: o.fit for o in offers}, "live_calls": counter.live().model_dump(),
                               "decision_calls": counter.decision_calls, "warnings": [w.model_dump() for w in global_warnings]})
        return result
