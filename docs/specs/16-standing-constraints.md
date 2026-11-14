# 16 — Buyer standing constraints and bound quotes

Status: in progress · Decided on 2026-11-14 after the external architecture review.

## Purpose
Today the gateway enforces the hard constraints the agent itself wrote into its request. An agent that points
its quote at a weaker request (or at none) gets a product the buyer organisation would never accept. The buyer
organisation states its own standing constraints once, per customer; they apply to every lookup, quote, order
and approval made on its behalf, whatever the agent asks for.

## Requirements
- **R-1 Customer policy:** new policy section `customers`: a map `customer_id → {required_claims[],
  exclude_allergens[], note}`. Default in `backend/policy.yaml`: an empty map with a commented example.
- **R-2 Effective constraints:** for a request, `required_claims` and `exclude_allergens` are the union of the
  customer's standing constraints and the request's own. The union is applied wherever the request's constraints
  are applied today: catalog filter in the lookup, hard-violation check at quote, at order placement and at
  approval.
- **R-3 Visible source:** `LookupResult.effective_constraints` lists each constraint with its source
  (`request` or `customer_policy`); `matched_because[]` and rule hits name the source the same way.
- **R-4 Current policy wins:** the customer's constraints are read at the moment of each step. A constraint
  added after a quote was created applies when that quote is ordered and when a pending order is approved.
- **R-5 Every quote is bound to a stored request:** `create_quote` takes `plan_ref` or `lines` together with
  `need_ref`. Explicit lines without `need_ref` answer `422 NEED_REF_REQUIRED`.
- **R-6 Governed like the rest of the policy:** `GET /v1/policy` returns `customers`; `PUT /v1/policy` accepts
  it (codes validated against the catalog vocabulary, see spec 15) and audits changes in the `POLICY_UPDATED`
  diff. A `PUT` body without `customers` keeps the current map.

## Acceptance criteria
- **AC-1** Customer "1" has `required_claims: [FREE_FROM_LACTOSE]`. A lookup whose request has no claims returns
  only lactose-free products; `effective_constraints` shows the claim with source `customer_policy`.
- **AC-2** Same customer policy: an agent quotes explicit lines for a product without the claim, with a
  `need_ref` to its own constraint-free lookup → the quote carries the hard violation, the order is `REJECTED`
  and cannot be approved.
- **AC-3** Explicit lines without `need_ref` → `422 NEED_REF_REQUIRED`; `plan_ref` alone works.
- **AC-4** A quote is created, then the customer policy gains an excluded allergen the quoted product contains
  → `place_order` gives `REJECTED` with the hit naming `customer_policy`.
- **AC-5** A pending order, then the customer policy gains a constraint the order violates → approve is refused
  with the rule hits; reject works.
- **AC-6** `PUT /v1/policy` without `customers` keeps the map; a change shows in the audit diff; an unknown code
  → `422`.
- **AC-7** Request and customer policy both exclude allergens → the union applies (a product excluded by either
  is removed) and each removal is counted in `no_match_reason` / exclusion counts.

## For the review
Defaults: the customer map starts empty; the demo sets one for the hotel chain. Budgets stay per agent; an
organisation-wide spend cap is a later step.
