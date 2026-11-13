"""Bounded fulfilment proposals using already-priced offers, without upstream calls or writes."""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from fractions import Fraction

from .models import CoveredQuantity, FulfilmentPlan, PlanLine, Warning


def build_plans(need, offers, evaluator, policy, conn, agent_id, catalog):
    target = Decimal(str(need.quantity.value))
    # Hard filters already ran before pricing. Exclude unverifiable/unsuitable offers as well.
    invalid = {"PRICE_ERROR", "SIZE_UNKNOWN", "SALES_BLOCK", "UNKNOWN_PRODUCT", "AVAILABILITY_ERROR"}
    eligible = [o for o in offers if o.fit is not None and o.fit >= policy.approval.min_fit_probability
                and o.currency == "EUR" and o.unit_price is not None and o.size_per_sales_unit
                and not any(w.code in invalid for w in o.warnings)
                and o.availability.available_amount >= 1]
    sizes = [Decimal(str(o.size_per_sales_unit)) for o in eligible]
    steps = [Fraction(str(catalog.get(o.product_id).allowed_lot_size)).numerator if o.sales_unit == "KG" else 1
             for o in eligible]
    limits = [int(Decimal(str(o.availability.available_amount)).to_integral_value(rounding=ROUND_FLOOR)) // step * step
              for o, step in zip(eligible, steps)]
    prices = [Decimal(str(o.unit_price)) for o in eligible]

    def line_cost(i, units):
        # Match offer_for's existing rounding, including prices with more than two decimals.
        return Decimal(str(round(eligible[i].unit_price * units, 2)))

    def cost(allocation):
        return sum(line_cost(i, units) for i, units in allocation)

    def make(allocation, kind):
        coverage = sum(sizes[i] * units for i, units in allocation)
        uncovered = max(Decimal(0), target - coverage)
        plan_warnings = ([Warning(code="PARTIAL_STOCK", text=(
            f"On-time plan leaves {uncovered:g} {need.quantity.unit} of the original need uncovered."))]
            if uncovered else [])
        lines, warnings = [], list(plan_warnings)
        for i, units in allocation:
            line = eligible[i].model_copy(deep=True)
            line.units_needed, line.total = units, float(line_cost(i, units))
            line.availability.enough, line.availability.coverage = True, 1
            # A split line can cover its allocation even though its original full-need offer could not.
            line.warnings = [w for w in line.warnings if w.code != "PARTIAL_STOCK"]
            lines.append(line)
            for warning in line.warnings:
                if warning not in warnings:
                    warnings.append(warning)
        total = float(cost(allocation))
        preview = evaluator.evaluate_order(conn, total, lines, agent_id=agent_id, policy=policy,
                                           warnings=plan_warnings)
        return FulfilmentPlan(
            kind=kind, lines=[PlanLine(product_id=eligible[i].product_id, units=units,
                                      covers=CoveredQuantity(value=float(sizes[i] * units), unit=need.quantity.unit))
                              for i, units in allocation],
            total=total, coverage=CoveredQuantity(value=float(coverage), unit=need.quantity.unit),
            uncovered=CoveredQuantity(value=float(uncovered), unit=need.quantity.unit),
            warnings=warnings, policy_preview=preview)

    singles = []
    for i, size in enumerate(sizes):
        units = int((target / size / steps[i]).to_integral_value(rounding=ROUND_CEILING)) * steps[i]
        if units <= limits[i]:
            singles.append(((i, units),))

    complete, partial = {}, {}

    def fill(allocation=(), covered=Decimal(0)):
        # Try each ranked offer as the next source, comparing the cost of the resulting fills.
        # At most 10P4 paths; never enumerate individual litres or make another live request.
        if allocation:
            key = tuple(sorted(allocation))
            (complete if covered >= target else partial)[key] = covered
        if covered >= target or len(allocation) == 4:
            return
        used = {i for i, _ in allocation}
        for i, size in enumerate(sizes):
            if i in used:
                continue
            units = min(limits[i], int(((target - covered) / size / steps[i]).to_integral_value(rounding=ROUND_CEILING)) * steps[i])
            if units:
                fill((*allocation, (i, units)), covered + size * units)

    fill()
    plans = [make(allocation, "single") for allocation in singles[:3]]
    best = min(complete, key=lambda a: (cost(a), a)) if complete else None
    # Greedy fills provide a fast upper bound. Branch-and-bound also checks interior allocations:
    # e.g. one 6 L pack + one 4 L pack can beat either rounded-up single-product order.
    order = sorted(range(len(eligible)), key=lambda i: (prices[i] / sizes[i], i))
    rounding_slack = (Decimal("0.0200001") if any(p != p.quantize(Decimal("0.01")) for p in prices)
                      else Decimal(0))
    visited = {}

    def cheapest(start=0, allocation=(), covered=Decimal(0), paid=Decimal(0)):
        nonlocal best
        if covered >= target:
            if best is None or cost(allocation) < cost(best):
                best = allocation
            return
        slots = 4 - len(allocation)
        remaining = order[start:]
        if not slots or not remaining:
            return
        state = (start, slots, covered)
        if state in visited and visited[state] <= paid:
            return
        visited[state] = paid
        if covered + sum(sorted((sizes[i] * limits[i] for i in remaining), reverse=True)[:slots]) < target:
            return
        lower = paid + (target - covered) * (prices[remaining[0]] / sizes[remaining[0]]) - rounding_slack
        lower = max(Decimal(0), lower)
        if best is not None and lower.quantize(Decimal("0.01"), rounding=ROUND_CEILING) >= cost(best):
            return
        i = remaining[0]
        maximum = min(limits[i], int(((target - covered) / sizes[i] / steps[i]).to_integral_value(rounding=ROUND_CEILING)) * steps[i])
        for units in range(maximum, -1, -steps[i]):
            amount = line_cost(i, units)
            if best is None or paid + amount < cost(best):
                cheapest(start + 1, (*allocation, (i, units)) if units else allocation,
                         covered + sizes[i] * units, paid + amount)

    cheapest()
    if best and len(best) > 1 and (not singles or cost(best) < min(cost(a) for a in singles)):
        plans.append(make(best, "split"))
    if not complete and partial:
        best = min(partial, key=lambda a: (-partial[a], cost(a), a))
        plans.append(make(best, "partial"))
    return plans
