# 32 — Recipe to order: ingredient baskets

Status: in progress · Decided on 2026-11-14 · Design reviewed with the external architect (round 4).

## Purpose
The brief's "need → recipe → order": the buyer's agent turns a recipe into ingredients; the gateway turns the
ingredient list into one purchasable, policy-checked basket.

## Requirements
- **R-1 Ingredient manifest (agent-supplied, versioned):** `{recipe_name, servings, ingredients[{name,
  quantity{value, unit}, search_terms?, required_claims?, exclude_allergens?, substitutions_allowed}],
  yield_note?}`.
- **R-2 `POST /v1/basket-lookups` / MCP `find_basket_offers`:** each ingredient becomes a need with the
  customer's standing constraints unioned in; candidate SKUs are deduplicated across ingredients; supplier price
  and availability are fetched in one batched call each; per ingredient the result holds ranked offers, the
  chosen line, purchasable quantity, overbuy and coverage.
- **R-3 Basket plan:** `basket {lines[], total, coverage_by_ingredient, uncovered[], policy_preview}`; an
  ingredient covered by the same SKU as another shares one line with summed quantity.
- **R-4 Basket quote and order:** `POST /v1/quotes` accepts `basket_ref {basket_lookup_id}`; one quote, one
  order, reservations for all lines commit atomically in simulation; the order states the adapter's atomicity.
- **R-5 Scaling:** `servings` scales ingredient quantities; the response shows the scaled manifest.
- **R-6 Units:** mass ↔ volume conversions happen only with density evidence from the catalog or the manifest;
  otherwise the ingredient is `uncovered` with reason "unit cannot be converted".
- **R-7 Demo:** `backend/demo/run_recipe_order.py` (lasagne for 200) with the decomposition done by the agent
  side; a fixture manifest for tests.

## Acceptance criteria
- **AC-1** A 5-ingredient manifest returns one basket with a line per distinct SKU and coverage for each
  ingredient; two supplier calls in total.
- **AC-2** Doubling servings changes quantities, pack rounding, overbuy, total and — past the limit — the policy
  preview from CONFIRMED to PENDING_APPROVAL.
- **AC-3** Two ingredients satisfied by the same SKU share one line.
- **AC-4** An ingredient with no compliant product is listed in `uncovered` and the basket preview needs
  approval (partial basket).
- **AC-5** A standing allergen exclusion removes a candidate for every ingredient.
- **AC-6** Basket quote → order reserves all lines or none.
- **AC-7** A grams-to-litres ingredient without density evidence is `uncovered` with the reason.
