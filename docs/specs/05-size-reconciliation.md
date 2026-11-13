# 05 — Size reconciliation (Core 1 quality)

Status: draft → in progress · Found in the frontend review on 2026-11-14.

## Purpose
"1,000 litres" must become the right number of packs. Today the size per sales unit comes first from the live
price ratio (`catalogPrice / comparisonPrice`), which carries rounding noise: 5.01 L per case instead of 5, 19.94
instead of 20. The result is one pack too many (201 cases instead of 200, 51 instead of 50) and odd numbers on
screen. Judged under "Solving the user need" (20%): how precisely the answer matches the request.

## Requirements
- **R-1 Three sources:** compute every available size per sales unit: `catalog` (base volume / net weight ×
  sales-unit multiplier), `name_parse` (size in the product name × multiplier) and `price_ratio`.
- **R-2 Prefer exact, confirmed by price:** when `catalog` or `name_parse` exists and the price ratio is within
  3 % of it (or no price ratio exists), use the exact catalog / name value. `size_source` stays `catalog` or
  `name_parse`; a new field `size_confirmed_by_price` (bool) says whether the live ratio agreed.
- **R-3 Catalog beats name** when both exist and agree within 3 %.
- **R-4 Disagreement is visible:** when an exact source and the price ratio differ by more than 3 %, use the
  price ratio (it reflects what is actually invoiced) and add warning `SIZE_MISMATCH` with both values in the
  text. `SIZE_MISMATCH` is a known warning code and is in the default `warnings_need_approval`.
- **R-5 Ratio only:** with no exact source, keep the price ratio as today.
- **R-6 No silent change elsewhere:** `units_needed`, totals and coverage follow the reconciled size; quotes keep
  their explicit `quantity_sales_units`.

## Acceptance criteria
- **AC-1** Name "5 x … 1 L", multiplier 5, price ratio 4.994 → size 5.0, source `name_parse`,
  `size_confirmed_by_price` true, 1,000 L → 200 units.
- **AC-2** Name "… 10 l novobox", multiplier 2, price ratio 19.94 → size 20.0, 1,000 L → 50 units.
- **AC-3** Catalog volume 1 L × multiplier 12 and price ratio 11.98 → size 12.0, source `catalog`.
- **AC-4** Name says 1 L × 5 but price ratio is 2.5 → size 2.5 from `price_ratio`, warning `SIZE_MISMATCH`
  naming both values; with the default policy the order needs approval.
- **AC-5** No catalog volume and no size in the name → price ratio used, no new warning (unchanged behaviour).
- **AC-6** No price ratio (price error) but a name size → name size used, `size_confirmed_by_price` false.
- **AC-7** `GET /v1/policy` lists `SIZE_MISMATCH` in `known_warning_codes`; the contract is regenerated.
- **AC-8** Live check: the 1,000 L milk lookup shows whole sizes (5, 20, 12 L) and 200 / 50 / 84 units.
