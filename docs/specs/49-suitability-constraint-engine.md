# 49 — Suitability constraint engine

Status: in progress · Decided on 2026-11-14 with Gökalp (capability spec, see spec 47). Builds on specs 03 (policy),
31 (functional substitution), 45 (guardrail profiles) and 46 R-6 (nutrition limits).

## Purpose
Whether a product may be bought for a customer is decided by one engine of declarative constraints, so any new rule —
remaining shelf life, nutrition, contract eligibility, certification, origin — is a configuration entry, and missing data
is never treated as proof.

## Requirements
- **R-1 Constraint registry:** constraint types are registered with an evaluator, the catalog or offer fields they need,
  and their outcome on missing data (`exclude`, `needs_approval`). Built-in types: allergen exclusion, nutrient limit
  per 100 g/ml, remaining shelf life at the consumption date (`min_days_left` against lot `use_by` / `best_before`,
  date type kept distinct), framework agreement eligibility, required certification or claim (organic, MSC, Finnish
  origin), maximum unit price, supplier allow/deny list.
- **R-2 Sources:** constraints come from the customer profile, tenant policy (spec 44 layers), the intent and the
  basket line, each with provenance. The strictest applies; a looser layer cannot relax a stricter one.
- **R-3 One evaluation point:** lookup, substitution, in-time alternatives, proposals and quotes call the engine before
  ranking. Results per offer: `eligible`, or `excluded {constraint, layer, reason, missing_fields}`, or
  `needs_approval {…}`. `GET /v1/offers/{lookup_id}/eligibility` shows every decision. Human approval cannot override
  `exclude` outcomes of safety or contract constraints.
- **R-4 Lots:** offers may carry lots `{lot_id, use_by|best_before, quantity}`; the engine picks lots that satisfy the
  shelf-life constraint for the consumption date, or excludes the offer and proposes a later delivery or another lot.
- **R-5 Unsupported constraints:** an intent with a mandatory constraint the engine cannot evaluate answers
  `422 CONSTRAINT_UNSUPPORTED` naming it, unless the customer marks it advisory; the gateway never silently ignores it.
- **R-6 Framework agreements:** versioned agreement fixtures (`backend/agreements/*.yaml`: customers, categories,
  suppliers, validity, `fixed_priority` supplier list) are one constraint type; quotes and orders carry the agreement id
  and version. Seed one agreement for the hospital kitchen preferring Valio over the synthetic supplier for dairy.
- **R-7 Adding a type is configuration:** a new constraint type is a registry entry plus an evaluator function; no
  changes in lookup, ranking or quoting code.
- **R-8 Conflict is missing:** when sources disagree on a fact a constraint needs (spec 54 R-5), the constraint's missing-data outcome applies, with `conflicting_sources`.
- **R-9 Substitutes are new offers:** every supplier- or agent-proposed replacement is evaluated from scratch against the original line's constraints and functional need; built-in type `preparation_state` against the customer's `kitchen_capabilities`.
- **R-10 Basket-level constraints:** the registry accepts basket-scope evaluators; built-in `purpose_plausibility` (per-portion quantity bands by ingredient role against declared covers) gives `needs_approval` with the expected band.
- **R-11 Volume commitments:** an agreement may declare `volume_commitment {period, amount}`; a quote moving agreement-category spend elsewhere shows `contract_effect {committed, projected, shortfall}` and needs approval on a projected shortfall.

## Acceptance criteria
- **AC-1** For a Thursday consumption date, the cheapest dairy lot with a Wednesday use-by is excluded with the
  shelf-life reason, and a suitable lot or later delivery is offered.
- **AC-2** A hospital lookup excludes the cheaper synthetic-supplier dairy offer with the agreement id and version; an
  approver cannot order it.
- **AC-3** A product without nutrition data is excluded under a nutrient limit with `missing_fields`.
- **AC-4** A mandatory "Finnish origin" constraint on a product without origin data excludes it; an unknown constraint
  type answers `CONSTRAINT_UNSUPPORTED`.
- **AC-5** A test-registered constraint type (e.g. maximum CO2 per kg) takes effect in lookup and quote without changes
  outside the registry.
- **AC-6** A tenant policy cannot loosen a customer allergen exclusion.
- **AC-7** A supplier substitute that needs preparation the kitchen lacks (raw vs pre-cooked) is excluded via `preparation_state`.
- **AC-8** A basket of 40 kg of salt for 50 covers passes every per-line rule but gets `needs_approval` from `purpose_plausibility` with the expected band.
- **AC-9** A cheaper basket that moves dairy spend away from the framework supplier shows the projected volume shortfall and needs approval.
