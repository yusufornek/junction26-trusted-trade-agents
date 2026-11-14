# 31 — Allergen-safe substitution for a hospital kitchen

Status: in progress · Decided on 2026-11-14 · Design reviewed with the external architect (round 4).

## Purpose
The brief's scenario: the usual product is out of stock and a hospital kitchen needs a substitute that is safe
under its recorded allergen rules. The gateway finds a functionally suitable alternative on evidence and shows
that evidence.

## Requirements
- **R-1 Seeded customer:** a hospital customer (sandbox and policy example) with standing `exclude_allergens`
  and an approved substitution profile `{allowed_brands?, max_price_increase_pct, pack_size_range,
  requires_approval: true|false}`.
- **R-2 Functional need:** `NeedSpec.function {product_type, intended_use, preparation?, acceptable_units[]}`
  (e.g. oat drink, for cooking, UHT). Candidates must match the product type through catalog category and the
  fit screening; the fit question includes the intended use.
- **R-3 Substitution request:** `NeedSpec.substitute_for {product_id}`: the lookup returns the preferred product
  first when available; when it is short, alternatives are ranked by closeness (same category, claims superset,
  pack size, price) and each carries `substitution {for, differences[], allergen_evidence[]}`.
- **R-4 Evidence rule:** only products that pass spec 15's allergen rule are offered; every alternative lists per
  excluded allergen which evidence qualified it (declared free-from / not listed in a complete declaration).
- **R-5 Wording:** results are described as "compliant with the recorded procurement constraints"; the order
  carries `clinical_review: required` when the customer profile says so, which forces approval by a
  buyer_approver.
- **R-6 Scenario:** sandbox scenario `hospital_stockout` and `backend/demo/run_hospital_substitution.py`.

## Acceptance criteria
- **AC-1** Preferred product out of stock → an alternative of the same product type with qualifying evidence for
  every excluded allergen is offered, with the differences listed.
- **AC-2** A cheaper candidate with a may-contain entry for an excluded allergen is not offered and is counted
  in the exclusion reasons.
- **AC-3** A lactose-free product that contains milk allergen is excluded when milk (AM) is an excluded allergen.
- **AC-4** A candidate without allergen data is not offered.
- **AC-5** The substitution order goes to approval with `clinical_review: required`; an operator-only approval
  is refused when the profile requires a buyer approver.
- **AC-6** With the preferred product in stock, it is the best offer and no substitution is proposed.
- **AC-7** Price increase above the profile's limit → the alternative is flagged and needs approval.
