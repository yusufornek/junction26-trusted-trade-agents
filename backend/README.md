# Trusted Trade Agents backend MVP

One Python core serves FastAPI and the MCP 2.3 server. It searches the local catalog, screens a
40-product pool with Decisions, checks live Valio price/availability for suitable candidates (up to 25 by default), locks fixed-price quotes, then
runs the approval policy. The upstream mock has no search or order endpoint: quotes, orders,
approvals and reservations exist only in this gateway.

## Commands

No installation is needed in the supplied environment. From the **repository root**:

```sh
cd backend
.venv/bin/python scripts/gen_keys.py --write
.venv/bin/python -m app.catalog build
.venv/bin/python -m uvicorn app.api:app --host 127.0.0.1 --port 8000
```

In separate terminals, starting in `backend/`:

```sh
# MCP stdio; stdout is reserved for protocol messages
.venv/bin/python -m app.mcp_server --agent-id hotel-chain-agent

# MCP Streamable HTTP: http://127.0.0.1:8001/mcp
.venv/bin/python -m app.mcp_server --agent-id hotel-chain-agent --http --host 127.0.0.1 --port 8001

# Entire offline suite, with Valio/OpenAI mocked using respx
.venv/bin/python -m pytest tests -q

# Regenerate the shared frontend contract; no startup or credentials required
.venv/bin/python scripts/export_openapi.py

# Archive demo state and rebuild a clean catalog (stop the API first)
.venv/bin/python scripts/reset_demo.py
```

The build reads the unchanged gzip/CSV files in `../data/` and writes `backend/var/app.db` (gitignored).
Rebuilding replaces catalog/FTS rows and catalog metadata, retaining orders and audit. Deleted products
are skipped. Missing reliability/CO2 files or required columns stop the build with a named error.
The JSON build report prints `rows`, `deleted_skipped`, `seconds` and `coverage` with filled/empty counts
for every catalog field. Coverage, code vocabularies and global ranking statistics are stored in SQLite;
`GET /health` returns the row count and coverage. Rebuild before using the new evidence model.
For an isolated rebuild use `--db /tmp/catalog-check.db`; fixture builds may also supply `--source`,
`--reliability` and `--carbon`. API docs: `http://127.0.0.1:8000/docs`.

`scripts/reset_demo.py` moves the database, any WAL/SHM sidecars and runtime policy into a new
`var/archive/YYYYMMDD-HHMMSS/` folder, then rebuilds the catalog with empty transaction/audit state.
The effective policy returns to `policy.yaml` defaults. Every earlier archive is preserved.
The script refuses an answering API port (default 8000); use `--port PORT` for another port or
`--force` to continue anyway. It prints the archive path, product count and API start command.

Startup loads `VALIO_API_BASE`, `VALIO_API_KEY`, `OPENAI_API_KEY`, `AGENT_KEYS`, `OPERATOR_TOKEN`, and `OPERATOR_AGENT_ID` from repo-root `.env.local`
if present. Existing environment variables take precedence. The parser accepts comments, optional
`export`, and quoted values; it does not execute shell code. See `backend/.env.example` for variable
names. No keys are printed. A missing Decisions key yields `NO_FIT_SCORE` and human approval;
missing Valio configuration produces visible unavailable evidence and prevents quoting.

For a stdio MCP client, set its executable to the **absolute** `backend/.venv/bin/python` path,
arguments to `["-m", "app.mcp_server", "--agent-id", "hotel-chain-agent"]`, and working directory to the absolute `backend/` path.
Tools: `search_products`, `find_offers`, `create_quote`, `place_order`, `get_order_status`, `cancel_order`, `get_order_ubl`.
`cancel_order(order_id, reason="")` cancels only confirmed orders placed by the server's `--agent-id`,
before every known line cutoff. Lines without a cutoff do not block. Cancellation releases local
reservations, preserves fixed prices, and records the actor and reason without upstream calls.
The SDK import is `mcp.server.mcpserver.MCPServer`; both transports use the installed 2.x API.

## Demo flow

1. Call `find_offers` (MCP), or `POST /v1/offers`, with this structured need:

```json
{
  "customer_id": "1",
  "intent": "1000 litres of lactose-free milk for a hotel",
  "search_terms": ["milk"],
  "required_claims": ["FREE_FROM_LACTOSE"],
  "exclude_allergens": ["AE"],
  "quantity": {"value": 1000, "unit": "L"},
  "deliver_by": "2026-11-16",
  "max_candidates": 10
}
```

2. Select an offer or a fulfilment plan (see below). Call `create_quote`, or `POST /v1/quotes`, with `customer_id`, `deliver_by`,
   `need_ref` set to the returned `lookup_id`, and `lines` containing its `product_id` and
   `quantity_sales_units` set to `units_needed`. Multiple explicit lines are supported. The gateway
   stores lookups and retrieves fit/constraints itself; clients cannot inject a trusted fit score.
   Alternatively, omit `lines` and send `plan_ref: {"lookup_id": "...", "plan_index": 0}`;
   `need_ref` then defaults to that lookup. Exactly one of `lines` or `plan_ref` is required.
   Customer and delivery date must match the lookup. Invalid plan indices return 422.
   Explicit lines without `need_ref` return `422 NEED_REF_REQUIRED`; every new quote binds a stored need.
3. Call `place_order(quote_id, idempotency_key)`. REST: `POST /v1/orders` with `{"quote_id":"..."}`,
   headers `Idempotency-Key` and `Authorization: Bearer <agent secret>` for a registered agent such as
   `hotel-chain-agent`. Lookup, quote and order require a mandate (`403 AGENT_NOT_REGISTERED`). Fixed prices remain the quoted prices. Repeated keys return the existing order;
   reuse with a changed command payload by that principal is a conflict. Another principal may use the same key string.
4. If `PENDING_APPROVAL`, inspect `GET /v1/approvals`. A human calls
   `POST /v1/approvals/{order_id}/approve` or `/reject`, body
   `{"approver":"Ada","comment":"Reviewed delivery and budget","evidence_revision":1}`.
   Approval is REST-only. Every order starts at `evidence_revision: 1`; the revision in the body is optional.
   A stale stated revision returns `409 EVIDENCE_CHANGED` with the current revision before any upstream call.
   Approval refreshes stock and policy and compares decision evidence: each line's quantity, covered sales
   units, delivery date and warning codes, order-level warning codes, and policy rule names. Fetch timestamps,
   warning texts and rule details do not change that evidence. Changed evidence stays `PENDING_APPROVAL`,
   stores the refreshed lines/policy/warnings, increments the revision, and returns
   `409 EVIDENCE_CHANGED` with `data {evidence_revision, changes[]}` in plain words. Review the refreshed
   order before approving again. Unchanged evidence confirms; hard failures and unverifiable stock still
   prevent confirmation. The timeline includes an `EVIDENCE_CHANGED` decision step with its actual calls.
5. Read `/v1/orders/{id}`, `/v1/quotes/{id}`, `/v1/orders`, and
   `/v1/audit?order_id=...&quote_id=...`. When both audit filters are supplied, they combine with AND.

The offline full-flow tests exercise 1,000 L → 200 sales units at 13 EUR each → 2,600 EUR quote →
`PENDING_APPROVAL` → human approval → `CONFIRMED`, over both REST and MCP core tools.

## Frontend control plane

- `GET /v1/orders/{order_id}/timeline` returns the complete order story: the lookup referenced by the
  quote's `need_ref`, quote creation, placement and idempotent replays, human decisions, expiration,
  `EVIDENCE_CHANGED` revisions, cancellation and failed attempts tied to the order or its quote. The response contains `order_id`,
  `quote_id`, nullable `lookup_id`, `agent_id`, current `status`, `steps`, `total_live_calls`
  (`price`, `availability`) and `total_decision_calls`. Each step preserves audit `ts`, `actor`, `action`,
  `summary` and `data`, plus a `step` stage (`lookup`, `quote`, `order`, `decision`, `cancellation`,
  `system`) and actual `live_calls` / `decision_calls` (zero if absent). Steps sort by timestamp and
  audit ID, without duplicates; totals include failed checks and human approval stock checks.
  The read refreshes local expirations, makes no upstream requests, and returns `404 ORDER_NOT_FOUND`
  for unknown orders. MCP `get_order_status` continues to return the existing order model.
- `GET /v1/lookups/{lookup_id}` returns the original stored `LookupResult`, including alternatives;
  use a quote's `need_ref`. Unknown IDs return `404 LOOKUP_NOT_FOUND`. No upstream calls are made.
- `GET /v1/policy` returns `approval`, `reject`, `ranking_weights`, `shortlist`, `agents`, `customers`, `updated_at`,
  `updated_by`, and `known_warning_codes`. The last three fields are server supplied; initial
  committed defaults have null update metadata. `PUT /v1/policy` accepts
  `{"policy": <editable sections>, "updated_by": "Ada", "comment": "Budget review"}` and returns
  the effective resource. Echoing the GET response as `policy` also works; its metadata is replaced.
  Thresholds/weights must be nonnegative, fit must be 0–1, weights must sum above zero, catalog pool
  must be 10–100, live shortlist cap must be 10–40, and warning codes must be known. Mandate IDs and
  customer IDs must be non-blank, each mandate needs at least one customer, and limits must be finite
  and nonnegative. Invalid requests return `422 VALIDATION_ERROR`.
- `POST /v1/policy/reset` with `{"updated_by": "Ada"}` restores committed defaults and returns
  the effective resource with new update metadata. Both writes append `POLICY_UPDATED` by
  `human:operator`, with the typed name in `data.stated_name`; `data.diff` maps changed dotted field paths to `{before, after}` and also records
  the optional comment and whether the operation was a reset.
- Effective policy is loaded from gitignored `backend/var/policy.yaml` before `backend/policy.yaml`.
  Writes atomically replace the runtime file; committed defaults remain unchanged. REST and MCP
  reread it for subsequent operations without restart. With a custom database path, the runtime
  policy lives beside that database so independent gateway instances stay isolated.
  PUTs omitting `agents`, `customers` or `shortlist.live_max` keep the current values, including older frontend
  request shapes. Older runtime files missing those fields inherit the committed defaults; an
  explicit `agents: {}` removes all mandates. Reset restores the committed mandates and limits.
- `GET /v1/stats?since=<ISO datetime>` returns described Pydantic metrics in the OpenAPI contract.
  `since` is inclusive, normalized to UTC (naive timestamps mean UTC). Current order counts and
  confirmed spend use **order creation time**; lookups, placement outcomes, human decisions and
  agent sightings use **audit event time**. Placement outcomes exclude retries and remain unchanged
  by later approval/rejection. Older placement audit rows use their recorded summary outcome.
  Live-call averages include lookups only, including cached zero-call lookups; Decisions totals
  count actual HTTP requests. Warning counts count offer occurrences, excluding lookup-level
  duplicates; rule counts count placement hits. Top-five ties sort by code/rule. Empty averages
  are zero. Each `agents_seen` entry includes `registered`, based on the effective policy at read time.
  Stats refresh local expirations and make no upstream calls.

## Agent mandates

`policy.yaml` registers `hotel-chain-agent` and `web-console` for customer `"1"`, each with a
5,000 EUR rolling 24-hour autonomous limit. `agents` maps each identity to `customer_ids`, optional
`max_auto_total_eur`, required `max_auto_daily_total_eur`, and a free-text `note` recording its grant.
REST resolves identity from `Authorization: Bearer <secret>`; `X-Agent-Id` has no authority. MCP fixes
identity using `--agent-id` at launch; tools cannot change it per call. The console uses the operator token
and its buyer actions run as `OPERATOR_AGENT_ID` (default `web-console`).

Lookup, quote and order require a mandate. Unregistered agents receive
`403 AGENT_NOT_REGISTERED`, create no order or reservation, and failed attempts are audited under
`agent:<id>`. Registered agents may look up, quote or
order only for their listed customers; violations return `403 CUSTOMER_NOT_IN_MANDATE`, audit a
failed attempt, and do not commit an order. Scope is rechecked at placement under the current policy.

The effective per-order ceiling is the lower of global `approval.max_auto_total_eur` and the
mandate's optional ceiling; `max_auto_total_eur` hits name the amount and its source. A registered
agent's currently confirmed autonomous EUR orders created in the inclusive rolling 24-hour window
are summed before adding the new total. Exceeding the daily ceiling adds `max_auto_daily_total_eur`
with the prior spend, new total and ceiling. Cancelled, pending, rejected, expired and human-approved
orders do not consume that budget. Equal-to-limit totals pass. This check and insertion share the
SQLite writer lock across REST/MCP instances, so concurrent split orders cannot bypass the ceiling.
Idempotent replays retain their existing result, even after mandate removal, and never add spend.
A human may approve an order held for a spend limit through the normal inbox. If the placing agent
lost its mandate, approval returns `403 AGENT_NOT_REGISTERED` with rule hits in `data`; the order
stays pending and a human can reject it.

## Fulfilment plans and substitutes (spec 08)

Every lookup adds `plans` alongside the existing offers, using all ranked, already-priced offers before
the returned offer slice. Fit evidence for every priced candidate is stored for later plan quotes.
No additional Valio or Decisions calls are made. Up to three top-ranked `single` plans cover the whole
need. A `split` plan combines up to four products when no single can cover it, or when splitting is
cheaper; the planner compares lot/pack combinations and respects on-time stock after local
reservations. Both fill enumeration and cheapest-split search share a 20,000-node budget per lookup
(the internal `build_plans(..., node_budget=...)` argument can override it). On exhaustion it keeps the
best valid plan found, with greedy fills always available as a fallback. It may miss a cheaper combination,
including savings of a cent due only to rounding individual lines. Reservations and autonomous daily spend
are read in one read snapshot; planning runs after that snapshot closes. The write transaction only stores
the lookup and its audit row, so planning holds no writer lock against quotes, orders or approvals.
Products must pass hard filters and have fit at or above the policy floor, valid EUR
prices and verified availability. If full coverage is impossible, a `partial` plan provides the best
coverage possible within four lines and states the missing quantity with `PARTIAL_STOCK`.
Offer-level `PARTIAL_STOCK` does not exclude a candidate before allocation, even when the policy rejects
partial orders: the plan's actual uncovered quantity determines that warning and its policy preview.

Plan lines contain `product_id`, numeric sales `units`, and `covers {value, unit}`. KG-sold products use
`allowed_lot_size` multiples (e.g. 10.5 kg for a 10.25 kg need with a 0.5 kg lot); other products use whole
sales units. A KG single plan's units and total match its offer. `coverage {value, unit}`
is the sum of line covers (possibly above the need due to whole packs); `uncovered {value, unit}` is
zero for full plans. `total` is the sum of rounded net EUR line totals. `warnings` combines line risks
and any uncovered-need warning. The latter survives plan quotation and placement, so ordering only
the available portion still requires approval by default. Quotes always recheck price and stock live.

Each plan's `policy_preview {outcome, hits}` uses the same evaluation as real placement, including
the caller's registration, customer scope, per-order ceiling and rolling daily spend. A registered agent
outside its customer mandate sees every plan preview `REJECTED` with `CUSTOMER_NOT_IN_MANDATE`;
quoting and placement retain their `403 CUSTOMER_NOT_IN_MANDATE` responses. REST lookup callers should send
`X-Agent-Id`; MCP uses its fixed server identity. Preview evaluation creates no order, quote, reservation
or separate audit event. A lookup still persists its normal lookup evidence and audit event. Stock,
spend and policy can change between preview and placement; **the actual order result is authoritative**.

Optional `preferences` contains `brands` (case-insensitive exact catalog brands), `max_price_per_unit`
(net EUR per L/KG), `pack_size {min, max}` (L/KG per sales unit), and `labels` (catalog label IDs).
Hard constraints remain `required_claims` / `exclude_allergens`. An offer leaving a soft preference
carries `OUTSIDE_PREFERENCE` listing every difference; unknown price/size/brand cannot prove a match.
All matching offers rank above substitutes, with existing scores within each group and best/better/good
labels assigned overall. Split and partial plans are first built from matching offers, preserving an
in-preference plan even when a substitute costs less. A cheaper split containing substitutes is offered
after it with `OUTSIDE_PREFERENCE`; preferred-only partials also remain visible when substitutes can
complete the need. Substitutes require approval in the committed default policy. Existing runtime
policy files continue loading and keep their configured warning list. Empty hard-filter results return
`no_match_reason` with per-constraint removal counts (overlapping failures can count in multiple buckets).
Old stored lookups without preferences/plans remain readable, including through the order timeline.

## Evidence, policy and lifecycle

- `search_products` is catalog-only. FTS searches English/Finnish names, English ingredients and marketing
  using AND tokens. `find_offers` reports `retrieval {mode, terms_used}`: explicit `search_terms` uses
  mode `terms`, otherwise all intent words use mode `intent`. Only an empty intent search broadens
  deterministically to mode `intent_broadened`: lower-case meaningful words, with numbers, common
  English/Finnish stop words and units removed (including decilitres, centilitres, litraa and kiloa);
  hyphenated words are also tried in parts. OR results
  are ordered by most distinct terms matched, then FTS rank and product ID. Hard constraints stay
  unchanged and Decisions receives the original intent. `RETRIEVAL_BROADENED` names the tried words;
  if no products match, `no_match_reason` names them and suggests `search_terms`. Explicit terms never
  broaden. The small stop-word/unit list is documented in `app/lookup.py`.
  Required claims and allergen exclusions are hard filters before shortlisting. The catalog preserves
  `contains`, `may_contain`, `undeclared`, `free_from` and `has_allergen_declaration`; `X99` is a declaration
  marker, never an allergen. Exclusion passes only on an uncontradicted free-from entry or a declaration
  not listing the excluded code. Missing declarations, contains, may-contain, undeclared and conflicting
  entries fail. Required claims contradicted by contains/may-contain data also fail (lactose, gluten,
  egg, soy, wheat and vegan mappings). `exclusion_counts` includes reasons and sources, also shown in
  `no_match_reason` for empty results. This is catalog evidence, not a safety certification.
  Allergen/claim codes are trimmed and uppercased; labels match case-insensitively to the build vocabulary.
  Unknown codes return `422 UNKNOWN_CODE` naming the field, code and valid values, including policy codes.
  Quantity units are L, KG or SALES_UNIT. `customer_id` is required and non-blank; `deliver_by` is today
  through +60 days in Europe/Helsinki. Quantities are strict numeric values >0 and <=1,000,000 (no booleans
  or numeric strings). Price/pack-size preferences and `max_candidates` also reject booleans and numeric
  strings; `max_candidates` requires an integer. Blank brand entries fail, and pack-size minimum cannot exceed maximum.
  MCP validation failures use JSON `{code, message}` tool errors as REST does.
- Decisions screens `shortlist.catalog_pool` (default 40). Every candidate at or above
  `approval.min_fit_probability` is priced, highest fit first, up to `shortlist.live_max` (default 25,
  validated 10–40). If too few pass, the next best fits fill up to `max_candidates`; without fit scores,
  the first catalog candidates are used and carry `NO_FIT_SCORE`. All live candidates share one price
  and one availability batch (cached evidence can reduce actual calls). Ranking runs before returning
  the top `max_candidates` (at most 10). `priced_count` counts live-price candidates including cache hits.
  Fit's ranking contribution is capped at the suitability floor so noise above it cannot reorder
  suitable products; response and approval checks retain the original probabilities.
  `matched_count` counts every search match passing hard constraints before the pool limit;
  `screened_count` reports candidates actually submitted to Decisions (zero without a key),
  including failed requests and candidates without returned answers. When `matched_count > screened_count`,
  `POOL_TRUNCATED` reports both numbers and suggests narrowing the search. The `LOOKUP` audit records
  `retrieval`, `matched_count`, `screened_count` and `priced_count`. The two retrieval warnings are
  informational in the default policy. Older stored lookups default counts to zero and retrieval
  to `intent` with no recorded terms.
  Policy updates omitting `shortlist.live_max` retain its current value; older runtime files inherit
  the committed default.
- Conversion computes catalog base content times the sales-unit multiplier, parsed name size times the
  multiplier, and the live catalog/comparison price ratio. Among exact sources confirmed by price,
  catalog content takes precedence over name content. When the ratio agrees within 3% (inclusive), the exact size is used and
  `size_confirmed_by_price` is true. Without a confirming ratio the exact size carries `SIZE_UNCONFIRMED`
  and requires approval. MLT/CLT volumes convert to litres; a KI sales case uses CASE content when present.
  CASE sales content needs no base-unit multiplier; CASE and name evidence are compared at matching
  packaging levels. Legacy CASE suspicion flags are recalculated from retained content and names.
  Catalog sizes >100 L, >100 kg, <1 g per base unit or
  differing from every compatible name-size candidate by a factor >=10 are suspect. Portion weights do not
  invalidate a matching package weight. `volume_suspect` and `weight_suspect` track dimensions independently;
  `size_suspect` remains their aggregate. Both English and Finnish names supply size evidence at build.
  Reconciliation rechecks both names even for legacy records with missing or incorrectly false dimension flags.
  Suspect content falls back to the name or ratio, then `SIZE_UNKNOWN`. A disagreement
  uses the ratio and emits `SIZE_MISMATCH` naming the values; this requires approval by default.
  With no exact content the ratio remains the fallback. Quantities, totals and coverage use the
  reconciled size; quotes retain their explicitly requested sales quantities. KG sales use their lot-size step;
  other sales quantities are whole units. Unknown content never silently becomes one litre/kg.
- Prices are positive net `catalogPrice.value`; missing/zero/negative prices or missing upstream currency
  give `PRICE_ERROR` and prevent quoting. No EUR fallback is assumed. Tax-inclusive values are returned separately. Every quote rechecks
  both endpoints bypassing the 60-second per-product cache. Order placement rechecks availability only.
  Expiry is the earliest price `validTo`, future cutoff, or local UTC now + 30 minutes. A past cutoff
  warns instead of creating an instantly expired quote. EUR is the MVP policy currency.
- Availability sums unblocked lines arriving on/before `deliver_by`. Future lines are flagged
  `DATE_SHIFT`, never counted as on-time coverage. `CUTOFF_PASSED` compares cutoff with upstream `timeNow`;
  its precise meaning is unconfirmed. Missing/unparseable cutoff gives `CUTOFF_UNKNOWN`; naive cutoffs
  are interpreted in Europe/Helsinki. Missing/different schedule `salesUnit` gives `UNIT_MISMATCH` and
  that line contributes zero stock. Missing/unknown logistic models give `STOCK_MODEL_UNKNOWN`, are finite,
  and subtract local reservations. Stock-model zero stock without a date gives rejecting `NO_STOCK`.
  Provenance uses upstream `timeNow` where supplied, otherwise local
  UTC receipt time (the price response normally has no server clock).
- Warnings: `PARTIAL_STOCK`, `DATE_SHIFT`, `CUTOFF_PASSED`, `SALES_BLOCK`, `UNKNOWN_PRODUCT`,
  `PRICE_ERROR`, `SIZE_UNKNOWN`, `SIZE_UNCONFIRMED`, `SIZE_MISMATCH`, `UNIT_MISMATCH`, `STOCK_MODEL_UNKNOWN`,
  `CUTOFF_UNKNOWN`, `NO_STOCK`, `NO_FIT_SCORE`, `OUTSIDE_PREFERENCE`, `AVAILABILITY_ERROR`,
  `VARIABLE_WEIGHT`, `DEPOSIT`, `RETRIEVAL_BROADENED`, and `POOL_TRUNCATED`. Variable-weight goods state
  that final price follows delivered weight; deposits include catalog details. Empty schedule lines signal
  `UNKNOWN_PRODUCT`. Lookup failures remain visible; a quote requires a valid price and successful
  upstream checks. Human approval cannot override sales blocks, unknown products, hard constraints,
  or unverifiable availability.
- `policy.yaml` defines ranking weights and all approval/rejection thresholds. Every matching rule is
  reported; rejection takes precedence. Every warning outside `reject.on` and `approval.ignored_warnings`
  requires approval even with empty `warnings_need_approval`. The ignored list defaults empty in the model;
  committed defaults ignore only VARIABLE_WEIGHT, DEPOSIT, RETRIEVAL_BROADENED and POOL_TRUNCATED.
  PRICE_ERROR, AVAILABILITY_ERROR, SIZE_UNKNOWN, SIZE_UNCONFIRMED, UNIT_MISMATCH, STOCK_MODEL_UNKNOWN,
  NO_FIT_SCORE and CUTOFF_UNKNOWN cannot be ignored (PUT returns 422). Disabling hard-violation rejection
  routes request violations to approval; customer standing violations always reject and cannot be approved.
  Legacy runtime policies without `approval.ignored_warnings` acquire the new default evidence/reject codes
  on load. Explicit current-policy edits retain their configured warning lists across reloads.
- `purchase_total` is units needed times net sales-unit price, rounded in decimal currency arithmetic;
  `app.quantities.pack_quantity` converts sizes, factors, multipliers and quantities via `Decimal(str(value))`,
  quantizes content to six decimal places, then ceilings the exact quotient to the sales-unit step.
  Ingestion, size reconciliation, offers (including quote lines) and plans share this arithmetic.
  `overbuy {amount, unit}` shows whole-pack excess. Both are explained in offer text; `unit_price` remains
  per sales unit. The price factor uses purchase total when size is known; unknown-size offers compare
  their valid per-unit prices within a separate range, retaining `SIZE_UNKNOWN` and no rank label.
  `score_breakdown {price, coverage, reliability, co2, fit}` contains weighted contributions summing to score.
  Reliability uses its absolute 0–1 value; CO2 uses the catalog-wide range. Missing values use the global
  catalog median, with neutral text (0.5 when no global data). CO2 is a product-group average.
  Reject-coded and SIZE_UNKNOWN/PRICE_ERROR/AVAILABILITY_ERROR/UNIT_MISMATCH offers follow all orderable
  offers and receive no rank label. Missing-fit offers follow scored offers; within those groups soft
  preference matches precede substitutes. Eligible top three labels are best/better/good.
- Policy `customers` maps each customer ID to `required_claims`, `exclude_allergens`, and `note`, default `{}`.
  Standing constraints are unioned with the request at lookup, quotation, placement and approval, always
  reading current policy. `effective_constraints` lists `{code, source}` entries with `request` or
  `customer_policy`; matched evidence, removal counts and hard-violation hits name the source.
  Updates validate vocabulary and audit customer changes in `POLICY_UPDATED.data.diff`. Approvals also
  recheck the placing agent's current customer scope. Rejection remains available when constraints tighten.
  Persisted legacy quotes without `need_ref` still load and retain their recorded violations; placement and
  approval also evaluate the quoted customer's current standing constraints for every product.
- `OPEN` quotes become `ACCEPTED` or `EXPIRED`. Each quote can create only one order.
  `PENDING_APPROVAL` orders become `CONFIRMED`, `REJECTED`, or `EXPIRED`; their approval window is the
  quote expiry. Expiration is evaluated on requests (no background worker).
  `POST /v1/orders/{order_id}/cancel` accepts `{"cancelled_by":"Ada","reason":"Wrong product"}`;
  the human name is required and non-blank, while `reason` defaults to `""`. REST and MCP share
  `Orders.cancel`: only `CONFIRMED` → `CANCELLED` is allowed, and every known line `closing_time`
  must still be in the future. Missing cutoffs do not block; a cutoff equal to now has passed.
  Fixed prices and quote evidence remain unchanged. The order stores `cancelled_by` (human name or
  placing agent ID) and `cancel_reason`; older/uncancelled orders return null for both fields.
  Successful cancellation records one `CANCEL` audit event by `human:operator` or `agent:<id>` with
  the reason, order ID and quote ID. Refused attempts record `CANCEL_FAILED` with the error code.
  Errors: `404 ORDER_NOT_FOUND`, `409 INVALID_TRANSITION`, `409 CUTOFF_PASSED`,
  `404 ORDER_NOT_FOUND` (another agent's order), and `422 VALIDATION_ERROR` (invalid request).
  Cancelled orders count under stats `orders_by_status.CANCELLED` and leave confirmed spend.
- Stock reservations are local because mock stock never decreases. Both pending and confirmed orders
  reserve the **full requested** quantity, conservatively across all delivery dates. Availability
  subtracts reservations and clamps at zero; approval excludes its own reservation from rechecks.
  Reject/expire/cancel releases reservations. Terminal products have no local reservation.
  A human can explicitly accept partial/date/cutoff risks; approving partial stock is a local commitment
  with those warnings, not proof the upstream can fulfill it. Fulfillment/replenishment is deferred.
- SQLite `BEGIN IMMEDIATE` serializes placement/reservations/idempotency across REST and MCP processes.
  The cache holds up to 2,048 entries per process and never bypasses local reservations.
  Audit insertions and state changes share a transaction; SQLite triggers prohibit audit UPDATE/DELETE.
  Audit stores inputs, rule hits, fit scores and actual outbound-call counts, including failed attempts
  and zero-call idempotent retries. No secrets are included.

## MVP boundaries

This prototype uses operator-issued shared Bearer secrets and one operator role.
Keep REST/MCP bound to loopback; CORS allows `http://localhost:*` for the frontend teammate.
Signed quotes/receipts, hash chaining with external checkpoints and UBL 2.1 exports are implemented below.
Peppol network delivery/certification, multiple approver roles, drift detection,
automatic re-planning after commitment, recipe-aware substitutions, payments, deployment and separate buyer-agent code are deferred.
The generated frontend contract is `contracts/openapi.yaml`; domain/validation errors use `{code,message}` with optional `data` for domain evidence.
Live Valio/OpenAI integration requires network access and configured keys; the suite uses no network.


## Authentication and console sign-in

From `backend/`, generate missing credentials for the effective policy's agents and the operator:

```sh
.venv/bin/python scripts/gen_keys.py --write
# Optional isolated env file:
.venv/bin/python scripts/gen_keys.py --write --env-file /path/to/local.env
```

The command preserves existing values, uses `secrets.token_urlsafe(32)` for new values, and prints
only added names. A second run changes nothing. The default target is repo-root `.env.local`;
`AGENT_KEYS` is a comma-separated list of `agent-id:secret`, and `OPERATOR_TOKEN` is required to start
the REST API. Process environment values take precedence. The server retains SHA-256 digests for
constant-time comparisons; credentials belong only in the local env file, never policy or audit data.
Restart after provisioning or rotating credentials. Never share the operator token with buyer agents.

An agent sends its own secret on every REST call. For example, with its key in `AGENT_SECRET`:

```python
import os
import httpx

response = httpx.get("http://localhost:8000/v1/orders",
                    headers={"Authorization": "Bearer " + os.environ["AGENT_SECRET"]})
response.raise_for_status()
```

`GET /health`, `/docs`, and `/openapi.json` stay public. Other REST routes require a valid credential
(`401 UNAUTHENTICATED`). Lookup, quote and order require a registered mandate and the customer's scope
(`403 AGENT_NOT_REGISTERED` / `CUSTOMER_NOT_IN_MANDATE`). Lookups and quotes store `agent_id`; agents
can only access their own lookups, quotes, orders, cancellations and timelines. Other agents' objects
return 404, and `GET /v1/orders` returns only the caller's orders. Legacy records without ownership
are operator-only. Operators can see all objects. Approvals/rejections, the approval inbox, policy
read/write/reset, audit and stats require the operator credential (`403 OPERATOR_REQUIRED` for agents).
Credential and operator checks precede validation errors on every protected route, including malformed JSON.
Operator changes use audit actor `human:operator`; typed names remain on the order/policy and in
`data.stated_name`. The console buyer identity remains subject to its mandate and budgets.

Open the console and paste `OPERATOR_TOKEN` from your local env file into its password field, then
click **Sign in**. The token stays in memory; reload or **Sign out** clears it. A 401 returns to sign-in.
Approval submits the displayed `evidence_revision`; `409 EVIDENCE_CHANGED` shows the returned changes
and reloads the inbox for review before another approval.

MCP stdio is unchanged: launch `python -m app.mcp_server --agent-id hotel-chain-agent` (the demo
`demo/run_buyer_agent.sh` continues to provision this identity). For `--http`, configure that agent
in `AGENT_KEYS` and send `Authorization: Bearer <that agent's secret>` on **every** HTTP request,
including initialization, GET and DELETE. Another agent's key or the operator token is refused.
OAuth for remote MCP and named operator accounts are production follow-ups.

## UBL 2.1 / Peppol BIS Ordering 3 mapping

Both interfaces use the same core: REST **OpenAPI 3.1.0** (the generated contract is
`contracts/openapi.yaml`) and **MCP protocol 2026-07-28** (SDK 2.3.0; negotiated
protocol versions are supported by the SDK). Exports target **OASIS UBL 2.1** Order
and OrderResponse and map the **Peppol BIS Ordering 3** business fields below.
Official XSD validation proves UBL structure; it does not certify Peppol business
rules, routing, party identity or transmission. Placeholder endpoint identifiers
make these demonstration documents unsuitable for delivery over Peppol.

| Gateway field / business field | UBL Order / OrderResponse mapping | Source or missing data |
|---|---|---|
| Standard / process identifiers | UBLVersionID = 2.1; CustomizationID = urn:fdc:peppol.eu:poacc:trns:order:3 or order_response:3; ProfileID = urn:fdc:peppol.eu:poacc:bis:ordering:3 | Peppol BIS Ordering 3 identifiers; no certification claim |
| Order.id / response ID | cbc:ID; response adds -response; OrderReference/ID identifies the order | Gateway-generated identifiers |
| Quote.id | QuotationDocumentReference/ID | Locked quote reference in Order |
| Order.created_at / decided_at | IssueDate, IssueTime | Creation for Order; decision time for OrderResponse |
| Order.currency | DocumentCurrencyCode and currencyID on amounts | Upstream price currency (EUR) |
| Quote.customer_id, Order.customer_id | BuyerCustomerParty/Party/PartyIdentification/ID; PartyName/Name | Internal customer identifier and display name; legal registered buyer name needs data from Valio Aimo |
| Seller identity | SellerSupplierParty/Party/PartyIdentification/ID; PartyName/Name | Explicit gateway-simulated identity/name; seller legal registration identifier and legal name need data from Valio Aimo |
| Buyer and seller electronic address (mandatory Peppol routing data) | BuyerCustomerParty and SellerSupplierParty / Party/EndpointID, schemeID=0088 | Literal placeholder: **needs data from Valio Aimo**; not an invented GLN |
| Party legal registration, postal addresses, country codes, VAT identifiers, contacts | PartyLegalEntity, PostalAddress/Country, PartyTaxScheme, Contact | needs data from Valio Aimo; omit unavailable fields, including conditional Peppol fields |
| Quote.deliver_by | Delivery/RequestedDeliveryPeriod/EndDate | Requested delivery deadline; delivery location/address and recipient identity need data from Valio Aimo |
| Line index | OrderLine/LineItem/ID | Stable 1-based line identifier |
| Line.product_id / GTIN | Item/SellersItemIdentification/ID; StandardItemIdentification/ID schemeID=0160 | GS1 salesUnitGtin; catalog also stores units[].gtin by unitId. Missing GTIN is omitted, seller item ID remains |
| Line.name | Item/Name | synkkaData.names[] (English, then Finnish) |
| Line.units_needed | LineItem/Quantity with unitCode | Sales-unit quantity, decimal string; unit mapping below |
| Line.unit_price | LineItem/Price/PriceAmount and BaseQuantity=1 with unitCode | Locked net price per sales unit; no price negotiation |
| Line.total / Order.total | LineExtensionAmount; AnticipatedMonetaryTotal (Order) or LegalMonetaryTotal (response): LineExtensionAmount, TaxExclusiveAmount | Locked net line and order totals |
| Tax / gross / payable | TaxTotal/TaxAmount, TaxInclusiveAmount, PayableAmount | Derive tax from upstream unit_price_with_tax minus locked net totals, round line gross to cents. Tax category, VAT percentage, exemption reason and seller VAT registration need data from Valio Aimo. If gross is missing, omit tax/gross and explicitly label payable as net in a Note |
| Allowances, charges, rounding, payment means/terms, accounting references, originator references | AllowanceCharge, monetary totals, PaymentMeans, PaymentTerms, AccountingCost, OriginatorDocumentReference | No gateway allowances/charges; other mandatory-if-applicable Peppol data needs data from Valio Aimo; never manufacture values |
| Order.status | OrderResponseCode: CONFIRMED=AP, REJECTED=RE, PENDING_APPROVAL=CA; CANCELLED/EXPIRED=RE | Gateway decision, not a supplier response |
| Order.policy.hits | Note per rule and detail | All policy rule hits; includes approval conditions |
| Order.commitment | Note: gateway-simulated, no supplier order transmitted | Explicit simulated commitment |

UN/ECE Recommendation 20 measures and Recommendation 21 package codes (X prefix):

| Catalog salesUnit | UBL unitCode | Meaning / mapping assumption |
|---|---|---|
| L, LTR | LTR | litre |
| KG | KGM | kilogram |
| ST | H87 | piece |
| KI, KRT | XCT | carton |
| RAS | XPU | tray |
| SK | XSA | sack |
| PAK, DSP | XPK | package; DSP uses generic package pending a precise packaging classification |
| BAG | XBG | bag |
| PRK | XJR | jar |
| BOT | XBO | bottle |
| NIP | XBE | bundle |
| CS | XCS | case |
| PAL | XPX | pallet |
| KAN | XCA | can |
| DZ | DZN | dozen |
| KLE | XBX | box |
| Unknown unit | H87 | fallback with a document Note; precise unit needs data from Valio Aimo |

All 17 sales-unit values in the supplied catalog are covered in `app/ubl.py`.
GS1 source vocabulary is retained: GTIN = `salesUnitGtin` and `units[].gtin`;
allergens = `synkkaData.classifications[name=allergen].values[].id/unit`;
free-from = `classifications[name=nonAllergen]`; claims =
`classifications[name=nutritionalClaim].values[].id`; packaging labels =
`classifications[name=packagingMarkedLabel].values[].id`. Missing facts are not proof.

GET `/v1/orders/{id}/ubl?document=order|response` (default: order) returns XML.
MCP `get_order_ubl(order_id, document)` returns the identical XML. Agents can export
only their own orders; the operator can export all orders. XSD tests use
`xmlschema` with local-only loading of `vendor/ubl-2.1/xsd/maindoc/`.

Public principal `credential_ref` values use a non-secret identity/key-slot
reference (`agent:<id>:key:1` or `operator:<id>:key:1`). Bearer secrets and their
SHA-256 digests are never included in these references or principal responses.

## Signed promises and tamper-evident audit

Each quote carries `price_commitment: gateway-locked` and an Ed25519 detached
JWS (RFC 7515, EdDSA). Its protected header contains `alg` and `kid`; resolve
the public key from unauthenticated GET `/.well-known/jwks.json`. RFC 8785
canonical JSON payload (`app.signing.quote_payload`) contains:

- id, customer_id, deliver_by, total, expires_at, price_commitment;
- lines: product_id, units (from units_needed), unit_price, currency, gtin, provenance.

The canonical payload preserves spec 10 (and the GTIN addition in spec 13). New
quotes add protected JWS header members `quote_version: 2` and `quote_integrity`:
the latter is SHA-256 hex of the RFC 8785 canonical immutable resource, excluding
only signature, status and live_calls, with all numbers normalized to decimal
strings. This also authenticates owner, need binding, warnings, fit, tax prices
and export evidence without expanding the detached promise payload. A standard
JWS verifier can reconstruct the specified payload independently; the supplied
verifier also checks the protected evidence digest.

Money and quantity values in the signed payload are normalized decimal strings
(no exponent or unnecessary trailing zeros); the existing JSON resource keeps
its numeric fields. Provenance covers price and availability sources, fetched_at
and valid_to exactly as serialized in the resource. No Unicode normalization is
performed. The JWS is `base64url(protected)..base64url(signature)`; the Ed25519
signing input is `base64url(protected) + '.' + base64url(canonical_payload)`
(RFC 7515 encoded payload, not RFC 7797). Quote status changes do not alter the
signed price promise. Placement checks the persisted signature before stock
lookup or committing; alteration is refused and audited as QUOTE_SIGNATURE_INVALID.
Both earlier unversioned formats (the specified payload and the short-lived
expanded payload with an integrity member) remain readable, verifiable offline
and usable for placement until expiry, subject to current authority and policy.
Their original signatures are preserved; they are never silently re-signed.
Unsigned legacy quotes remain readable and require re-quoting for a new order.
Existing orders replay after ownership/identity checks
without requiring a historical quote key to remain published. Their stored buyer
mandate is still revalidated, including when no header is supplied.
An authenticated legacy absence of `need_ref` remains loadable and enforces its
recorded violations plus current customer constraints; deleting a signed binding
cannot turn a current quote into such a legacy record.

Every new order decision carries a receipt over
`{order_id, quote_id, quote_signature_digest, customer_id, agent_id, status,
 total, currency, decided_at, decided_by_kind, commitment}`.
The quote_signature_digest is SHA-256 of the UTF-8 detached quote JWS; total is
a decimal string. The receipt is replaced on approval, rejection, cancellation
and expiration. `decided_by_kind` is agent, operator or system.
`commitment: gateway-simulated` means this gateway records a decision; no
supplier order is transmitted. Signing proves the gateway's origin and integrity;
truth of product, origin and stock claims depends on the upstream provenance.

Save the quote response and public JWKS, then verify without a running gateway
(only standard library plus cryptography required):

```sh
.venv/bin/python scripts/verify_quote.py quote.json --jwks jwks.json --customer 1
.venv/bin/python scripts/verify_quote.py quote.json --jwks jwks.json --at 2026-11-14T12:00:00Z
.venv/bin/python scripts/rotate_key.py
```

The verifier accepts a file or an HTTP(S) JWKS URL; use a saved file for fully
offline verification. Exit 0 means signature, expiry and optional customer match;
errors identify signature, expiry or customer. Trust the origin of your saved JWKS.
Runtime signing keys are generated on first start under `backend/var/keys/`,
with private PEM files mode 0600 and an atomic, process-locked active-key registry.
Demo reset leaves keys in place. Rotation makes a new key active immediately
for REST and MCP; retired public keys remain published for **7 days**. Keep saved
JWKS snapshots for verification beyond that window. `--keys-dir DIR` supports
isolated rotation; Settings.signing_key_dir overrides the default directory,
otherwise it is db_path.parent / keys. Tests always use temporary directories.

Every audit write appends inside the same BEGIN IMMEDIATE transaction as the
business operation. REST/MCP writers serialize across processes. The hash is
SHA-256 of UTF-8 prev_hash (64 lowercase hex characters) concatenated with
RFC 8785 JSON `{seq, ts, actor, action, summary, order_id, quote_id, data}`;
seq is the audit id, genesis is 64 zero characters. Audit entries expose hash and
prev_hash, and timeline steps expose hash. Existing entries are chained once in
an atomic migration followed by AUDIT_CHAIN_START; subsequent starts never
silently repair the chain.

Any authenticated principal can call `GET /v1/audit/head` (entries, head_hash,
at, jws) and `GET /v1/audit/verify` (valid, entries, head_hash, first_break).
The head's JWS signs `{entries, head_hash, at}` with the active quote key.
Store it outside the gateway; later pass both
`?checkpoint_entries=N&checkpoint_hash=H` to verify. checkpoint_match compares
the recomputed prefix at N, so rewriting and rehashing old entries cannot match
an earlier external checkpoint. first_break is the first broken sequence position
(including a missing position); empty chains have zero entries and the genesis
head. Raw audit entries remain operator-only under spec 19. A chain in the same
database cannot prove integrity against an attacker who rewrites the whole
database without an independently held checkpoint. Signed heads provide that
checkpoint, not an external timestamp or proof of upstream truth.
Audit entry exports preserve the exact stored `ts` string (including `+00:00`
versus `Z`) used by the hash, including migrated rows. Verification streams one
SQLite read snapshot with bounded memory; full scans are serialized per gateway.


## Buyer-issued verifiable mandates (spec 12)

The buyer signs an Ed25519 W3C VC 2.0 JWT (`typ: vc+jwt`); the authenticated
agent presents it with its existing Bearer credential. `sub` and
`vc.credentialSubject.agent_id` must equal that credential's agent ID.
The issuer grants customer scope, per-order and rolling 24-hour EUR limits,
with optional catalog category codes. An agent needs no policy agent row when
it presents a valid buyer mandate. Existing policy rows can only tighten its
customer scope and limits; the global per-order ceiling still applies.
Calls without a buyer VC retain the existing policy-row behavior.

From `backend/`, issue a mandate (first use generates the private issuer key
under `var/buyer-keys/`, mode 0600; demo reset leaves this folder alone):

```sh
.venv/bin/python scripts/issue_mandate.py --customer 1 --agent hotel-chain-agent --order-limit 1500 --daily-limit 5000 --days 1 --category 10400 --out /tmp/hotel-mandate.jwt
```

The command prints the issuer ID, public JWK and mandate `jti`, never the
private key. Keep its stdout as onboarding metadata. For isolated runs use
`--keys-dir /tmp/buyer-keys`; repeat `--category` to allow several categories.
Buyer credentials are reusable authority documents; store their files securely.
The operator registers the printed issuer and public JWK in the policy:

```yaml
customers:
  "1":
    mandate_issuers:
      urn:buyer:hotel-chain:
        - kid: <printed kid>
          kty: OKP
          crv: Ed25519
          alg: EdDSA
          use: sig
          x: <printed public x>
```

Use authenticated `GET /v1/policy`, preserve its other fields, add this customer
section, then `PUT /v1/policy` with `{policy: ..., updated_by: "Buyer onboarding"}`
using the **operator** Bearer token. This is audited as `POLICY_UPDATED`.
Public keys alone belong in policy; private keys stay with the buyer.

Present the file over stdio:

```sh
.venv/bin/python -m app.mcp_server --agent-id hotel-chain-agent --mandate /tmp/hotel-mandate.jwt
```

For REST, and MCP HTTP on every request, send both headers:
`Authorization: Bearer <agent secret>` and `Agent-Mandate: <JWT file contents>`.
For example (environment `AGENT_SECRET` contains that agent's credential):

```python
import os
from pathlib import Path
import httpx
headers = {"Authorization": "Bearer " + os.environ["AGENT_SECRET"],
           "Agent-Mandate": Path("/tmp/hotel-mandate.jwt").read_text().strip()}
# Reuse these headers for POST /v1/offers, /v1/quotes and /v1/orders.
# Pass lookup_id as need_ref when quoting; send Idempotency-Key when ordering.
with httpx.Client(base_url="http://localhost:8000", headers=headers) as client:
    print(client.get("/v1/orders").json())
```

Revoke the mandate using the `jti` printed at issuance, with the same issuer key:

```sh
.venv/bin/python scripts/issue_mandate.py --revoke <issued-jti> --out /tmp/hotel-revocation.jwt
```

Submit it to the gateway with an authenticated credential (the signed buyer
revocation is the authority, not the submitting agent's identity):

```python
from pathlib import Path
import httpx
response = httpx.post("http://localhost:8000/v1/mandates/revocations",
    headers=headers, json={"jwt": Path("/tmp/hotel-revocation.jwt").read_text().strip()})
response.raise_for_status()
```

An invalid revocation returns 422 `REVOCATION_INVALID`. The next procurement
call with that mandate returns 403 `MANDATE_INVALID`, naming `revoked`.
Other named checks include `format`, `signature`, `issuer`, `nbf`, `exp`,
`sub`, `customer`, `category`, and `binding`. Invalid calls are audited.
Lookup and local search filter mandate categories before counting, limiting or
deciding to broaden retrieval; explicit
quote/order lines must all be allowed. Quote/order records bind the original
JWT privately, so omitting a later header or swapping the mandate cannot bypass
revocation. Human approval revalidates the **placing agent's stored mandate**,
including expiry, issuer, customer and category, while the approver must still
be an authenticated operator. A revoked pending order can be rejected.
Objects and successful audit entries expose `mandate {id, issuer, limits,
valid_until}` with effective limits; stats `agents_seen` includes the currently
valid, trusted, unrevoked `mandate_id` from the latest successful use (including
local catalog searches). Credentials are deduplicated by digest; successful use
updates a durable monotonic sequence, including repeated A → B → A use. Historical
credential rows retain insertion ordering until reused. Registration reflects current policy or
current buyer authority; historical evidence stays on objects and audit entries.
New revocations apply only to customers trusting the actual signing key, even
when an issuer ID or key ID is reused with different keys. Legacy unscoped
revocations retain their prior denial through migration because their signer
scope was not recorded; migration never silently restores revoked authority.
The JWT itself is not returned in objects or audit entries. Holder binding uses
the existing agent secret; key-bound proof of possession and OAuth remain follow-ups.

## Public audit anchoring (spec 21)

Anchoring is off unless all four required environment variables below are set.
`Settings.from_env()` loads them from repo-root `.env.local` without overriding
the process environment. Keep the wallet key only in that local file/environment;
`.env.example` lists names with empty values. No RPC calls are made while off.

| Variable | Polygon mainnet | Amoy testnet |
|---|---|---|
| `ANCHOR_RPC_URL` | `https://polygon-rpc.com` or your provider | `https://rpc-amoy.polygon.technology` or your provider |
| `ANCHOR_CHAIN_ID` | `137` | `80002` |
| `ANCHOR_PRIVATE_KEY` | Your funded anchoring wallet key | Your test wallet key |
| `ANCHOR_EXPLORER_TX_URL` | `https://polygonscan.com/tx/` | `https://amoy.polygonscan.com/tx/` |
| `ANCHOR_EVERY_N_ENTRIES` (optional) | Positive integer, e.g. `100` | Positive integer, e.g. `10` |

These are example public RPC URLs; no live endpoint was contacted during implementation.
The RPC chain ID must match configuration. The wallet signs locally using
`eth-account`; httpx sends plain JSON-RPC. A type-2 EIP-1559 transaction sends
zero value **to the same wallet** with data
`VAIMO-AUDIT-1:<entries>:<head_hash>`. Only the count and hash leave the gateway;
no business data or private key is sent. A funded wallet pays transaction gas.
The public wallet address appears in `/health`'s `anchoring` section when enabled.

From `backend/`, anchor once:

```sh
.venv/bin/python scripts/anchor_audit.py
# Isolated database:
.venv/bin/python scripts/anchor_audit.py --db /tmp/demo/app.db
```

The command prints `sent:` or `confirmed:` and the explorer link. Without
configuration it exits 1 and names the missing variables. Run again to poll a
sent transaction. Each invocation captures its current head before receipt
reconciliation; if an older pending checkpoint exists, it reconciles that hash
then submits the captured head using the pending nonce. Background receipt polls
only reconcile existing submissions. The computed hash and signed bytes are stored before broadcast;
after an uncertain send, retry checks that hash and may rebroadcast identical
bytes if the node does not know it, without allocating a new nonce. Existing
anchor rows lacking signed bytes still load and reconcile by their recorded hash.
Confirmed receipts update `audit_anchors`
and append exactly one `AUDIT_ANCHORED` event. RPC errors produce sanitized
messages and a failed row; retry preserves a previously broadcast transaction
hash. `GET /v1/audit/anchors` lists checkpoints for authenticated callers.
When configuration is removed, it returns an empty list without deleting history
or making RPC calls. Positive verification requires a valid block timestamp.

With `ANCHOR_EVERY_N_ENTRIES` set, a post-commit background worker anchors after
N new audit entries. RPC work runs outside the request database transaction;
failures do not fail business calls and the next trigger retries even without
new audit entries. Triggers arriving while busy are retained; after completion
the worker checks the threshold again. `AUDIT_ANCHORED` entries do not recursively
trigger anchors. Pending receipts are polled in the background every second,
independently of new business traffic, and can also be checked by CLI. Configuration and worker
state are process-local; a file lock serializes CLI/worker submissions across
processes sharing the database.

Use an authenticated client to call `GET /v1/audit/verify?anchor=latest` or
`?anchor=0x<transaction hash>`. Verification reads the chain transaction and
successful receipt, validates its self-addressed payload, recomputes the local
prefix, and returns `anchor_match` plus `anchor_block_time`. Later appended
entries do not invalidate an earlier anchor; edited, deleted or rehashed old
history fails the checkpoint comparison. Missing, malformed or unavailable chain
evidence returns `anchor_match: false`. An anchor is an external timestamped
checkpoint, not proof of the upstream business facts. Chain reorganization and
confirmation-depth policies are production follow-ups.

Tests use temporary DB/key folders and respx JSON-RPC mocks, with no external
network. `test_ac_6_local_anvil_real_transaction_and_verification` uses loopback
Anvil when it is on PATH and loopback listeners are permitted; otherwise it is explicitly skipped.

Review regressions additionally use the real organiser catalog. Spec 15 AC-16 replays the captured demo
projection in `tests/fixtures/demo_lookup_capture.json` against all 17,546 products, then quotes and orders
464306 for EUR 1,010. This is recorded gateway evidence, not raw upstream HTTP: only captured candidates
have replayed fit/price/stock evidence, and validity/delivery times are rebased. The synthetic smoke test
remains separate. A fresh live demo check is still required to establish current live AC-16 acceptance;
the offline replay does not establish that result. Historical lookup statistics bypass request date-window
validation, while new requests still enforce it. Malformed cancellation JSON returns 401 without a valid
credential, or structured 422 after authentication.

## Shared commands, recovery and limits (specs 22–24)

REST and MCP use `app.commands` for principal-scoped idempotency and token costs. Order placement
requires its existing `Idempotency-Key`; quote, cancel, approve and reject also accept this header
(optional for compatibility). Policy update/reset and signed mandate revocation accept it too.
MCP quote and cancel accept `idempotency_key`. Reuse a key only for the
same command and payload. Exact successful replays use read tokens and return the stored result;
order replays still revalidate any bound buyer credential and retain the historical `ORDER_REPLAY`
audit. Changed payloads return `409 IDEMPOTENCY_CONFLICT`. Command results, business state, audit and
domain events commit atomically. Refusals do not cache a successful command result.

Every new lookup, quote and order has `context`: customer, supplier (`valio-aimo`), request reference,
effective constraints, mandate reference, SHA-256 of canonical effective policy, and environment
(`live`; the sandbox is a later spec). `links` connect request, quote, order, receipt, event, audit
checkpoint and trace identifiers; task references remain null until task transports are installed.
Order detail exposes all event IDs, audit checkpoints and trace IDs. Trace links are stored locally;
when an OpenTelemetry span exists its trace ID is used. Span export is provided by spec 27.

`X-Correlation-Id` is accepted/generated and returned on success and failure. MCP accepts the HTTP
header or `_meta.correlation_id` (`correlationId` also accepted). Audit data, events and recovery
payloads retain it. Events have schema version 1 and per-aggregate revisions, including quote
acceptance. `events.audit_id` links the audit hash chain. `outbox` has one durable row per registered
consumer. Register `gateway.outbox.register("webhooks", handler)` (or `"a2a-push"`, `"activity"`);
registration without a handler reserves the durable consumer name. `replay=True` backfills existing
events. Post-commit dispatch never runs callbacks inside the procurement transaction. Failures remain
pending with exponential retry intervals, and `dispatch()` can be called by a worker independently
of new traffic. Callbacks must deduplicate by event ID: delivery is at least once. Named registrations
survive restart; callbacks are attached again by their owning feature. No webhook or A2A delivery is
claimed before those features are installed.

REST errors use `application/problem+json` with RFC 9457 fields and legacy `code`, `message`, `data`.
Recovery version 1 adds `retryable`, `next_action`, `retry_after_seconds`, `correlation_id`, and
`violations` (field, problem, valid_values). Resource references are omitted unless read access has
been verified; current errors conservatively omit them. Authentication errors disclose no resource
details. MCP domain failures return `isError: true` with the same structured payload; malformed
protocol use remains a protocol error. The explicit inventory is `app/recovery.py`.

| next_action | Client transition |
|---|---|
| RETRY_SAME_COMMAND | Wait retry_after_seconds; preserve payload and key |
| REFRESH_QUOTE | Obtain a new quote, then place it |
| LOOKUP_REQUIRED | Find offers before quoting |
| WAIT_FOR_APPROVAL | Poll status after poll_after_seconds |
| REVIEW_UPDATED_EVIDENCE | Have an authorised approver review the new evidence revision |
| FIX_REQUEST | Repair violations using valid_values |
| CONTACT_ADMIN | Resolve credential or authority configuration |
| STOP | End automatic recovery |

Confirmed orders return `next_action: null`; rejected, expired and cancelled orders return `STOP`.
`clients/python/reference_buyer.py` is the deterministic
MCP recovery loop reused by the later connect kit: inject `call_tool`, then call
`await ReferenceBuyer(call_tool).purchase(need_json)`. Lost responses retry the unchanged command/key;
expiry, throttles and approval waiting are driven by `next_action`, never prose or domain codes.

Policy `rate_limits` has `lookup`, `mutation`, `read`, each with `capacity` and `refill_per_minute`.
Defaults are 60/60, 120/120, 600/600 respectively. An agent's `agents.<id>.rate_limits` overrides all
three buckets. Limits take effect on the next request without resetting consumed tokens. Exhaustion
returns `429 RATE_LIMITED`, `Retry-After`, and retry guidance before Decisions or supplier work.
Throttles are audited and counted in `/v1/stats.throttles`. Supplier admission is globally bounded
within the process: 8 active calls, 16 waiting, a 5-second queue wait; overflow/timeouts yield retryable
`503 UPSTREAM_BUSY`. Unknown credentials have a separate source-address bucket before credential
resolution (60 attempts, 60/minute; successful authentication refunds its attempt). Limits are
**in-process, single instance**; SQLite commands/events remain shared across processes.

`GET /v1/me` returns the resolved principal and current limits/remaining tokens. Principals include
kind, ID, organisation, customers, role and a digest credential reference. Policy `organisations`
maps IDs to `{customer_ids: [...]}`; `identities` maps credential IDs to `{kind, role, organisation_id}`.
Buyer roles are `buyer_admin`, `buyer_approver`, `buyer_viewer`; suppliers use `supplier_agent`.
`scripts/gen_keys.py --write` provisions every ID in `agents` and `identities`, with the same secret
format and without printing secrets. These identity labels grant no new operations by themselves.
The existing operator-only guards remain in force until the buyer/supplier feature specs add scope.

Public `GET /v1/meta` separates specification versions from installed packages and states limiter
scope. A2A, Standard Webhooks and the MCP server card are explicitly planned, not implemented.
The server-card draft is pinned to vendored commit `526201bbc80231daa40ffcdecfc9da4e54e5dc93`;
Standard Webhooks is pinned to vendored commit `36afae792227eae8a182a7a1a5711dbdc08a4334`.
