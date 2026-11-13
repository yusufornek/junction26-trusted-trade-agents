# 01 — Lookup (Core 1)

Status: built · Challenge text: "a structured way for an external agent to search for and retrieve what's
available and at what price."

## Purpose
An outside buyer agent sends a structured need and gets back ranked, explained offers with live price and stock.

## Requirements
- **R-1 Interfaces:** MCP tools `search_products` (catalog only) and `find_offers`; REST `POST /v1/offers`,
  `GET /v1/products/{id}`, `GET /v1/lookups/{lookup_id}`. One core behind both.
- **R-2 Structured need:** `customer_id`, `intent`, `search_terms`, `required_claims`, `exclude_allergens`,
  `quantity {value, unit: L | KG | SALES_UNIT}`, `deliver_by`, `max_candidates` (≤ 10). The buyer agent turns the
  buyer's sentence into this; the gateway is deterministic.
- **R-3 Hard filters first:** required claims and excluded `CONTAINS` allergens are checked on catalog facts;
  products that fail never become offers. `excluded_count` reports how many were removed.
- **R-4 Screen, then query live:** a catalog pool (`shortlist.catalog_pool`, default 40) is scored by the
  Decisions provider in one call; only the best `max_candidates` go to Valio — one `/api/price` and one
  `/api/availability` call per lookup ("query only when needed").
- **R-5 Offer card:** size per sales unit and its source, units needed, unit / per-L-or-KG / total price,
  availability (amount by the deadline, logistic model, closing time, coverage), warnings, delivery
  reliability, CO2, labels, `matched_because`, `fit` + `fit_question`, provenance (source, fetched-at, valid-to).
- **R-6 Warnings:** `PARTIAL_STOCK`, `DATE_SHIFT`, `CUTOFF_PASSED`, `SALES_BLOCK`, `UNKNOWN_PRODUCT`,
  `PRICE_ERROR`, `SIZE_UNKNOWN`, `NO_FIT_SCORE`, `AVAILABILITY_ERROR` — each with code and plain text.
- **R-7 Ranking:** weighted score (price, coverage, reliability, CO2, fit; weights from policy), top three
  labelled best / better / good.
- **R-8 Degrade visibly:** missing Decisions key or upstream failure produces warnings, never a crash or a
  silent guess.
- **R-9 Evidence:** each lookup is stored (`lookup_id`) and audited with inputs, fit scores and live-call counts.

## Acceptance criteria → tests (`backend/tests/`)
| AC | Given / when / then | Test |
|---|---|---|
| AC-1 | Catalog built → lactose-free filter and allergen exclusion remove the right products | `test_catalog_build_and_hard_filters` |
| AC-2 | Size from price ratio, catalog volume, name parse; unknown size flagged | `test_size_paths`, `test_name_parse_kg_and_lot_rounding`, `test_unknown_size_and_price_flags` |
| AC-3 | Date shift, partial stock, passed cut-off, unknown product, sales block are flagged | `test_warning_flags` |
| AC-4 | One batched price + availability call per lookup; cache keyed by customer/date | `test_lookup_batches_filters_and_cache`, `test_cache_key_date_customer_and_expiry` |
| AC-5 | Decisions screens the whole pool; only the shortlist goes live | `test_decisions_screen_pool_before_live_calls` |
| AC-6 | No Decisions key / failed call → `NO_FIT_SCORE`, lookup still answers | `test_no_decisions_key_degrades`, `test_decisions_failure_degrades` |
| AC-7 | Upstream failure is visible as a warning | `test_upstream_failure_is_visible` |
| AC-8 | Stored lookup is retrievable; unknown id → 404 | `test_lookup_fetch_and_not_found` |
| AC-9 | Ranking weights and pool size follow the policy | `test_policy_applies_shortlist_and_ranking` |
| AC-10 | Same result over MCP (stdio and HTTP) | `test_mcp_tools_use_shared_core`, `test_stdio_transport`, `test_streamable_http_transport` |

Verified live 2026-11-13: 1,000 L lactose-free milk → 40 screened, 1 + 1 Valio calls, best offer 1,010 €.

## Open points for the review
- Size from the price ratio is noisy (5.01 L instead of 5) → [05 — Size reconciliation](05-size-reconciliation.md).
- Catalog search is keyword based; a category hint or embeddings could widen recall.
