# 15 — Evidence completeness: missing data never counts as proof

Status: in progress · Decided on 2026-11-14 · Source: a missing-data audit of every pipeline stage (catalog build,
filters, ranking, upstream responses, orders and policy, request inputs), each gap reproduced by a test.

## Purpose
The gateway tells an outside agent and a human approver that a product fits, is priced, is in stock and covers the
request. Every such statement rests on a data field. When the field is missing, empty, contradictory or carries an
unknown code, the statement is unproven: the gateway says so, and the decision falls to a human or the product is
left out. Known facts from the audit (17,546 products): 47 % have no allergen data at all; 9,956 "may contain"
and 303 "undeclared" allergen entries were dropped at build; 75 % have no delivery-reliability value and 37 % no
CO2 value; 24 % of live price rows have no comparison price, so the pack size cannot be confirmed by price; some
catalog sizes are wrong by a factor of 1,000 (e.g. a 20 L soy sauce stored as 20,000 L).

## Requirements

### Allergens (rule agreed on 2026-11-14)
- **R-1 Full allergen status at build:** the catalog stores, per product, each allergen code with its status
  `contains`, `may_contain`, `undeclared` or `free_from` (from `nonAllergen`), plus `has_allergen_declaration`
  (true when the product has any allergen entry with status contains / may_contain / undeclared, or the GS1
  marker `X99` "no declarable allergens"). `X99` is a declaration marker, not an allergen.
- **R-2 Exclusion passes on evidence only:** a product passes `exclude_allergens` code C when
  (a) C is declared `free_from` and no entry for C says contains / may_contain / undeclared → evidence text
  "C declared free-from (catalog nonAllergen)"; or (b) the product has an allergen declaration and has no entry for
  C → evidence text "C not listed in the catalog allergen declaration". Every other case removes the product:
  `contains`, `may_contain`, `undeclared`, conflicting entries, or no allergen data at all. Removal counts per
  reason appear in `no_match_reason` / exclusion counts ("no allergen data: 812", "may contain: 40").
- **R-3 Claim contradictions:** a required claim counts as proven only when the same record does not contradict
  it. Map: FREE_FROM_LACTOSE ↔ ML, FREE_FROM_GLUTEN ↔ AW (and the gluten cereals UW, GB, NR, GS, GK),
  FREE_FROM_EGGS ↔ AE, FREE_FROM_SOY ↔ AY, FREE_FROM_WHEAT ↔ UW, VEGAN ↔ AM, ML, AE, AF, AC, AN (milk, lactose,
  egg, fish, crustaceans, molluscs) with status contains or may_contain. A contradicted claim does not satisfy
  `required_claims`; the product is removed with reason "claim contradicted by allergen data".

### Codes and request inputs
- **R-4 Known vocabularies:** allergen, claim and label codes are normalised (trim, upper case for allergen and
  claim codes, case-insensitive match for labels) and checked against the catalog vocabulary computed at build.
  An unknown code in `required_claims`, `exclude_allergens`, `preferences.labels` or the customer policy of spec 16
  answers `422 UNKNOWN_CODE` naming the field, the code and the valid values.
- **R-5 Request bounds:** `customer_id` is required and non-blank (no default); `deliver_by` lies between today
  and today + 60 days (Europe/Helsinki); `quantity.value` and `quantity_sales_units` are > 0 and ≤ 1,000,000;
  numbers are strict (no booleans or numeric strings); a blank brand preference entry is refused; `pack_size.min`
  ≤ `pack_size.max`. Violations answer `422` with the field named, over REST and MCP alike (MCP returns the same
  JSON `{code, message}` as a tool error).
- **R-6 Honest fit question:** the sentence "hard constraints are already verified" appears in the Decisions
  question only when the request carries hard constraints.

### Price, size, stock and dates from the upstream
- **R-7 Price:** a net price that is missing, zero or negative, or a row without currency, gives `PRICE_ERROR`
  (no EUR default). A product with a variable sales unit (`variableUnit`, weighed goods) carries
  `VARIABLE_WEIGHT` ("final price follows the delivered weight"); a product with deposits carries `DEPOSIT`
  with the deposit details. Both are informational.
- **R-8 Size confirmation:** when the pack size comes from the catalog or the product name and no live price
  ratio confirms it (comparison price missing or in another unit), the offer carries `SIZE_UNCONFIRMED`, which
  needs approval. At build, sizes outside plausible bounds (volume > 100 L or weight > 100 kg or < 1 g per base
  unit, or catalog vs. name size differing by a factor ≥ 10) are stored as suspect; a suspect size is not used
  (the offer falls back to the name size or `SIZE_UNKNOWN`). The build also converts MLT / CLT volumes and uses
  the CASE record when the case is the sales unit.
- **R-9 Units:** an availability schedule line whose `salesUnit` is missing or differs from the catalog sales unit
  gives `UNIT_MISMATCH` (needs approval) and its amount counts as 0.
- **R-10 Stock:** an unknown or missing `logisticModel` is treated as finite stock with unknown amount: local
  reservations are subtracted and the offer carries `STOCK_MODEL_UNKNOWN` (needs approval). A Stock-model
  product with zero available and no date carries `NO_STOCK` (reject).
- **R-11 Cut-off:** a missing or unparseable `closingTime` gives `CUTOFF_UNKNOWN` (needs approval); a cut-off
  without a UTC offset is read in Europe/Helsinki time.

### Ranking and policy
- **R-12 Orderable first:** offers carrying a reject code or SIZE_UNKNOWN / PRICE_ERROR / AVAILABILITY_ERROR /
  UNIT_MISMATCH rank after all others and receive no `rank_label`; plans are built from all ranked offers, not
  only the returned slice.
- **R-13 Missing ranking data is neutral:** delivery reliability is scored on its absolute 0–1 value and CO2 on
  the catalog-wide range; a missing value scores the catalog median of that factor; the offer text says
  "reliability: no data (neutral)". Offers without a fit answer rank after offers with one.
- **R-14 Policy fails closed:** every warning code on a line or plan that is not in `reject.on` and not in a new
  `approval.ignored_warnings` list (default empty) requires approval. `PUT /v1/policy` refuses (`422`) a policy
  where a protected code (PRICE_ERROR, AVAILABILITY_ERROR, SIZE_UNKNOWN, SIZE_UNCONFIRMED, UNIT_MISMATCH,
  STOCK_MODEL_UNKNOWN, NO_FIT_SCORE, CUTOFF_UNKNOWN) is in `ignored_warnings`; `reject.on_hard_constraint_violation:
  false` turns a hard violation into an approval case. The runtime policy gains new default codes on load.
- **R-15 Approval re-checks scope:** approval evaluates the order's customer against the agent's mandate.
- **R-16 Catalog build fails loudly:** a missing or renamed reliability / CO2 file or column stops the build with
  a clear message; products with `deleted: true` are left out; the build prints a coverage report (per field:
  filled / empty) and stores it, `GET /health` shows the totals.

## Acceptance criteria
- **AC-1** `exclude_allergens: [AE]`: a product with no allergen data is removed (counted as "no allergen data");
  a product with only `AM contains` passes with "AE not listed in the catalog allergen declaration"; a product
  with `AE free_from` passes with "AE declared free-from"; a product with `AE may_contain` is removed.
- **AC-2** A product with `X99` only passes an exclusion of AE with the "not listed" evidence and is not treated
  as containing an allergen called X99.
- **AC-3** `required_claims: [FREE_FROM_LACTOSE]` and the product also has `ML contains` → removed as "claim
  contradicted by allergen data".
- **AC-4** `exclude_allergens: ["ae"]` is normalised to AE; `exclude_allergens: ["EGG"]` → `422 UNKNOWN_CODE`
  listing valid codes; the same for an unknown claim and an unknown label.
- **AC-5** `deliver_by` yesterday → 422; `quantity.value: 0` → 422; `quantity.value: 2e6` → 422; missing
  `customer_id` → 422; the same refusal over MCP is a JSON tool error with the code.
- **AC-6** Net price 0 → `PRICE_ERROR`, no quote can be created for it.
- **AC-7** Name-sized product without a comparison price → `SIZE_UNCONFIRMED`; its plan preview is
  `PENDING_APPROVAL`. A product confirmed by the price ratio carries no such warning.
- **AC-8** A catalog size of 20,000 L for a product named "… 20 l" is stored as suspect and the offer uses 20 L
  from the name (with `SIZE_UNCONFIRMED` when no price ratio confirms it).
- **AC-9** Schedule line `salesUnit` ST for a product sold in KI → `UNIT_MISMATCH`, covered amount 0.
- **AC-10** `logisticModel` missing → `STOCK_MODEL_UNKNOWN`, reservations subtracted; Stock with 0 and no date
  → `NO_STOCK`, order `REJECTED`.
- **AC-11** `closingTime` missing → `CUTOFF_UNKNOWN`, order needs approval.
- **AC-12** A cheap offer with `UNKNOWN_PRODUCT` ranks after the orderable offers and has no `rank_label`.
- **AC-13** Two offers equal except reliability: one 0.0, the other no data → the no-data offer ranks higher on
  that factor (neutral median beats known-worst); a sole known value does not receive the full factor.
- **AC-14** `PUT /v1/policy` with empty `warnings_need_approval` → a line with `PARTIAL_STOCK` still needs
  approval; `ignored_warnings: [PRICE_ERROR]` → 422.
- **AC-15** Catalog build with a missing CO2 file → exits with an error naming the file; a source record with
  `deleted: true` is not in the catalog; the coverage report lists each field.
- **AC-16** The live demo request (1,000 L lactose-free milk, search term "milk", customer "1") still returns
  464306 as best at 1,010 € and confirms within the `web-console` mandate.

## For the review
Strict reading chosen for exclusions; the "not listed" rule relies on the EU duty to declare the 14 major
allergens. The informational codes (VARIABLE_WEIGHT, DEPOSIT, RETRIEVAL_BROADENED, POOL_TRUNCATED) go into
`approval.ignored_warnings` in the default policy.
