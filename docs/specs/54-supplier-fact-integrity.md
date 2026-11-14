# 54 — Supplier fact integrity and freshness

Status: in progress · Decided on 2026-11-14 with Gökalp (capability spec, see spec 47; drafted by Fable from Astra's
unexpected-situation review). Builds on specs 05, 15, 25, 35 and 49.

## Purpose
Everything the gateway knows from a supplier is a fact with a source, a time, a unit basis and an identity. Malformed,
implausible, stale, conflicting or re-identified facts are caught at the adapter boundary and become "unknown", so they
reach no decision.

## Requirements
- **R-1 Response contract:** every adapter response is validated (content type, schema, required fields, declared
  locale and number format). A failure is `SUPPLIER_RESPONSE_INVALID`, is kept out of every cache, and answers
  `503 SUPPLIER_DATA_UNAVAILABLE` (retryable) with the age of the last good snapshot.
- **R-2 Staged refresh:** a catalogue refresh is staged and applied only when the batch passes: reject rate and
  row-count change within thresholds, per-field numeric change within bounds against the previous snapshot, and
  cross-field checks (unit price × pack content ≈ pack price; nutrients per 100 g sum ≤ 100). Failing records are
  quarantined with the reason (spec 48 R-5); a failing batch keeps the last good snapshot and raises
  `CATALOG_REFRESH_REJECTED`.
- **R-3 Product fingerprint:** each `(supplier_id, sku)` has an identity digest (GTIN, sale unit, pack content, net
  weight) and a formulation digest (ingredients, allergens, nutrition). Quotes, approvals, proposals and recurring
  baselines store both. An identity change at a later step answers `409 PRODUCT_IDENTITY_CHANGED` with
  `LOOKUP_REQUIRED`; a formulation change creates a new evidence revision (spec 17) and recurring drift (spec 35). A
  successor is a `SUCCESSOR_LINKED` event; history stays with the old fingerprint. Listings stay keyed by supplier SKU
  and sale unit; grouping by GTIN is display only.
- **R-4 Typed quantities:** every numeric fact carries unit and basis (`per_100g_as_sold`, `per_100g_prepared`,
  `per_serving`, `each`, `case`). Comparison and constraint evaluation use one basis; conversion needs a declared
  factor; a value without basis counts as missing.
- **R-5 Source agreement:** facts available from several sources (language variants, structured field vs text, two
  feeds) are compared at ingest, with the allergen vocabulary scanned in every language. A disagreement sets the fact to
  `conflicting {sources, values}`, which constraints treat as missing.
- **R-6 Freshness:** every fact has `observed_at` and a maximum age per class (price, stock, lot dates, credit,
  catalogue) in policy. A state-changing step refreshes older facts or treats them as unknown; responses show the age.
- **R-7 Time authority:** the gateway clock decides validity. Signed artefacts carry UTC `iat` and `exp`; one leeway
  published in `/v1/meta` is used by gateway and verifier. Supplier validity windows are shortened by a safety margin. A
  step started inside validity completes or answers `EXPIRED_QUOTE` with `REFRESH_QUOTE`. A periodic check against an
  external time source sets `CLOCK_DRIFT` and pauses signing above the threshold.
- **R-8 Sandbox faults:** HTML-200 response, decimal-shifted export, reused SKU, successor redirect, skewed clock.

## Acceptance criteria
- **AC-1** The sandbox supplier returns 200 with an HTML login page: lookup answers `SUPPLIER_DATA_UNAVAILABLE` with
  the snapshot age, and nothing is cached.
- **AC-2** An export where `1.250` becomes `1,250` is rejected by the numeric-change gate; prices stay at the last good
  snapshot and `CATALOG_REFRESH_REJECTED` is raised.
- **AC-3** A SKU reloaded as a 12-pack instead of a single pack: ordering the earlier approved quote answers
  `PRODUCT_IDENTITY_CHANGED`.
- **AC-4** A recurring order whose product redirects to a reformulated successor stops for approval with the
  formulation difference; the old approval is not inherited.
- **AC-5** A product whose Finnish text names milk while the Swedish text and structured field omit it is
  `conflicting` and excluded under a milk exclusion.
- **AC-6** Dry-concentrate and prepared-serving sodium values are kept apart; a per-100 g-as-sold limit is evaluated
  only on the as-sold value, or is missing.
- **AC-7** A single-pack and a case listing sharing a manufacturer code stay two offers with their own price and stock.
- **AC-8** With the verifier clock 90 s ahead and a 120 s leeway the receipt verifies; a quote inside the supplier
  margin answers `REFRESH_QUOTE` before approval.
