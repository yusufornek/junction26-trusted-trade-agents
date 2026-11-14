"""Evidence-based substitutions; procurement compliance is not a clinical safety claim."""
from .catalog import allergen_evidence, constraint_failures, number
from .db import DomainError
from .lookup import reconcile_size
from .models import Substitution, Warning


COMPLIANCE_TEXT = "Compliant with the recorded procurement constraints."


def functional_categories(catalog, need):
    if need.function and need.quantity.unit not in need.function.acceptable_units:
        raise DomainError("FUNCTION_UNIT_MISMATCH", "Requested unit is not in function.acceptable_units", 422)
    if need.function:
        matches, _ = catalog.search(need.function.product_type, limit=100, max_limit=100)
        return {p.category for p in matches if p.category}
    if need.substitute_for:
        preferred = catalog.get(need.substitute_for.product_id)
        return {preferred.category} if preferred.category else set()
    return None


def include_preferred(pool, need, catalog, categories):
    if need.substitute_for:
        p = catalog.get(need.substitute_for.product_id)
        if (not constraint_failures(p, need.required_claims, need.exclude_allergens)
                and (categories is None or p.category in categories) and all(p.id != x.id for x in pool)):
            pool = [p, *pool]
    return pool


def annotate(offer, need, catalog, policy, prices):
    if not need.substitute_for or offer.product_id == need.substitute_for.product_id:
        return
    original = catalog.get(need.substitute_for.product_id)
    product = catalog.get(offer.product_id)
    baseline = prices.get(original.id, {})
    size = reconcile_size(original, baseline, need.quantity.unit)[0]
    net = number((baseline.get("catalogPrice") or {}).get("value"))
    currency = (baseline.get("catalogPrice") or {}).get("currency") or baseline.get("_currency")
    differences = []
    if product.brand != original.brand:
        differences.append(f"brand: {original.brand or 'unknown'} → {product.brand or 'unknown'}")
    missing = sorted(set(original.claims) - set(product.claims))
    if missing:
        differences.append("claims absent from substitute: " + ", ".join(missing))
    if offer.size_per_sales_unit != size:
        differences.append(f"pack size: {size} → {offer.size_per_sales_unit} {need.quantity.unit}")
    increase = None
    if net and net > 0 and size and offer.price_per_l_or_kg is not None and not baseline.get("error") and currency == offer.currency:
        increase = (offer.price_per_l_or_kg / (net / size) - 1) * 100
        differences.append(f"comparable price increase: {increase:.2f}%")
    else:
        differences.append("comparable price increase: unknown")
    evidence = [allergen_evidence(product, code)[1] for code in need.exclude_allergens]
    offer.substitution = Substitution(**{"for": original.id, "differences": differences, "allergen_evidence": evidence,
        "comparison": {"brand": product.brand, "pack_size": offer.size_per_sales_unit,
                       "price_increase_pct": increase, "unit": need.quantity.unit}})
    if COMPLIANCE_TEXT not in offer.matched_because:
        offer.matched_because.append(COMPLIANCE_TEXT)
    apply_profile(offer, policy, need.customer_id)


def apply_profile(offer, policy, customer_id):
    """Current authority over immutable quoted comparison facts, never current prices."""
    if offer.substitution is None:
        return
    customer = policy.customers.get(customer_id)
    profile = customer.substitution_profile if customer else None
    comparison = offer.substitution.comparison or {}
    brand, size, increase = (comparison.get(k) for k in ("brand", "pack_size", "price_increase_pct"))
    offer.warnings = [w for w in offer.warnings if w.code != "SUBSTITUTION_PROFILE"]
    reasons = []
    if profile:
        if profile.requires_approval:
            reasons.append("profile requires substitution approval")
        if profile.allowed_brands and (brand or "").casefold() not in {b.casefold() for b in profile.allowed_brands}:
            reasons.append("brand outside approved substitution profile")
        bounds = profile.pack_size_range
        if (not size or (bounds.min is not None and size < bounds.min)
                or (bounds.max is not None and size > bounds.max)):
            reasons.append("pack size outside approved substitution profile")
        if increase is None or increase > profile.max_price_increase_pct + 1e-9:
            reasons.append(f"price increase exceeds {profile.max_price_increase_pct:g}% or cannot be verified")
    if reasons:
        offer.warnings.append(Warning(code="SUBSTITUTION_PROFILE", text="; ".join(reasons)))


def rank_substitutes(offers, need, catalog, prices):
    original = catalog.get(need.substitute_for.product_id)
    preferred = next((o for o in offers if o.product_id == original.id), None)
    blocked = {"PRICE_ERROR", "AVAILABILITY_ERROR", "SIZE_UNKNOWN", "UNIT_MISMATCH", "SALES_BLOCK", "NO_STOCK", "NOT_AVAILABLE"}
    def usable(o):
        return o.availability.enough and not any(w.code in blocked for w in o.warnings)
    if preferred and usable(preferred) and preferred.arrives_in_time is not False:
        preferred.rank_label = "best"
        return [preferred]
    baseline = prices.get(original.id, {})
    size = preferred.size_per_sales_unit if preferred else reconcile_size(original, baseline, need.quantity.unit)[0]
    net = number((baseline.get("catalogPrice") or {}).get("value"))
    original_price = net / size if net and net > 0 and size and not baseline.get("error") else None
    alternatives = [o for o in offers if o.product_id != original.id]
    alternatives.sort(key=lambda o: (
        not usable(o), catalog.get(o.product_id).category != original.category,
        not set(original.claims).issubset(catalog.get(o.product_id).claims),
        abs(o.size_per_sales_unit - size) if size and o.size_per_sales_unit else float("inf"),
        abs(o.price_per_l_or_kg - original_price) if original_price is not None and o.price_per_l_or_kg is not None else float("inf"),
        -o.score, o.product_id))
    for i, o in enumerate(alternatives):
        o.rank_label = ("best", "better", "good")[i] if i < 3 and usable(o) else None
    return alternatives
