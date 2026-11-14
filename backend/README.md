# Trusted Trade Agents backend MVP

One Python core serves FastAPI and the MCP 2.3 server. It searches the local catalog, screens a
40-product pool with Decisions, checks live Valio price/availability for suitable candidates (up to 25 by default), locks fixed-price quotes, then
runs the approval policy. The upstream mock has no search or order endpoint: quotes, orders,
approvals and reservations exist only in this gateway.

## Commands

No installation is needed in the supplied environment. From the **repository root**:

```sh
cd backend
.venv/bin/python -m app.catalog build
.venv/bin/python -m uvicorn app.api:app --host 127.0.0.1 --port 8000
```

In separate terminals, starting in `backend/`:

```sh
# MCP stdio; stdout is reserved for protocol messages
.venv/bin/python -m app.mcp_server

# MCP Streamable HTTP: http://127.0.0.1:8001/mcp
.venv/bin/python -m app.mcp_server --http --host 127.0.0.1 --port 8001

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

Startup loads `VALIO_API_BASE`, `VALIO_API_KEY`, and `OPENAI_API_KEY` from repo-root `.env.local`
if present. Existing environment variables take precedence. The parser accepts comments, optional
`export`, and quoted values; it does not execute shell code. See `backend/.env.example` for variable
names. No keys are printed. A missing Decisions key yields `NO_FIT_SCORE` and human approval;
missing Valio configuration produces visible unavailable evidence and prevents quoting.

For a stdio MCP client, set its executable to the **absolute** `backend/.venv/bin/python` path,
arguments to `["-m", "app.mcp_server"]`, and working directory to the absolute `backend/` path.
Tools: `search_products`, `find_offers`, `create_quote`, `place_order`, `get_order_status`, `cancel_order`.
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
   header `Idempotency-Key`, and `X-Agent-Id` identifying a registered agent such as
   `hotel-chain-agent`. The default `external` is unregistered: ordering requires a mandate
   (`403 AGENT_NOT_REGISTERED`); lookups and quotes are open. Fixed prices remain the quoted prices. Repeated keys return the existing order;
   reuse with another quote/agent is a conflict.
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
  `human:<name>`; `data.diff` maps changed dotted field paths to `{before, after}` and also records
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
REST uses `X-Agent-Id` (also accepted by `POST /v1/offers` for lookup audit attribution). MCP fixes
identity using `--agent-id` at launch; tools cannot change it per call. The frontend buyer flow
should send `X-Agent-Id: web-console`.

Ordering requires a mandate. Unregistered agents may search and quote; placement returns
`403 AGENT_NOT_REGISTERED`, creates no order or reservation, and audits the failed attempt under
`agent:<id>`. Plan previews show `REJECTED` with `unregistered_agent`. Registered agents may quote or
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
reservations. Cheapest-split search uses stock-aware bounds and a shared 20,000-node budget per lookup
(the internal `build_plans(..., node_budget=...)` argument can override it). On exhaustion it keeps the
best valid plan found, with greedy fills always available as a fallback. It may miss a cheaper combination,
including savings of a cent due only to rounding individual lines. Reservations and autonomous daily spend
are read in one read snapshot; planning runs after that snapshot closes. The write transaction only stores
the lookup and its audit row, so planning holds no writer lock against quotes, orders or approvals.
Products must pass hard filters and have fit at or above the policy floor, valid EUR
prices and verified availability. If full coverage is impossible, a `partial` plan provides the best
coverage possible within four lines and states the missing quantity with `PARTIAL_STOCK`.

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
  English/Finnish stop words and units removed; hyphenated words are also tried in parts. OR results
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
  or numeric strings), blank brand entries fail, and pack-size minimum cannot exceed maximum.
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
  `screened_count` reports the catalog screening pool. When `matched_count > screened_count`,
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
  Catalog sizes >100 L, >100 kg, <1 g per base unit or differing from the name by a factor >=10 are
  stored as `size_suspect` and ignored, falling back to the name or ratio, then `SIZE_UNKNOWN`. A disagreement
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
  routes violations to approval. Older runtime policies acquire the new default evidence/reject codes on load.
- `purchase_total` is units needed times net sales-unit price, rounded in decimal currency arithmetic;
  `overbuy {amount, unit}` shows whole-pack excess. Both are explained in offer text; `unit_price` remains
  per sales unit. The price factor uses purchase total when size is known, otherwise per-unit comparison.
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
- `OPEN` quotes become `ACCEPTED` or `EXPIRED`. Each quote can create only one order.
  `PENDING_APPROVAL` orders become `CONFIRMED`, `REJECTED`, or `EXPIRED`; their approval window is the
  quote expiry. Expiration is evaluated on requests (no background worker).
  `POST /v1/orders/{order_id}/cancel` accepts `{"cancelled_by":"Ada","reason":"Wrong product"}`;
  the human name is required and non-blank, while `reason` defaults to `""`. REST and MCP share
  `Orders.cancel`: only `CONFIRMED` → `CANCELLED` is allowed, and every known line `closing_time`
  must still be in the future. Missing cutoffs do not block; a cutoff equal to now has passed.
  Fixed prices and quote evidence remain unchanged. The order stores `cancelled_by` (human name or
  placing agent ID) and `cancel_reason`; older/uncancelled orders return null for both fields.
  Successful cancellation records one `CANCEL` audit event by `human:<name>` or `agent:<id>` with
  the reason, order ID and quote ID. Refused attempts record `CANCEL_FAILED` with the error code.
  Errors: `404 ORDER_NOT_FOUND`, `409 INVALID_TRANSITION`, `409 CUTOFF_PASSED`,
  `403 NOT_ORDER_OWNER` (MCP agent ownership), and `422 VALIDATION_ERROR` (invalid request).
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

This local development gateway has one named human approver role and no authentication.
Keep REST/MCP bound to loopback; CORS allows `http://localhost:*` for the frontend teammate.
Signed quotes, verifiable-credential mandates, UBL/Peppol, hash chaining, multiple approver roles, drift detection,
automatic re-planning after commitment, recipe-aware substitutions, payments, deployment and separate buyer-agent code are deferred.
The generated frontend contract is `contracts/openapi.yaml`; domain/validation errors use `{code,message}` with optional `data` for domain evidence.
Live Valio/OpenAI integration requires network access and configured keys; the suite uses no network.
