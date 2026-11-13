# Trusted Trade Agents backend MVP

One Python core serves FastAPI and the MCP 2.3 server. It searches the local catalog, shortlists at
most ten products, checks Valio price/availability and Decisions fit, locks fixed-price quotes, then
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
Tools: `search_products`, `find_offers`, `create_quote`, `place_order`, `get_order_status`.
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
   header `Idempotency-Key`, and optional `X-Agent-Id` (default `external`). Fixed prices remain the
   quoted prices. Repeated keys return the existing order; reuse with another quote/agent is a conflict.
4. If `PENDING_APPROVAL`, inspect `GET /v1/approvals`. A human calls
   `POST /v1/approvals/{order_id}/approve` or `/reject`, body
   `{"approver":"Ada","comment":"Reviewed delivery and budget"}`.
5. Read `/v1/orders/{id}`, `/v1/quotes/{id}`, `/v1/orders`, and
   `/v1/audit?order_id=...&quote_id=...`. When both audit filters are supplied, they combine with AND.

The offline full-flow tests exercise 1,000 L → 200 sales units at 13 EUR each → 2,600 EUR quote →
`PENDING_APPROVAL` → human approval → `CONFIRMED`, over both REST and MCP core tools.

## Evidence, policy and lifecycle

- `search_products` is catalog-only. FTS searches English/Finnish names, English ingredients and marketing
  using AND tokens; `search_terms` avoids incidental words in the buyer's natural-language intent.
  Required claims and excluded `CONTAINS` allergens are hard filters before shortlisting. Absence from
  the contains list is catalog evidence, not certification that an allergen is absent.
- Conversion priority is live catalog/comparison price ratio, catalog base content times sales-unit
  multiplier, then size parsed from the name times the multiplier. KG sales use their lot-size step;
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
  `PRICE_ERROR`, `SIZE_UNKNOWN`, `NO_FIT_SCORE`, and `AVAILABILITY_ERROR`. Empty schedule lines signal
  `UNKNOWN_PRODUCT`. Lookup failures remain visible; a quote requires a valid price and successful
  upstream checks. Human approval cannot override sales blocks, unknown products, hard constraints,
  or unverifiable availability.
- `policy.yaml` defines ranking weights and all approval/rejection thresholds. Every matching rule is
  reported; rejection takes precedence. Missing ranking metrics score zero; known values are min/max
  normalized across candidates. CO2 is a product-group average. Top three labels: best/better/good.
- `OPEN` quotes become `ACCEPTED` or `EXPIRED`. Each quote can create only one order.
  `PENDING_APPROVAL` orders become `CONFIRMED`, `REJECTED`, or `EXPIRED`; their approval window is the
  quote expiry. Expiration is evaluated on requests (no background worker). The core also supports
  `Orders.cancel` for `CONFIRMED` → `CANCELLED` before known cutoffs; no MVP REST/MCP cancellation tool.
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
Signed quotes, credentials/mandates, UBL/Peppol, hash chaining, multiple approver roles, drift detection,
automatic splitting/substitution, payments, deployment and separate buyer-agent code are deferred.
The generated frontend contract is `contracts/openapi.yaml`; domain/validation errors use `{code,message}`.
Live Valio/OpenAI integration requires network access and configured keys; the suite uses no network.
