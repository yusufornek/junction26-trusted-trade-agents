from .telemetry import traced
"""Bounded fulfilment proposals using already-priced offers, without upstream calls or writes."""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from .models import CoveredQuantity, FulfilmentPlan, PlanLine, Warning
from .quantities import pack_quantity, sales_step


@traced("plan.build")
def build_plans(need, offers, evaluator, policy, agent_id, catalog, *,
                autonomous_total=Decimal(0), node_budget=20_000):
    target = Decimal(str(need.quantity.value))
    # Hard filters already ran before pricing. Exclude unverifiable/unsuitable offers as well.
    # Stock warnings on offer cards refer to the whole need, not the eventual split allocation.
    invalid = (set(policy.reject.on) - {"PARTIAL_STOCK"}) | {"PRICE_ERROR", "SIZE_UNKNOWN", "SALES_BLOCK", "UNKNOWN_PRODUCT", "NOT_AVAILABLE",
                                      "AVAILABILITY_ERROR", "UNIT_MISMATCH", "NO_STOCK"}
    eligible = [o for o in offers if o.fit is not None and o.fit >= policy.approval.min_fit_probability
                and o.currency == "EUR" and o.unit_price is not None and o.size_per_sales_unit
                and not any(w.code in invalid for w in o.warnings)
                and (o.availability.coverage > 0 if not o.availability.availability_known
                     else o.availability.available_amount > 0)]
    steps = [sales_step(o) for o in eligible]
    # Search in integer lot counts; convert back to sales quantities only at the boundary.
    sizes = [pack_quantity(o.size_per_sales_unit) * step for o, step in zip(eligible, steps)]
    limits = [int((Decimal(str(o.availability.available_amount if o.availability.availability_known
                             else o.units_needed * o.availability.coverage)) / step).to_integral_value(rounding=ROUND_FLOOR))
              for o, step in zip(eligible, steps)]
    prices = [Decimal(str(o.unit_price)) * step for o, step in zip(eligible, steps)]

    def needed_lots(i, requested):
        return int(pack_quantity(eligible[i].size_per_sales_unit, requested=requested, step=steps[i]) / steps[i])

    def line_cost(i, lots):
        # Same decimal currency rounding as offer_for, including fractional-cent unit prices.
        return (Decimal(str(eligible[i].unit_price)) * lots * steps[i]).quantize(Decimal("0.01"))

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
            line.units_needed, line.total = float(units * steps[i]), float(line_cost(i, units))
            line.availability.enough, line.availability.coverage = True, 1
            # A split line can cover its allocation even though its original full-need offer could not.
            line.warnings = [w for w in line.warnings if w.code != "PARTIAL_STOCK"]
            lines.append(line)
            for warning in line.warnings:
                if warning not in warnings:
                    warnings.append(warning)
        total = float(cost(allocation))
        preview = evaluator.evaluate(total, lines, agent_id=agent_id, customer_id=need.customer_id,
                                     autonomous_total=autonomous_total, policy=policy, warnings=plan_warnings)
        return FulfilmentPlan(
            kind=kind, lines=[PlanLine(product_id=eligible[i].product_id, units=float(units * steps[i]),
                                      covers=CoveredQuantity(value=float(sizes[i] * units), unit=need.quantity.unit))
                              for i, units in allocation],
            total=total, coverage=CoveredQuantity(value=float(coverage), unit=need.quantity.unit),
            uncovered=CoveredQuantity(value=float(uncovered), unit=need.quantity.unit),
            warnings=warnings, policy_preview=preview)

    singles = []
    for i, size in enumerate(sizes):
        units = needed_lots(i, target)
        if units <= limits[i]:
            singles.append(((i, units),))

    nodes_left = max(0, node_budget)  # Shared by every search, in preferred-only and mixed groups.

    def composite(indices):
        complete, partial = {}, {}

        def remember(allocation, covered):
            if allocation:
                key = tuple(sorted(allocation))
                (complete if covered >= target else partial)[key] = covered

        # Bounded greedy seeds survive even a zero search budget. Taking the four largest
        # capacities guarantees a complete fallback whenever four eligible lines can cover.
        for order in (indices, sorted(indices, key=lambda i: (-sizes[i] * limits[i], i)),
                      sorted(indices, key=lambda i: (prices[i] / sizes[i], i))):
            allocation, covered = (), Decimal(0)
            for i in order[:4]:
                units = min(limits[i], needed_lots(i, target - covered))
                if units > 0:
                    allocation = (*allocation, (i, units))
                    covered += sizes[i] * units
                if covered >= target:
                    break
            remember(allocation, covered)

        def fill(allocation=(), covered=Decimal(0)):
            nonlocal nodes_left
            nodes_left -= 1
            remember(allocation, covered)
            if covered >= target or len(allocation) == 4:
                return
            used = {i for i, _ in allocation}
            for i in indices:
                if nodes_left <= 0:
                    return
                if i in used:
                    continue
                units = min(limits[i], needed_lots(i, target - covered))
                if units:
                    fill((*allocation, (i, units)), covered + sizes[i] * units)

        if nodes_left > 0:
            fill()
        if not complete:
            if partial:
                return min(partial, key=lambda a: (-partial[a], cost(a), a)), "partial"
            return None
        best = min(complete, key=lambda a: (cost(a), a))
        best_cost = cost(best)
        order = sorted(indices, key=lambda i: (prices[i] / sizes[i], i))
        capacities = {i: sizes[i] * limits[i] for i in indices}
        # Precompute each suffix's top-k capacity; no sorting in the search itself.
        max_cover = []
        for start in range(len(order) + 1):
            largest = sorted((capacities[i] for i in order[start:]), reverse=True)
            max_cover.append([sum(largest[:slots]) for slots in range(5)])

        def lower_bound(start, covered, paid, slots):
            remaining = target - covered
            if remaining <= 0:
                return paid
            if not slots or max_cover[start][slots] < remaining:
                return Decimal("Infinity")
            # Fractional cheapest-first fill respects stock (relaxes lots and the line limit).
            lower = paid
            for i in order[start:]:
                amount = min(remaining, capacities[i])
                lower += amount * prices[i] / sizes[i]
                remaining -= amount
                if remaining <= 0:
                    break
            # Deliberately ignore savings of a cent caused only by split-line rounding.
            return lower.quantize(Decimal("0.01"), rounding=ROUND_CEILING)

        def cheapest(start=0, allocation=(), covered=Decimal(0), paid=Decimal(0)):
            nonlocal best, best_cost, nodes_left
            if nodes_left <= 0:
                return
            nodes_left -= 1
            if covered >= target:
                if paid < best_cost:
                    best, best_cost = allocation, paid
                return
            slots = 4 - len(allocation)
            if lower_bound(start, covered, paid, slots) >= best_cost:
                return
            i = order[start]
            maximum = min(limits[i], needed_lots(i, target - covered))
            for units in range(maximum, 0, -1):
                if nodes_left <= 0:
                    return
                next_covered, next_paid = covered + sizes[i] * units, paid + line_cost(i, units)
                # Below the target, shrinking this cheapest source raises the child bound.
                # The initial rounded-up lot may over-cover; dropping that lot can lower cost.
                if lower_bound(start + 1, next_covered, next_paid, slots - 1) >= best_cost:
                    if next_covered < target:
                        break
                    continue
                cheapest(start + 1, (*allocation, (i, units)), next_covered, next_paid)
            # Skipping this source restores a line slot, so check it even after a capacity break.
            if nodes_left > 0:
                cheapest(start + 1, allocation, covered, paid)

        if nodes_left > 0:
            cheapest()
        group_singles = [a for a in singles if a[0][0] in indices]
        if len(best) > 1 and (not group_singles or best_cost < min(cost(a) for a in group_singles)):
            return best, "split"
        return None

    preferred = [i for i, offer in enumerate(eligible)
                 if not any(w.code == "OUTSIDE_PREFERENCE" for w in offer.warnings)]
    preferred_singles = [a for a in singles if a[0][0] in preferred]
    proposals = [(a, "single") for a in preferred_singles[:3]]
    preferred_plan = composite(preferred)
    if preferred_plan:
        proposals.append(preferred_plan)
    substitute_singles = [a for a in singles if a[0][0] not in preferred]
    proposals.extend((a, "single") for a in substitute_singles[:max(0, 3 - len(preferred_singles))])
    if len(preferred) != len(eligible):
        mixed = composite(list(range(len(eligible))))
        if mixed and any(i not in preferred for i, _ in mixed[0]):
            proposals.append(mixed)
    return [make(allocation, kind) for allocation, kind in proposals]
