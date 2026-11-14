import re
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
from math import isclose
from uuid import uuid4

from .auth import check_owner

from . import audit
from .mandates import evidence as mandate_evidence, decode
from .catalog import (allergen_evidence, constraint_failures, number, suspect_content, case_suspect,
                      named_case_sizes, name_sales_sizes, name_measures, name_size_evidence, sizes_agree)

from .quantities import pack_quantity, sales_step
from .db import DomainError
from .models import Availability, LookupResult, OfferCard, Provenance, Retrieval, Warning
from .valio import RequestCounter
from .runtime import ENVIRONMENT
from .suppliers import reservation_key

WARNING_TEXT = {
    "CLINICAL_REVIEW": "Clinical review by a buyer approver is required.",
    "SUBSTITUTION_PROFILE": "The substitute needs approval under the customer's substitution profile.",
    "PARTIAL_BASKET": "Some recipe ingredients are uncovered; review the partial basket.",
    "PARTIAL_STOCK": "On-time available stock covers only part of the requested quantity.",
    "DATE_SHIFT": "The upstream API returned a delivery date after the requested deadline.",
    "CUTOFF_PASSED": "The order cut-off for this delivery has passed.",
    "SALES_BLOCK": "The product or a schedule line is blocked for sale.",
    "UNKNOWN_PRODUCT": "Legacy stored unknown-product warning; no longer emitted.",
    "NOT_AVAILABLE": "Valio reports this product as not available",
    "AVAILABILITY_UNCONFIRMED": "Valio does not know this product's stock; its ordering logic accepts any quantity",
    "PRICE_ERROR": "A valid net catalog price could not be obtained.",
    "SIZE_UNKNOWN": "Sales-unit content cannot be converted to the requested unit.",
    "SIZE_MISMATCH": "Pack content evidence differs by more than 3%.",
    "NO_FIT_SCORE": "Decisions fit probability is unavailable; a human must review suitability.",
    "AVAILABILITY_ERROR": "Availability could not be verified with the upstream API.",
    "OUTSIDE_PREFERENCE": "The product is a substitute outside the buyer's soft preferences.",
    "RETRIEVAL_BROADENED": "No intent matches; searched any of these words instead",
    "POOL_TRUNCATED": "Only part of the matching catalog was screened; add search terms to narrow.",
    "SIZE_UNCONFIRMED": "Catalog or name pack content is not confirmed by a live price ratio.",
    "UNIT_MISMATCH": "A schedule salesUnit is missing or differs from the catalog sales unit; its amount counts as zero.",
    "STOCK_MODEL_UNKNOWN": "The stock model is missing or unknown; stock is finite and local reservations are subtracted.",
    "NO_STOCK": "Stock-model product has zero available stock and no delivery date.",
    "CUTOFF_UNKNOWN": "Order cutoff is missing or unparseable and needs human review.",
    "VARIABLE_WEIGHT": "Final price follows the delivered weight.",
    "DEPOSIT": "Product carries deposits; see the catalog deposit details.",
}


# Deliberately small English/Finnish stop-word and unit list, not a language model.
INTENT_STOP_WORDS = frozenset({
    "of", "for", "the", "a", "an", "and", "with", "to", "by", "per",
    "litre", "litres", "liter", "liters", "l", "kg", "g", "ml", "dl", "cl", "pcs", "kpl",
    "kilogram", "kilograms", "gram", "grams", "millilitre", "millilitres", "milliliter", "milliliters",
    "decilitre", "decilitres", "deciliter", "deciliters",
    "centilitre", "centilitres", "centiliter", "centiliters",
    "litra", "litraa", "litrat", "kilo", "kiloa", "kilot",
    "kilogramma", "kilogrammaa", "kilogrammat", "gramma", "grammaa", "grammat",
    "millilitra", "millilitraa", "millilitrat", "desilitra", "desilitraa", "desilitrat",
    "senttilitra", "senttilitraa", "senttilitrat",
    "ja", "tai", "sekä",
})


def broadened_terms(intent):
    """Preserve word order, remove numbers/units, and also try hyphenated parts."""
    words = re.findall(r"[^\W\d_]+(?:-[^\W\d_]+)*", intent.lower(), re.UNICODE)
    terms = []
    for word in words:
        for term in [word, *word.split("-")]:
            if term not in INTENT_STOP_WORDS and term not in terms:
                terms.append(term)
    return terms


def warning(code):
    return Warning(code=code, text=WARNING_TEXT[code])


def timestamp(value, naive_tz=timezone.utc):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=naive_tz)
    except (ValueError, TypeError, OverflowError):
        return None


def day(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def round_quantity(quantity, product):
    return float(pack_quantity(1, requested=quantity, step=sales_step(product)))


def size_for(product, price, requested_unit):
    return reconcile_size(product, price, requested_unit)[:2]


def reconcile_size(product, price, requested_unit):
    if requested_unit == "SALES_UNIT":
        return 1.0, "sales_unit", False, []
    net = number((price.get("catalogPrice") or {}).get("value"))
    comparison = number((price.get("comparisonPrice") or {}).get("value"))
    ratio = None
    if not price.get("error") and price.get("comparisonUnit") == requested_unit and net and net > 0 and comparison and comparison > 0:
        ratio = float(pack_quantity(Decimal(str(net)) / Decimal(str(comparison))))
        if ratio <= 0:
            ratio = None
    if product.sales_unit == requested_unit == "KG":
        # Sold by weight: one sales unit is one kg by definition; a piece weight in the catalog or name
        # ("ca 2,4 kg") describes the item, not the sales unit.
        confirmed = ratio is not None and abs(ratio - 1) <= 0.03
        return 1.0, "base_unit", confirmed, []
    multiplier = product.units.get(product.sales_unit, 1 if product.sales_unit == product.base_unit else None)
    exact = {}
    content = product.base_volume_l if requested_unit == "L" else product.base_net_weight_kg
    sales_content = product.sales_volume_l if requested_unit == "L" else product.sales_net_weight_kg
    # Recheck retained evidence even when an older build stored False after checking only English.
    # Keep each dimension independent so a bad volume does not discard a valid weight (or vice versa).
    names = [product.name_en, product.name_fi]
    named_cases = named_case_sizes(names, requested_unit) if product.sales_unit in {"KI", "CASE"} else []
    # Recompute CASE suspicion from retained evidence: old builds falsely flagged
    # whole-case names after dividing their matching CASE weight by the multiplier.
    base_suspect = suspect_content(content, names, requested_unit)
    sales_suspect = case_suspect(sales_content, multiplier, names, requested_unit, product.base_unit, content)
    if not sales_suspect and sales_content and sales_content > 0:
        exact["catalog"] = float(pack_quantity(sales_content, multiplier=multiplier, whole_case=True))
    named = []
    if multiplier is not None and multiplier > 0:
        evidence = name_size_evidence(names, requested_unit, 1 if product.base_unit == requested_unit else multiplier)
        named = sorted({r["size"] for r in evidence if not r["check_only"] and r["size"] > 0})
        if "catalog" not in exact and not base_suspect and content and content > 0:
            exact["catalog"] = float(pack_quantity(content, multiplier=multiplier))
        if product.base_unit == requested_unit:
            # Here the multiplier is already a measure, not a count of packages.
            exact.setdefault("catalog", float(pack_quantity(multiplier)))
        if "catalog" not in exact and sales_content and sales_content > 0 and not suspect_content(
                sales_content / multiplier, [], requested_unit) and (not named or any(
                max(sales_content / size, size / sales_content) < 10 for size in named)):
            # Retain plausible but contradictory CASE evidence for explicit review.
            # Grossly suspect data still falls back as required by spec 15 R-8.
            exact["catalog"] = float(pack_quantity(sales_content, whole_case=True))
        # Use the same nested-count/net-weight parsing as catalog CASE validation.
        if named:
            # Price can confirm either translation, even when another language
            # agrees with the catalog. Keep every candidate until this decision.
            confirmed_names = [size for size in named if ratio is not None and
                (abs(ratio / size - 1) <= .03 or isclose(abs(ratio / size - 1), .03, rel_tol=1e-12))]
            exact["name_parse"] = next(iter(confirmed_names), next((size for size in named if exact.get("catalog", 0) > 0 and
                abs(size / exact["catalog"] - 1) <= .03), min(named)))

    # A nonpositive size, including one rounded to zero, is never evidence or a divisor.
    exact = {source: size for source, size in exact.items() if size > 0}


    def packaging_warnings(size):
        if named_cases and not any(abs(size / named - 1) <= .03 or
                isclose(abs(size / named - 1), .03, rel_tol=1e-12) for named in named_cases if named > 0):
            return [Warning(code="SIZE_MISMATCH", text=(
                f"{WARNING_TEXT['SIZE_MISMATCH']} used={size:g} {requested_unit}; "
                f"name_case={named_cases} {requested_unit} per sales unit."))]
        return []

    if exact or named:
        if ratio is None:
            source = next(iter(exact))
            # Independent explicit counts can contradict the sales multiplier. A
            # lone "1 x" is an inner package description when the multiplier > 1.
            count_evidence = named_cases if named_cases and not all(
                counts == [1] for name in names for _, counts in name_measures(name, requested_unit)
                if counts) else []
            candidates = [*named, *count_evidence] if multiplier is not None and multiplier > 0 else named_cases
            size = exact[source]
            disagrees = [candidate for candidate in candidates if not (abs(size / candidate - 1) <= .03 or isclose(
                abs(size / candidate - 1), .03, rel_tol=1e-12))]
            if disagrees:
                values = ", ".join(f"{key}={value:g} {requested_unit}" for key, value in exact.items())
                mismatch = Warning(code="SIZE_MISMATCH", text=(
                    f"{WARNING_TEXT['SIZE_MISMATCH']} {values}; name_case={candidates} {requested_unit}; "
                    f"used={size:g} {requested_unit} from {source}; approval required."))
                return size, source, False, [warning("SIZE_UNCONFIRMED"), mismatch]

            return size, source, False, [warning("SIZE_UNCONFIRMED"), *packaging_warnings(size)]
        # Prefer confirmed structured content; the ratio can resolve conflicting catalog/name sizes.
        for source, size in [*exact.items(), *(("name_parse", s) for s in named)]:
            difference = abs(ratio / size - 1)
            if difference < 0.03 or isclose(difference, 0.03, rel_tol=1e-12):
                return size, source, True, packaging_warnings(size)
        values = ", ".join(f"{key}={value:g} {requested_unit}"
                           for key, value in [*exact.items(), *(("name_parse", s) for s in named)])
        mismatch = Warning(code="SIZE_MISMATCH", text=(
            f"{WARNING_TEXT['SIZE_MISMATCH']} {values}; price_ratio={ratio:g} {requested_unit} per sales unit."
        ))
        return ratio, "price_ratio", False, [mismatch]
    if ratio is not None:
        return ratio, "price_ratio", False, packaging_warnings(ratio)
    return None, None, False, []


def reserved(conn, product_id, exclude_order=None, supplier_id="valio"):
    product_id = reservation_key(supplier_id, product_id)
    return conn.execute(
        "SELECT coalesce(sum(quantity),0) FROM reservations WHERE product_id=? AND order_id!=?",
        (product_id, exclude_order or "")).fetchone()[0]


def availability_for(raw, deliver_by, quantity, reserved_amount=0, sales_unit=None):
    codes = []
    # Advance the supplier clock on cache hits without changing signed provenance.
    fetched = timestamp(raw.get("_fetched_at"))
    now = fetched + timedelta(seconds=raw.get("_cache_age", 0)) if fetched else audit.utcnow()
    cutoff = timestamp(raw.get("closingTime"), ZoneInfo("Europe/Helsinki"))
    if cutoff is None:
        codes.append("CUTOFF_UNKNOWN")
    model = raw.get("logisticModel")
    if model not in {"Stock", "Terminal"}:
        codes.append("STOCK_MODEL_UNKNOWN")
    lines = raw.get("scheduleLines") or []
    if sales_unit is not None and any(line.get("salesUnit") != sales_unit for line in lines):
        codes.append("UNIT_MISMATCH")
    blocked = raw.get("salesBlock") or any(line.get("salesBlock") for line in lines)
    if blocked:
        codes.append("SALES_BLOCK")
    if raw.get("error"):
        codes.append("AVAILABILITY_ERROR")
    elif not lines:
        codes.append("NOT_AVAILABLE")
    dates = [d for line in lines if not line.get("salesBlock") and (d := day(line.get("availableDate")))]
    if any(d > deliver_by for d in dates):
        codes.append("DATE_SHIFT")
    if cutoff and cutoff < now:
        codes.append("CUTOFF_PASSED")
    usable = [line for line in lines if not line.get("salesBlock")
              and (sales_unit is None or line.get("salesUnit") == sales_unit)]
    finite = raw.get("_stock_semantics") == "finite"
    unlimited = raw.get("_stock_semantics") == "unlimited"
    unknown = not finite and not unlimited and not (raw.get("salesBlock") or raw.get("error")) and any(
        (number(line.get("availableAmount")) or 0) >= 999999 for line in usable)
    on_time = [line for line in usable if (d := day(line.get("availableDate"))) and d <= deliver_by]
    unknown_on_time = (unknown and any((number(line.get("availableAmount")) or 0) >= 999999 for line in on_time)) or (
        unlimited and bool(on_time) and not raw.get("salesBlock") and not raw.get("error"))
    amount = 0.0 if raw.get("salesBlock") or raw.get("error") else sum(
        max(0, number(line.get("availableAmount")) or 0) for line in on_time
        if finite or (number(line.get("availableAmount")) or 0) < 999999)
    if unknown:
        codes.append("AVAILABILITY_UNCONFIRMED")
    if unknown_on_time:
        amount = quantity or 0
    else:
        amount = max(0, amount - reserved_amount)
    if model == "Stock" and amount == 0 and not dates and lines and not raw.get("error"):
        codes.append("NO_STOCK")
    coverage = min(1.0, amount / quantity) if quantity and quantity > 0 else 0.0
    if quantity and amount < quantity:
        codes.append("PARTIAL_STOCK")
    warnings = [warning(c) for c in codes]
    if cutoff and cutoff < now:
        delivery = ", ".join(sorted({d.isoformat() for d in dates})) or "unknown date"
        warnings = [Warning(code=w.code, text=f"Order cut-off for delivery on {delivery} passed at {cutoff.isoformat()}")
                    if w.code == "CUTOFF_PASSED" else w for w in warnings]
    return Availability(stock_semantics="unlimited" if unlimited else "unknown" if unknown else "finite",
                        available_amount=None if unknown_on_time else amount, availability_known=not unknown_on_time,
                        logistic_model=raw.get("logisticModel"),
                        closing_time=cutoff, available_date=min(dates) if dates else None,
                        enough=quantity is not None and amount >= quantity, coverage=coverage), warnings


def offer_for(product, need, price, stock, fit=None, *, sales_quantity=None, reserved_amount=0, sources=None):
    size, source, confirmed, size_warnings = reconcile_size(product, price, need.quantity.unit)
    quantity = sales_quantity if sales_quantity is not None else (
        float(pack_quantity(size, requested=need.quantity.value,
                            step=sales_step(product))) if size else None)
    available, warnings = availability_for(stock, need.deliver_by, quantity, reserved_amount, product.sales_unit)
    warnings.extend(size_warnings)
    catalog = price.get("catalogPrice") or {}
    net = number(catalog.get("value"))
    valid_from, valid_to = timestamp(catalog.get("validFrom")), timestamp(catalog.get("validTo"))
    fetched = timestamp(price.get("_fetched_at")) or audit.utcnow()
    currency = catalog.get("currency") or price.get("_currency")
    if price.get("error") or net is None or net <= 0 or not currency or valid_to is None or (valid_from and valid_from > fetched) or (valid_to and valid_to <= fetched):
        warnings.append(warning("PRICE_ERROR"))
        net = None
    if not size:
        warnings.append(warning("SIZE_UNKNOWN"))
    if not fit:
        warnings.append(warning("NO_FIT_SCORE"))
    if product.variable_unit:
        warnings.append(warning("VARIABLE_WEIGHT"))
    if product.deposits:
        warnings.append(Warning(code="DEPOSIT", text=f"{WARNING_TEXT['DEPOSIT']} {product.deposits}"))
    preferences, differences = need.preferences, []
    brand = (product.brand or "").strip()  # catalog brands carry stray whitespace ("Arla ")
    if preferences.brands and brand.casefold() not in {b.strip().casefold() for b in preferences.brands}:
        differences.append(f"brand {brand or 'unknown'}, preferred {', '.join(preferences.brands)}")
    if preferences.max_price_per_unit is not None:
        per_unit = net / size if net is not None and size and need.quantity.unit != "SALES_UNIT" else None
        if per_unit is None:
            differences.append("price per L / KG unknown")
        elif per_unit > preferences.max_price_per_unit:
            differences.append(f"{per_unit:g} EUR/{need.quantity.unit}, preferred <= {preferences.max_price_per_unit:g}")
    if preferences.pack_size and (preferences.pack_size.min is not None or preferences.pack_size.max is not None):
        bounds = preferences.pack_size
        if not size or need.quantity.unit == "SALES_UNIT":
            differences.append("pack size in L / KG unknown")
        else:
            if bounds.min is not None and size < bounds.min:
                differences.append(f"{size:g} {need.quantity.unit} pack, preferred >= {bounds.min:g} {need.quantity.unit}")
            if bounds.max is not None and size > bounds.max:
                differences.append(f"{size:g} {need.quantity.unit} pack, preferred <= {bounds.max:g} {need.quantity.unit}")
    for label in preferences.labels:
        if label.casefold() not in {l.casefold() for l in product.labels}:
            differences.append(f"missing preferred label {label}")
    if differences:
        warnings.append(Warning(code="OUTSIDE_PREFERENCE", text="; ".join(differences)))
    def origins(field, code):
        return [v.source for v in getattr(sources, field) if v.code == code] if sources else ["request"]
    failed_claims = {code for field, code, _ in constraint_failures(product, need.required_claims, [])}
    matched = [f"{c} from catalog nutritionalClaim ({origin})" for c in need.required_claims if c not in failed_claims
               for origin in origins("required_claims", c)]
    matched += [f"{evidence} ({origin})" for a in need.exclude_allergens
                if (passes := allergen_evidence(product, a))[0] for evidence in [passes[1]]
                for origin in origins("exclude_allergens", a)]
    total = float((Decimal(str(net)) * Decimal(str(quantity))).quantize(Decimal("0.01"))) if net is not None and quantity is not None else None
    overbuy = max(Decimal(0), pack_quantity(size) * Decimal(str(quantity)) - Decimal(str(need.quantity.value))) if size and quantity else None
    matched.append(f"Purchase total: {total:.2f} {currency}; overbuy: {float(overbuy):g} {need.quantity.unit}."
                   if total is not None and overbuy is not None else "Purchase total or overbuy: unknown.")
    return OfferCard(
        supplier_id=need.supplier_id, synthetic=product.synthetic or stock.get("_synthetic", False),
        product_id=product.id, gtin=product.gtin, name=product.name_en, sales_unit=product.sales_unit,
        allowed_lot_size=float(sales_step(product)),
        size_per_sales_unit=size, size_unit=need.quantity.unit, size_source=source, units_needed=quantity,
        size_confirmed_by_price=confirmed,
        name_size_candidates=[{"size_per_sales_unit": r["size"], "source_text": r["source_text"],
                               "expression": r["expression"]}
            for r in name_size_evidence([product.name_en, product.name_fi], need.quantity.unit,
                product.units.get(product.sales_unit, 1 if product.sales_unit == product.base_unit else None))]
            if need.quantity.unit != "SALES_UNIT" else [],
        unit_price=net, unit_price_with_tax=number(catalog.get("valueWithTax")),
        price_per_l_or_kg=net / size if net is not None and size and need.quantity.unit != "SALES_UNIT" else None,
        total=total, purchase_total=total,
        overbuy={"amount": float(overbuy), "unit": need.quantity.unit} if overbuy is not None else None,
        currency=currency, valid_from=valid_from, valid_to=valid_to,
        net_price_found=bool(price.get("netPriceFound")), availability=available, warnings=warnings,
        delivery_reliability=product.delivery_reliability, co2_kg_per_kg=product.co2_kg_per_kg,
        labels=product.labels, matched_because=matched,
        fit=fit["probability"] if fit else None, fit_question=fit["question"] if fit else None,
        provenance={"price": Provenance(source=price.get("_source", "valio:/api/price"), fetched_at=fetched, valid_to=valid_to,
                    **({"environment": "sandbox", "synthetic": True} if ENVIRONMENT.get() == "sandbox" else
                       {"synthetic": True} if product.synthetic else {})),
                    "availability": Provenance(source=stock.get("_source", "valio:/api/availability"),
                                               **({"environment": "sandbox", "synthetic": True} if ENVIRONMENT.get() == "sandbox" else
                                                  {"synthetic": True} if product.synthetic else {}),
                                               requested_date=need.deliver_by, returned_dates=[d for line in stock.get("scheduleLines", []) if (d := day(line.get("availableDate")))],

                                               fetched_at=timestamp(stock.get("_fetched_at")) or audit.utcnow(), valid_to=available.closing_time)},
    )


def rank(offers, weights, fit_floor=None, *, catalog_stats=None, reject_codes=()):
    catalog_stats = catalog_stats or {}
    invalid = set(reject_codes) | {"SALES_BLOCK", "UNKNOWN_PRODUCT", "NOT_AVAILABLE", "NO_STOCK", "SIZE_UNKNOWN",
                                   "PRICE_ERROR", "AVAILABILITY_ERROR", "UNIT_MISMATCH"}
    def unorderable(offer):
        return any(w.code in invalid for w in offer.warnings)
    for offer in offers:
        offer.score, offer.rank_label, offer.score_breakdown = 0, None, {}
    metrics = {"price": lambda o: o.purchase_total if o.size_per_sales_unit is not None else (o.price_per_l_or_kg or o.unit_price),
               "coverage": lambda o: o.availability.coverage, "reliability": lambda o: o.delivery_reliability,
               "co2": lambda o: o.co2_kg_per_kg,
               # Above the suitability gate, fit noise must not change the commercial ranking.
               "fit": lambda o: min(o.fit, fit_floor) if o.fit is not None and fit_floor is not None else o.fit}
    for key, value in metrics.items():
        known = [value(o) for o in offers if value(o) is not None and not unorderable(o)]
        low, high = (min(known), max(known)) if known else (0, 0)
        unit_prices = [value(o) for o in offers if o.size_per_sales_unit is None and value(o) is not None]
        unit_low, unit_high = (min(unit_prices), max(unit_prices)) if unit_prices else (0, 0)
        for offer in offers:
            v = value(offer)
            if key in {"reliability", "co2"}:
                stats = catalog_stats.get(key, {})
                if v is None:
                    v = stats.get("median")
                    text = f"{'reliability' if key == 'reliability' else 'CO2'}: no data (neutral)"
                    if text not in offer.matched_because:
                        offer.matched_because.append(text)
                if key == "reliability":
                    normalized = v if v is not None else .5
                else:
                    lo, hi = stats.get("min", 0), stats.get("max", 0)
                    normalized = (hi - v) / (hi - lo) if v is not None and hi > lo else .5
            elif key in {"coverage", "fit"}:
                normalized = v if v is not None else 0
            else:
                lo, hi = (unit_low, unit_high) if offer.size_per_sales_unit is None else (low, high)
                normalized = 0 if v is None else (1 if hi == lo else (hi - v) / (hi - lo))
            contribution = weights[key] * max(0, min(1, normalized))
            offer.score_breakdown[key] = contribution
            offer.score += contribution
    offers.sort(key=lambda o: (unorderable(o), o.fit is None,
                               any(w.code == "OUTSIDE_PREFERENCE" for w in o.warnings), -o.score, o.product_id))
    for offer, label in zip((o for o in offers if not unorderable(o)), ("best", "better", "good")):
        offer.rank_label = label


class Lookup:
    def __init__(self, db, catalog, valio, decisions, policy, mandates):
        self.db, self.catalog, self.valio, self.decisions, self.policy = db, catalog, valio, decisions, policy
        self.mandates = mandates

    def get(self, lookup_id, agent_id=None):
        with self.db.connect() as conn:
            row = conn.execute("SELECT data FROM lookups WHERE id=?", (lookup_id,)).fetchone()
        if not row:
            raise DomainError("LOOKUP_NOT_FOUND", "Lookup was not found", 404)
        result = check_owner(LookupResult.model_validate_json(row["data"], context={"stored": True}),
                             agent_id, "LOOKUP_NOT_FOUND")
        from .foundation import forward_links
        with self.db.connect() as conn:
            result.links.update(forward_links(conn, request_id=lookup_id, agent_id=agent_id))
        return result

    def find_offers(self, need, actor="agent:external", *, mandate=None):
        return self._run(need, actor, lambda counter: self._find_offers(need, actor, mandate, counter))

    def _run(self, need, actor, operation):
        counter = RequestCounter()
        try:
            return operation(counter)
        except DomainError as error:
            with self.db.connect(write=True) as conn:
                audit.append(conn, actor, "LOOKUP_FAILED", error.message,
                             data={"inputs": need.model_dump(mode="json"), "error": error.code,
                                   "live_calls": counter.live().model_dump(),
                                   "decision_calls": counter.decision_calls, **error.data})
            raise

    def _find_offers(self, need, actor, mandate, counter):
        adapter = self.suppliers.get(need.supplier_id)
        catalog = adapter.catalog
        # Check authority before catalog screening, Decisions or any Valio call.
        with self.db.connect() as conn:
            policy, record, mandate = self.mandates.authorize(conn, mandate, actor.removeprefix("agent:"), need.customer_id, supplier_id=need.supplier_id)
        query = " ".join(need.search_terms) if need.search_terms else need.intent
        original_need = need
        need, effective = self.policy.effective_need(need, policy)
        pool_size = max(need.max_candidates, policy.shortlist.catalog_pool)
        categories = decode(mandate)[1]["vc"]["credentialSubject"].get("categories") if mandate is not None else None
        from .substitution import functional_categories
        functional = functional_categories(catalog, need)
        if functional is not None:
            categories = sorted(functional if categories is None else functional.intersection(categories))
        removed_by, counts = {}, {}
        retrieval = Retrieval(mode="terms" if need.search_terms else "intent",
                              terms_used=list(need.search_terms) if need.search_terms else
                              re.findall(r"[^\W_]+", need.intent, re.UNICODE))
        pool, excluded = catalog.search(query, need.required_claims, need.exclude_allergens, pool_size,
                                             max_limit=pool_size, removed_by=removed_by, counts=counts, sources=effective,
                                             categories=categories)
        global_warnings = []
        if not pool and retrieval.mode == "intent":
            retrieval = Retrieval(mode="intent_broadened", terms_used=broadened_terms(need.intent))
            removed_by, counts = {}, {}
            pool, excluded = catalog.search(" ".join(retrieval.terms_used), need.required_claims,
                                                 need.exclude_allergens, pool_size, max_limit=pool_size,
                                                 removed_by=removed_by, mode="or", counts=counts, sources=effective,
                                                 categories=categories)
            global_warnings.append(Warning(code="RETRIEVAL_BROADENED", text=(
                f"{WARNING_TEXT['RETRIEVAL_BROADENED']}: {', '.join(retrieval.terms_used) or '(none)'}.")))
        matched_count = counts.get("matched_count", len(pool))
        from .substitution import include_preferred
        pool = include_preferred(pool, need, catalog, categories)[:pool_size]
        # Fit gates the live shortlist; price/stock must be known before ranking suitable products.
        fits = self.decisions.ask(need, pool, counter)
        screened_count = counter.screened_count
        if need.function:
            # Functional substitution is offered only with both category and fit evidence.
            failed = [p for p in pool if (fits or {}).get(p.id, {}).get("probability", -1) < policy.approval.min_fit_probability]
            if failed:
                removed_by["function (fit not proven)"] = len(failed)
                excluded += len(failed)
            pool = [p for p in pool if p not in failed]
        if matched_count > screened_count:
            global_warnings.append(Warning(code="POOL_TRUNCATED", text=(
                f"{screened_count:,} of {matched_count:,} matching products screened; add search terms to narrow.")))
        if fits:
            order = {p.id: i for i, p in enumerate(pool)}
            pool = sorted(pool, key=lambda p: (-(fits.get(p.id) or {}).get("probability", -1), order[p.id]))
            passing = sum((fits.get(p.id) or {}).get("probability", -1) >= policy.approval.min_fit_probability for p in pool)
            candidates = pool[:min(policy.shortlist.live_max, max(passing, need.max_candidates))]
        else:
            candidates = pool[:need.max_candidates]
        if need.substitute_for:
            preferred = next((p for p in pool if p.id == need.substitute_for.product_id), None)
            if preferred and all(p.id != preferred.id for p in candidates):
                candidates = [preferred, *candidates[:max(0, policy.shortlist.live_max - 1)]]
        ids = [p.id for p in candidates]
        if need.substitute_for and need.substitute_for.product_id not in ids:
            ids.append(need.substitute_for.product_id)
        price, stock = {}, {}
        for endpoint, target in (("price", price), ("availability", stock)):
            try:
                target.update(adapter.fetch(endpoint, need.customer_id, need.deliver_by, ids, counter))
            except DomainError as error:
                if (error.code in {"UPSTREAM_BUSY", "UPSTREAM_RATE_LIMITED", "SUPPLIER_UNAVAILABLE"}
                        or error.code == "VALIO_UNAVAILABLE" and (
                            error.data.get("timeout") or error.data.get("upstream_status") in {502, 503})):
                    raise
                global_warnings.append(warning("PRICE_ERROR" if endpoint == "price" else "AVAILABILITY_ERROR"))
        if candidates and (not fits or any(p.id not in fits for p in candidates)):
            global_warnings.append(warning("NO_FIT_SCORE"))
        agent_id = actor.removeprefix("agent:")
        # A deferred read transaction gives reservations and spend one consistent snapshot.
        # Release it before planning; only persistence needs the SQLite writer lock.
        with self.db.connect() as conn:
            if not conn.in_transaction:
                conn.execute("BEGIN")
            reservations = {p.id: reserved(conn, p.id, supplier_id=need.supplier_id) for p in candidates}
            autonomous_total = self.policy.autonomous_total(conn, agent_id)
        offers = [offer_for(p, need, price.get(p.id, {"error": True}), stock.get(p.id, {"error": True}),
                            (fits or {}).get(p.id), reserved_amount=reservations[p.id], sources=effective) for p in candidates]
        if any(o.currency is not None and o.currency != "EUR" for o in offers):
            raise DomainError("UNSUPPORTED_CURRENCY", "Policy supports EUR only; no FX rate source is configured", 422)
        rank(offers, policy.ranking_weights.model_dump(), policy.approval.min_fit_probability,
             catalog_stats=catalog.metadata("ranking"), reject_codes=policy.reject.on)
        from .substitution import annotate, rank_substitutes
        for offer in offers:
            annotate(offer, need, catalog, policy, price)
        if need.substitute_for:
            offers = rank_substitutes(offers, need, catalog, price)

        from .plans import build_plans
        returned = offers[:need.max_candidates]
        plans = build_plans(need, offers, self.policy, policy, agent_id, catalog,
                            autonomous_total=autonomous_total)
        no_match_reason = None
        if not pool:
            constraint_counts = "; ".join(f"{key} removed {count} candidates" for key, count in sorted(removed_by.items()))
            no_match_reason = (f"No products pass the hard filters: {constraint_counts}. Counts may overlap."
                               if constraint_counts else "No catalog products match the search terms.")
            if retrieval.mode == "intent_broadened":
                tried = ", ".join(retrieval.terms_used) or "(no meaningful words)"
                no_match_reason += f" Tried words: {tried}. Supply search_terms to narrow the product search."
        # The stored need remains the request, so removed standing rules do not become permanent request rules.
        stored_need = original_need.model_copy(update={
            "required_claims": catalog.normalize_codes(original_need.required_claims, "required_claims"),
            "exclude_allergens": catalog.normalize_codes(original_need.exclude_allergens, "exclude_allergens"),
            "preferences": need.preferences})
        result = LookupResult(supplier_id=need.supplier_id, environment=ENVIRONMENT.get(), synthetic=adapter.synthetic, mandate=record, lookup_id=str(uuid4()), agent_id=agent_id, need=stored_need, effective_constraints=effective,
                              exclusion_counts=removed_by, offers=returned, plans=plans,
                              no_match_reason=no_match_reason, excluded_count=excluded,
                              retrieval=retrieval, matched_count=matched_count,
                              priced_count=len(candidates), screened_count=screened_count,
                              live_calls=counter.live(), decision_calls=counter.decision_calls, warnings=global_warnings)
        with self.db.connect(write=True) as conn:
            _, result.mandate, _ = self.mandates.authorize(conn, mandate, agent_id, need.customer_id, products=[o.product_id for o in offers], supplier_id=need.supplier_id)
            from .foundation import CORRELATION, procurement_context, trace_id
            result.context = procurement_context(self.policy, need.customer_id, result.lookup_id, effective, result.mandate, supplier_id=need.supplier_id)
            result.correlation_id = CORRELATION.get()
            result.links = {"request_id": result.lookup_id, "trace_id": trace_id(),
                            "event_id": str(uuid4()), "task_id": None}
            self.mandates.bind(conn, "lookup", result.lookup_id, mandate)
            conn.execute("INSERT INTO lookups VALUES(?,?)", (result.lookup_id, result.model_dump_json()))
            conn.execute("INSERT INTO lookup_offers VALUES(?,?)", (result.lookup_id, json.dumps([o.model_dump(mode="json") for o in offers])))
            audit.append(conn, actor, "LOOKUP", f"Priced {len(candidates)} candidates, returned {len(result.offers)} offers and excluded {excluded} hard-filter failures.",
                         data={"inputs": need.model_dump(mode="json"), "lookup_id": result.lookup_id, **mandate_evidence(result.mandate),
                                "fit_scores": {o.product_id: o.fit for o in offers}, "screened_by_decisions": screened_count,
                               "retrieval": result.retrieval.model_dump(), "matched_count": result.matched_count,
                               "priced_count": result.priced_count, "screened_count": result.screened_count,
                               "live_calls": counter.live().model_dump(),
                               "decision_calls": counter.decision_calls, "warnings": [w.model_dump() for w in global_warnings]})
        return result
