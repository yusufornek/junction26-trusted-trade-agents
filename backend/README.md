# Trusted Trade Agents backend MVP

One Python core serves FastAPI and the MCP 2.3 server. It searches the local catalog, screens a
40-product pool with Decisions, checks live Valio price/availability for suitable candidates (up to 25 by default), locks fixed-price quotes, then
runs the approval policy. The upstream mock has no search or order endpoint: quotes, orders,
approvals and reservations exist only in this gateway.

## Run

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
```

The build reads the unchanged gzip/CSV files in `../data/` and writes `backend/var/app.db` (gitignored).
Rebuilding replaces only catalog/FTS rows, retaining orders and audit. The real 17,546-row catalog
built in 1.719 seconds in the supplied environment. API docs: `http://127.0.0.1:8000/docs`.
`GET /health` returns the catalog row count; build the catalog before demonstrating lookup.

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

2. Select an offer. Call `create_quote`, or `POST /v1/quotes`, with `customer_id`, `deliver_by`,
   `need_ref` set to the returned `lookup_id`, and `lines` containing its `product_id` and
   `quantity_sales_units` set to `units_needed`. Multiple explicit lines are supported. The gateway
   stores lookups and retrieves fit/constraints itself; clients cannot inject a trusted fit score.
3. Call `place_order(quote_id, idempotency_key)`. REST: `POST /v1/orders` with `{"quote_id":"..."}`,
   header `Idempotency-Key`, and optional `X-Agent-Id` (default `external`, unregistered and always
   requiring human approval). Use a registered identity such as `hotel-chain-agent` for autonomous
   purchases. Fixed prices remain the quoted prices. Repeated keys return the existing order;
   reuse with another quote/agent is a conflict.
4. If `PENDING_APPROVAL`, inspect `GET /v1/approvals`. A human calls
   `POST /v1/approvals/{order_id}/approve` or `/reject`, body
   `{"approver":"Ada","comment":"Reviewed delivery and budget"}`.
5. Read `/v1/orders/{id}`, `/v1/quotes/{id}`, `/v1/orders`, and
   `/v1/audit?order_id=...&quote_id=...`. When both audit filters are supplied, they combine with AND.

The offline full-flow tests exercise 1,000 L → 200 sales units at 13 EUR each → 2,600 EUR quote →
`PENDING_APPROVAL` → human approval → `CONFIRMED`, over both REST and MCP core tools.

## Frontend control plane

- `GET /v1/orders/{order_id}/timeline` returns the complete order story: the lookup referenced by the
  quote's `need_ref`, quote creation, placement and idempotent replays, human decisions, expiration,
  cancellation and failed attempts tied to the order or its quote. The response contains `order_id`,
  `quote_id`, nullable `lookup_id`, `agent_id`, current `status`, `steps`, `total_live_calls`
  (`price`, `availability`) and `total_decision_calls`. Each step preserves audit `ts`, `actor`, `action`,
  `summary` and `data`, plus a `step` stage (`lookup`, `quote`, `order`, `decision`, `cancellation`,
  `system`) and actual `live_calls` / `decision_calls` (zero if absent). Steps sort by timestamp and
  audit ID, without duplicates; totals include failed checks and human approval stock checks.
  The read refreshes local expirations, makes no upstream requests, and returns `404 ORDER_NOT_FOUND`
  for unknown orders. MCP `get_order_status` continues to return the existing order model.
- `GET /v1/lookups/{lookup_id}` returns the original stored `LookupResult`, including alternatives;
  use a quote's `need_ref`. Unknown IDs return `404 LOOKUP_NOT_FOUND`. No upstream calls are made.
- `GET /v1/policy` returns `approval`, `reject`, `ranking_weights`, `shortlist`, `agents`, `updated_at`,
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
  PUTs omitting `agents` or `shortlist.live_max` keep the current values, including older frontend
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

Unregistered agents may search and quote, but their orders always require human approval with
`unregistered_agent`; hard rejection rules still take precedence. Registered agents may quote or
order only for their listed customers; violations return `403 CUSTOMER_NOT_IN_MANDATE`, audit a
failed attempt, and do not commit an order. Scope is rechecked at placement under the current policy.

The effective per-order ceiling is the lower of global `approval.max_auto_total_eur` and the
mandate's optional ceiling; `max_auto_total_eur` hits name the amount and its source. A registered
agent's currently confirmed autonomous EUR orders created in the inclusive rolling 24-hour window
are summed before adding the new total. Exceeding the daily ceiling adds `max_auto_daily_total_eur`
with the prior spend, new total and ceiling. Cancelled, pending, rejected, expired and human-approved
orders do not consume that budget. Equal-to-limit totals pass. This check and insertion share the
SQLite writer lock across REST/MCP instances, so concurrent split orders cannot bypass the ceiling.
Idempotent replays retain their existing result and never add spend. A human may approve an order
held for a missing mandate or spend limit through the normal inbox.

## Evidence, policy and lifecycle

- `search_products` is catalog-only. FTS searches English/Finnish names, English ingredients and marketing
  using AND tokens; `search_terms` avoids incidental words in the buyer's natural-language intent.
  Required claims and excluded `CONTAINS` allergens are hard filters before shortlisting. Absence from
  the contains list is catalog evidence, not certification that an allergen is absent.
- Decisions screens `shortlist.catalog_pool` (default 40). Every candidate at or above
  `approval.min_fit_probability` is priced, highest fit first, up to `shortlist.live_max` (default 25,
  validated 10–40). If too few pass, the next best fits fill up to `max_candidates`; without fit scores,
  the first catalog candidates are used and carry `NO_FIT_SCORE`. All live candidates share one price
  and one availability batch (cached evidence can reduce actual calls). Ranking runs before returning
  the top `max_candidates` (at most 10). `priced_count` counts live-price candidates including cache hits.
  Fit's ranking contribution is capped at the suitability floor so noise above it cannot reorder
  suitable products; response and approval checks retain the original probabilities.
  `screened_count` reports the catalog screening pool. Older stored lookups default both counts to zero.
  Policy updates omitting `shortlist.live_max` retain its current value; older runtime files inherit
  the committed default.
- Conversion computes catalog base content times the sales-unit multiplier, parsed name size times the
  multiplier, and the live catalog/comparison price ratio. Among exact sources confirmed by price,
  catalog content takes precedence over name content. When the ratio agrees within 3% (inclusive), the exact size is used and
  `size_confirmed_by_price` is true. Without a ratio the exact size is used unconfirmed. A disagreement
  uses the ratio and emits `SIZE_MISMATCH` naming the values; this requires approval by default.
  With no exact content the ratio remains the fallback. Quantities, totals and coverage use the
  reconciled size; quotes retain their explicitly requested sales quantities. KG sales use their lot-size step;
  other sales quantities are whole units. Unknown content never silently becomes one litre/kg.
- Prices are net `catalogPrice.value`; tax-inclusive values are returned separately. Every quote rechecks
  both endpoints bypassing the 60-second per-product cache. Order placement rechecks availability only.
  Expiry is the earliest price `validTo`, future cutoff, or local UTC now + 30 minutes. A past cutoff
  warns instead of creating an instantly expired quote. EUR is the MVP policy currency.
- Availability sums unblocked lines arriving on/before `deliver_by`. Future lines are flagged
  `DATE_SHIFT`, never counted as on-time coverage. `CUTOFF_PASSED` compares cutoff with upstream `timeNow`;
  its precise meaning is unconfirmed. Provenance uses upstream `timeNow` where supplied, otherwise local
  UTC receipt time (the price response normally has no server clock).
- Warnings: `PARTIAL_STOCK`, `DATE_SHIFT`, `CUTOFF_PASSED`, `SALES_BLOCK`, `UNKNOWN_PRODUCT`,
  `PRICE_ERROR`, `SIZE_UNKNOWN`, `SIZE_MISMATCH`, `NO_FIT_SCORE`, and `AVAILABILITY_ERROR`. Empty schedule lines signal
  `UNKNOWN_PRODUCT`. Lookup failures remain visible; a quote requires a valid price and successful
  upstream checks. Human approval cannot override sales blocks, unknown products, hard constraints,
  or unverifiable availability.
- `policy.yaml` defines ranking weights and all approval/rejection thresholds. Every matching rule is
  reported; rejection takes precedence. Missing ranking metrics score zero; known values are min/max
  normalized across candidates. CO2 is a product-group average. Top three labels: best/better/good.
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
automatic splitting/substitution, payments, deployment and separate buyer-agent code are deferred.
The generated frontend contract is `contracts/openapi.yaml`; domain/validation errors use `{code,message}`.
Live Valio/OpenAI integration requires network access and configured keys; the suite uses no network.
