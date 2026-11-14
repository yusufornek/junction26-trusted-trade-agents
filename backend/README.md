# Trusted Trade Agents backend MVP

## Agent connections (spec 42)

The console's Connect page uses operator-only `POST /v1/connections` with a required
`Idempotency-Key` and `{agent_id, client, customer_ids, label?}`. Agent IDs use
`^[a-z0-9][a-z0-9-]{1,47}$`; clients are `claude_desktop`, `claude_code`, `cursor`,
`vscode`, `chatgpt`, `mcp_other`, or `rest`. The client choice records the setup tool;
it grants no additional authority. Customer IDs must already occur in customer policy,
an agent mandate or an organisation's scope. Empty customer lists are refused.

Creation returns `201` with the console's `NewConnection` fields and a high-entropy
`vak_` key. Copy the key immediately: only its SHA-256 digest and first eight characters
are retained. Exact idempotent replays return the same connection with `key: null`;
changed payloads return `409 IDEMPOTENCY_CONFLICT`. No key is recorded in the command
ledger or audit. `GET /v1/connections` lists public `Connection` records newest first,
including revoked connections. `DELETE /v1/connections/{connection_id}` revokes a key
immediately for REST and MCP; repeated deletion preserves the first revocation timestamp.
Creation/revocation are audited by `human:operator`. Agent history and mandates remain.
Previously used signed buyer mandates also identify existing agents: issuing another key
returns their effective limits/autonomy without creating fallback policy authority.
Expired or revoked VC history remains identity evidence only; requests still revalidate
the VC. If a prior connection has lost its policy mandate and has no signed authority,
issuance returns `403 AGENT_NOT_REGISTERED` until authority is explicitly restored.

A new agent receives an `approval_required` mandate for the selected customers and
`approval.max_auto_daily_total_eur` (default 5,000 EUR). Existing agents retain their
mandate and autonomy. Issued keys resolve through the same principal/policy path as
`AGENT_KEYS`; environment keys continue to work. `last_used_at` updates on authentication,
at most once per minute per key. Operator credentials are required for all three
connection-management endpoints; agent keys cannot issue or revoke credentials.

The gateway serves Streamable HTTP MCP at exactly `/mcp`, with its session manager in
the gateway lifespan and an authenticated principal for each request. Bearer keys are
required even for ping and tool listing; tools audit as `agent:<agent_id>`. No agent is
bound at gateway startup. `GATEWAY_PUBLIC_URL` supplies the public origin for `mcp_url`
(`<origin>/mcp`) and `rest_base_url` (`<origin>/v1`). Without it, REST uses the request
origin; MCP advertises only loopback hosts (`localhost`, `127.0.0.1`, `[::1]`). External
request hosts receive `mcp_url: null` and use REST until a public origin is configured.
`create_app(..., serve_mcp=False)` disables MCP, returns `mcp_url: null`, and removes
the MCP catalog entry and server-card route.
Run one gateway process per database; do not start a second MCP process on its database.
The `chatgpt` client value records a preference only: OAuth sign-in is not implemented
by this connection API. Tests run offline against isolated databases and in-process MCP:
`.venv/bin/python -m pytest tests/test_spec42_agent_connections.py -q`.

## Buyer view and portable evidence (specs 28–29)

`GET /v1/buyer/{agents,mandates,spend,orders,approvals,activity}` accepts `buyer_user`
credentials with roles `buyer_viewer`, `buyer_approver`, or `buyer_admin`; the operator
may read all customers. Responses include `organisation_id` and `customer_ids`.
Identities and organisations are configured in policy; credentials come from
`AGENT_KEYS` or operator-issued connections. Buyer approvers/admins use `POST /v1/buyer/approvals/{order_id}/approve`
or `/reject` (the existing `/v1/approvals` routes also accept them). Approval retains
the evidence-revision rule and records `human:buyer_approver:<credential ref>`, the
organisation and `approval_side=buyer`. Viewers cannot review.

Buyer admins use `POST /v1/buyer/agents/{agent_id}/suspend` or `/resume` with an
optional `{reason}` body, and `POST /v1/buyer/mandates/{jti}/revoke` for a previously
used buyer VC. These local emergency controls are durable, scoped and audited.
Buyer writes accept `Idempotency-Key` through the shared command ledger.
Suspension returns `403 AGENT_SUSPENDED`, `CONTACT_ADMIN` on the next procurement
command; history remains readable. Revocation reuses spec 12's revocation ledger,
so bound pending orders cannot be approved. Spend uses policy's rolling 24-hour
autonomous ledger, effective daily limits, pending commitments and human-approved
totals. Domain activity is newest first with actors and human-readable summaries.

`GET /v1/orders/{order_id}/evidence` downloads a ZIP; `?format=json` returns its
signed manifest. MCP `get_order_evidence` returns the same manifest with authenticated,
immutable artifact and bundle links. The order agent, its scoped buyer organisation,
and the operator can read it; other agents receive 404. The ZIP includes signed quote,
receipt, audit entries and hash-only links for all undisclosed entries, signed checkpoint,
UBL Order/OrderResponse, public JWKS, optional anchor metadata, and `VERIFY.md`.
The signed manifest binds order/customer/environment, the artifact inventory and SHA-256
digests. Snapshots remain downloadable after later activity and key rotations.

From `backend/`, using **independently obtained trusted keys**:

```sh
.venv/bin/python scripts/verify_evidence.py purchase.zip --trusted-jwks /trusted/gateway-jwks.json
# Optional network anchor verification and explicit local official schema directory:
.venv/bin/python scripts/verify_evidence.py purchase.zip --trusted-jwks /trusted/gateway-jwks.json --rpc-url https://polygon-rpc.example --xsd-dir vendor/ubl-2.1
```

The JSON report separates `signature_valid`, `trusted_issuer_match`, `audit_consistent`,
`anchor_status` (`matched`, `pending`, `not_checked`), `ubl_schema_valid` and
`supplier_acceptance` (`simulated`), plus inventory and binding checks. It names failed
artifacts/entries and identifies offline versus network checks. Exit 0 requires all
executed checks to pass. Without RPC, the anchor is `not_checked`; a configured RPC
with an unconfirmed transaction fails with `pending`. UBL schemas default to the vendored
official UBL 2.1 directory and never load remote schema hints. Bundled keys are compared,
never treated as the trust root. `/.well-known/jwks-history.json` retains all public keys
indefinitely for historical evidence; `/.well-known/jwks.json` retains spec 10's current
seven-day quote-key window. Save trusted historical keys before offline verification.
The upstream has no order API: receipts and UBL records explicitly remain gateway-simulated.

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

# Gateway Streamable HTTP is already available at http://127.0.0.1:8000/mcp.
# Use the standalone --http entry only with a separate DB_PATH, never the gateway DB.

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
Tools: `get_agent_context`, `get_replenishment_suggestions`, `prepare_suggestion`, `create_recurring_from_suggestion`, `list_recurring_orders`, `create_recurring_order`, `run_recurring_order_now`, `pause_recurring_order`, `list_proposals`, `search_products`, `find_offers`, `find_basket_offers`, `create_quote`, `place_order`, `get_order_status`, `get_order_receipt`, `get_order_evidence`, `get_order_ubl`, `cancel_order`.
`cancel_order(order_id, reason="")` cancels only confirmed orders placed by the authenticated HTTP agent or stdio `--agent-id`,
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
   `need_ref` then defaults to that lookup. Recipe baskets use `basket_ref: {"basket_lookup_id": "..."}`.
   Exactly one of `lines`, `plan_ref` or `basket_ref` is required.
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
- Existing installations load gitignored `backend/var/policy.yaml` before `backend/policy.yaml`.
  Managed writes prepare/fsync a temporary file and save the policy in SQLite `runtime_files`, in the
  same transaction as the audit, events and keyed command result. The runtime YAML is atomically
  replaced only after the outer transaction commits; audit/result/commit failure leaves it unchanged.
  A process-shared writer lock covers commit and publication. SQLite holds the pending content,
  including for REST/MCP reads inside the transaction, until publication succeeds. A crash or disk error
  after commit leaves a recoverable projection: startup or the next write republishes the committed YAML.
  Such a committed change returns success and is effective immediately; the projection failure is logged.
  Temporary-file cleanup is best-effort before and after commit: an unlink failure is logged, preserves
  the transaction's original result/error, and cannot prevent connection or writer-lock closure.
  After publication the recovery row is removed and existing file-only loading remains compatible.
  Recovery paths are relative to the database directory so restored databases stay isolated.
  Committed defaults remain unchanged. With a custom database path, the runtime
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
`max_auto_total_eur`, required `max_auto_daily_total_eur`, optional `autonomy`, and a free-text `note` recording its grant.
Omitted autonomy uses `policy.autonomy.default: approval_required`: otherwise acceptable orders start
`PENDING_APPROVAL` with rule hit `autonomy_level`. The two demo mandates explicitly have
`autonomous_within_limits`, granted after the pilot's approved orders (demo setting), preserving the
1,010 EUR demo confirmation. Other policy limits and hard constraints still apply.
REST resolves identity from `Authorization: Bearer <secret>`; `X-Agent-Id` has no authority. Standalone MCP fixes
identity using `--agent-id` at launch; the gateway's `/mcp` resolves each request's Bearer credential.
Tools cannot choose another identity. The console uses the operator token
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
prices and orderable availability (including unknown but orderable stock). If full coverage is impossible, a `partial` plan provides the best
coverage possible within four lines and states the missing quantity with `PARTIAL_STOCK`.
Offer-level `PARTIAL_STOCK` does not exclude a candidate before allocation, even when the policy rejects
partial orders: the plan's actual uncovered quantity determines that warning and its policy preview.

Offers expose their effective `allowed_lot_size` so plans use the same step as the offer.
Plan lines contain `product_id`, numeric sales `units`, and `covers {value, unit}`. Every product uses whole
sales units, including KG. Organiser `allowedLotSize` values below one count as one (a 10.25 kg need
with a 0.01 or 0.5 lot needs 11 kg). Larger lot steps are rounded up to whole units. A KG single plan's units and total match its offer. `coverage {value, unit}`
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
- Conversion computes catalog base content times the sales-unit multiplier, local name/count candidates,
  and the live catalog/comparison price ratio. Among exact sources confirmed by price,
  catalog content takes precedence over name content. When the ratio agrees within 3% (inclusive), the exact size is used and
  `size_confirmed_by_price` is true. Without a confirming ratio the exact size carries `SIZE_UNCONFIRMED`
  and requires approval. MLT/CLT volumes convert to litres; a KI sales case uses CASE content when present.
  Name-derived conversion is supported only by a live ratio within 3%, matching explicit catalog content
  within 3%, one count expression matching the sales-unit multiplier with no contradictory measure,
  or one measure with multiplier 1. Other name sizes are display estimates: the smallest plausible
  candidate drives the displayed quantity, and `SIZE_UNCONFIRMED` prevents autonomous placement.
  Spec 15 still requires approval for every catalog/name size lacking a confirming live ratio,
  including otherwise unambiguous names. `name_size_candidates` exposes each candidate, its original
  `source_text` and local `expression` in offers and quote/order lines. A portion/count can check a stated
  package total but never multiply that total: `22g/2.77kg`, 126 pieces, gives 2.77 kg once.
  Repeated complete expressions (`4x1kg ... 4x1kg`) remain independent; genuinely separated outer
  counts (`8 x Brand 4x70g`) remain 32 portions. Inner portion counts are retained:
  `4 x 625g/25pc` with 100 pieces per case gives (100/25) × 0.625 = 2.5 kg.
  CASE sales content needs no additional base-unit multiplier. It must agree within 3% with whole-case
  name evidence (including leading counts such as `24x Brooklyn ... 0.33 L`), or inner content times the
  sales-unit multiplier. Nested counts (`8 x Brand 4x70g`, `4x6x ... 0.33 L`) retain the inner counts
  while consuming the outer sales multiplier once. Shared-unit net/drained names (`460/230g`) use
  the net weight, 460g. Nonpositive measures, including measures rounded to zero, supply no evidence.
  An inner-bottle size cannot validate a multi-bottle CASE. A KG base-unit multiplier
  measures case weight rather than counting packages, preserving valid whole-case weights.
  Legacy CASE suspicion flags are recalculated from retained content and names without a database rewrite.
  Historical lookups, quotes and orders default missing name candidates to an empty list and preserve
  the field's absence when serialized, retaining the original signed evidence shape.
  Catalog sizes >100 L, >100 kg, <1 g per base unit or
  differing from every compatible name-size candidate by a factor >=10 are suspect. Portion weights do not
  invalidate a matching package weight. `volume_suspect` and `weight_suspect` track dimensions independently;
  `size_suspect` remains their aggregate. Both English and Finnish names supply size evidence at build.
  Reconciliation rechecks both names even for legacy records with missing or incorrectly false dimension flags.
  Suspect CASE content falls back to valid BASE content, the name or ratio, then `SIZE_UNKNOWN`. A disagreement
  with the price ratio uses the ratio and emits `SIZE_MISMATCH` naming the values; a conversion inconsistent
  with explicit name counts also carries `SIZE_MISMATCH`, even without a ratio. Both require approval by default.
  Without a live ratio, plausible but contradictory catalog/name content is not confirmed: `SIZE_MISMATCH`
  names both figures and retains the plausible catalog package total as the anchor.
  Name candidates replace that anchor only when confirmed by the live ratio within 3%; either language can supply the confirmed candidate.
  Slash portion counts (10/1500G, 60KPL/900G, 900g/60pcs, with optional spaces) never multiply a weighed package total.
  Nested pcs/kpl chains keep every count and multiply only a portion measure that reconciles with a named or catalog total within 3%.
  Explicit quote quantities stay unchanged.
  With no exact content the ratio remains the fallback. Quantities, totals and coverage use the
  reconciled size; quotes retain explicitly requested whole sales quantities, including KG. Lot sizes below one
  count as one; fractional quotes return `422 INVALID_LOT_SIZE` naming the whole-unit step.
  Catalog reads normalize legacy lot sizes as well. Unknown content never silently becomes one litre/kg.
- Prices are positive net `catalogPrice.value`; missing/zero/negative prices or missing upstream currency
  give `PRICE_ERROR` and prevent quoting. No EUR fallback is assumed. Tax-inclusive values are returned separately. Every quote rechecks
  both endpoints bypassing the 60-second per-product cache. Order placement rechecks availability only.
  Expiry is the earliest price `validTo`, future cutoff, or local UTC now + 30 minutes. A past cutoff
  warns instead of creating an instantly expired quote. EUR is the MVP policy currency.
- Availability sums unblocked lines arriving on/before `deliver_by`. Future lines are flagged
  `DATE_SHIFT`, never counted as on-time coverage. `closingTime` is the order cut-off for the accompanying
  `availableDate`; a past cut-off gives `CUTOFF_PASSED`, naming the delivery date and cut-off timestamp,
  and still requires approval. On cache hits, cut-offs use the fetched supplier time advanced by the
  cache age (measured with a monotonic clock), falling back to local receipt time without a supplier clock;
  the original fetched-at provenance is preserved. Missing/unparseable cutoff gives `CUTOFF_UNKNOWN`; naive cutoffs
  are interpreted in Europe/Helsinki. Missing/different schedule `salesUnit` gives `UNIT_MISMATCH` and
  that line contributes zero stock. An individual usable schedule amount >=999999 gives informational
  `AVAILABILITY_UNCONFIRMED`, independently of `logisticModel`. When that line arrives on/before the deadline,
  any quantity may be ordered: `available_amount: null`, `availability_known: false`, full coverage,
  and no local reservation. Later unknown stock does not change finite on-time amounts or reservations.
  Finite amounts subtract reservations even for Terminal products.
  Missing/unknown logistic models still give `STOCK_MODEL_UNKNOWN`. Stock-model zero stock without a date gives rejecting `NO_STOCK`.
  Provenance uses upstream `timeNow` where supplied, otherwise local
  UTC receipt time (the price response normally has no server clock).
- Warnings: `PARTIAL_STOCK`, `DATE_SHIFT`, `CUTOFF_PASSED`, `SALES_BLOCK`, `NOT_AVAILABLE`, `AVAILABILITY_UNCONFIRMED`,
  `PRICE_ERROR`, `SIZE_UNKNOWN`, `SIZE_UNCONFIRMED`, `SIZE_MISMATCH`, `UNIT_MISMATCH`, `STOCK_MODEL_UNKNOWN`,
  `CUTOFF_UNKNOWN`, `NO_STOCK`, `NO_FIT_SCORE`, `OUTSIDE_PREFERENCE`, `AVAILABILITY_ERROR`,
  `VARIABLE_WEIGHT`, `DEPOSIT`, `RETRIEVAL_BROADENED`, and `POOL_TRUNCATED`. Variable-weight goods state
  that final price follows delivered weight; deposits include catalog details. Empty schedule lines signal
  rejecting `NOT_AVAILABLE`. A product omitted from the response stays `AVAILABILITY_ERROR`.
  `UNKNOWN_PRODUCT` remains a valid policy code for stored data but is no longer emitted.
  Lookup failures remain visible; a quote requires a valid price and successful
  upstream checks. Human approval cannot override sales blocks, unknown products, hard constraints,
  or unverifiable availability.
- `policy.yaml` defines ranking weights and all approval/rejection thresholds. Every matching rule is
  reported; rejection takes precedence. Every warning outside `reject.on` and `approval.ignored_warnings`
  requires approval even with empty `warnings_need_approval`. The ignored list defaults empty in the model;
  committed defaults ignore VARIABLE_WEIGHT, DEPOSIT, RETRIEVAL_BROADENED, POOL_TRUNCATED and AVAILABILITY_UNCONFIRMED.
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
  Reject/expire/cancel releases reservations. Unknown-availability products have no local reservation.
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
constant-time comparisons; environment credentials belong only in the local env file, never policy or audit data.
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
are operator-only. Operators can see all objects. The approval inbox and approve/reject routes also
accept the organisation's `buyer_approver` or `buyer_admin` (policy identities and organisations from spec 22).
Other organisations' decisions return 404. Human decisions show `approval_side: buyer | valio_operator`
and the authenticated actor in audit. Policy read/write/reset, audit and stats require the operator
credential (`403 OPERATOR_REQUIRED` for agents).
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
| Seller identity | SellerSupplierParty/Party/PartyIdentification/ID; PartyName/Name | Supplier ID; Valio gateway simulation or explicitly synthetic Nordfood; legal registration data is unavailable |
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
| Order.commitment | Note: gateway-simulated or supplier-acknowledged plus supplier reference | Explicit execution commitment; synthetic Nordfood acknowledgements are labelled |

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
signature, status, live_calls, expiration_reason and the derived navigation lists inside links,
with all numbers normalized to decimal strings. Original link identifiers remain authenticated. This also authenticates owner, need binding, warnings, fit, tax prices
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
Historical evidence omits `allowed_lot_size` and `availability_known` when those fields were absent
from the signed record, including REST/MCP responses and accepted/expired status saves. Their runtime
defaults still load as one whole sales unit and known availability; newly issued evidence includes both fields
under the existing version-2 protected integrity digest.
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
with optional catalog category codes and `autonomy: approval_required | autonomous_within_limits`.
Omitting autonomy uses the approval-first policy default. An agent needs no policy agent row when
it presents a valid buyer mandate. Existing policy rows can only tighten its
customer scope, limits and autonomy; the stricter autonomy level wins and the global per-order ceiling still applies.
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
(optional for compatibility). Policy update/reset, signed mandate revocation and autonomy changes accept it too.
Buyer suspension, resumption and local mandate revocation use the same command boundary and header.
MCP quote, cancel, basket lookup, suggestion actions and recurring workflow mutations accept `idempotency_key`. REST exposes the matching optional `Idempotency-Key` header on those commands, proposal submissions/decisions and buyer preference writes. Reuse a key only for the
same principal, command route (including every target/path parameter) and payload.
The added policy approval daily limit is omitted from command fingerprints at its default
(5,000 EUR). Commands saved before that field existed, and commands saved with its
default serialized, both replay; a changed non-default limit still conflicts.
The shared replay guard rechecks current authorization before exposing any saved result: ownership,
customer mandates, buyer organisation/role, trusted revocation issuer and operator permissions as applicable.
Revoked/invalid credentials are rejected again at the transport boundary. Quote/order/approval guards
also reverify their bound buyer VC. Agent cancellation rechecks original order authority on fresh calls
and replays. Operator cancellation and human rejection retain reviewer and ownership permissions.
New keyed commands cannot register without a replay guard.
Lookup, basket, quote, order and review command checks use their normal failure handlers on cached
replays as well as fresh requests. Each denied attempt commits one corresponding `*_FAILED` audit
with the caller, inputs and zero live-call counts; replay guards never repeat the business operation.
Exact successful replays use read tokens and return the stored result;
order replays still revalidate any bound buyer credential and retain the historical `ORDER_REPLAY`
audit. Spec 06 R-2's existing-order exception remains: removing a policy-row mandate alone does not deny
the original order retry; ownership and any signed buyer credential still apply, and an existing row's
changed customer scope is enforced. Human rejection needs no purchasing credential.
Autonomy replay never repeats a promotion/demotion, and rechecks the target's current organisation,
customers and existence. Changing the autonomy target under the same key returns `409 IDEMPOTENCY_CONFLICT`.
Buyer control replays likewise recheck the administrator role and the target's current organisation/customer
scope; they never repeat a suspension or revocation. Mandate events are scoped by customer, issuer and
mandate ID, and retain the buyer organisation and control provenance for activity and webhook delivery.
Earlier command envelopes still load; legacy autonomy entries must also match the saved target.
Changed payloads return `409 IDEMPOTENCY_CONFLICT`. Command results, business state, audit and
domain events commit atomically. Refusals do not cache a successful command result; committed evidence-refresh refusals retain their recovery payload.
A committed `EVIDENCE_CHANGED` refusal is saved and replayed too;
after reviewing the refreshed evidence, approve with a new key. Policy file rollback remains active
through the outer command commit. Policy digests preserve omission versus explicit clearing of sections
and `shortlist.live_max`; historical unversioned digests remain readable. Cached quote replays revalidate
the bound mandate and current customer scope. Legacy placement records without a command row use read
cost and acquire a saved result on their first command-boundary replay. Conflicts on existing keys also
spend read tokens. Spec 25's `PENDING_EXECUTION` exception still resumes ambiguous supplier execution.


Every new lookup, quote and order has `context`: customer, supplier (`valio-aimo`), request reference,
effective constraints, mandate reference, SHA-256 of canonical effective policy, and environment
(`live` or the isolated `sandbox` workspace). `links` connect request, quote, order, receipt, event, audit
checkpoint and trace identifiers; task references remain null until task transports are installed.
Order detail exposes all event IDs, audit checkpoints and trace IDs. Authorized quote and lookup reads
add `quote_ids`, `order_ids`, `event_ids`, `audit_checkpoints` and `trace_ids` to `links`, including for
historical rows; these derived navigation fields are excluded from signed quote evidence. Approval
refreshes context from the policy snapshot and effective constraints used for that decision. Trace links are stored locally;
when an OpenTelemetry span exists its trace ID is used. Span export is provided by spec 27.

`X-Correlation-Id` is accepted/generated and returned on success and failure. MCP accepts the HTTP
header or `_meta.correlation_id` (`correlationId` also accepted). Audit data, events and recovery
payloads retain it. Events have schema version 1 and per-aggregate revisions, including quote
acceptance. `events.audit_id` links the audit hash chain. `outbox` has one durable row per registered
consumer. Register `gateway.outbox.register("webhooks", handler)` (or `"a2a-push"`, `"activity"`);
registration without a handler reserves the durable consumer name. Dispatch selects only consumers
with an attached handler before applying the batch limit, so offline consumers cannot starve others. `replay=True` backfills existing
events. Post-commit dispatch never runs callbacks inside the procurement transaction. Failures remain
pending with exponential retry intervals, and `dispatch()` can be called by a worker independently
of new traffic. Callbacks must deduplicate by event ID: delivery is at least once. Named registrations
survive restart; callbacks are attached again by their owning feature. Specs 33–34 install signed
webhook delivery and durable A2A task projections over this same outbox.

REST errors use `application/problem+json` with RFC 9457 fields and legacy `code`, `message`, `data`.
Recovery version 1 adds `retryable`, `next_action`, `retry_after_seconds`, `correlation_id`, and
`violations` (field, problem, valid_values). Resource references are omitted unless read access has
been verified; current errors conservatively omit them. Authentication errors disclose no resource
details. MCP domain failures return `isError: true` with the same structured payload; malformed
protocol use remains a protocol error. The explicit inventory is `app/recovery.py`. Lookup timeouts and
supplier HTTP 502/503 failures propagate the retry contract; other failed supplier responses retain
spec 01's warning-based degradation. Cached availability ages are recomputed after missing-product
fetches so cut-offs use the time when the combined result is returned.

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
`await ReferenceBuyer(call_tool).purchase(need_json)`. Lost responses (including MCP SDK request-timeout and connection-closed errors) retry the unchanged command/key;
expiry, throttles and approval waiting are driven by `next_action`, never prose or domain codes.

Policy `rate_limits` has `lookup`, `mutation`, `read`, each with `capacity` and `refill_per_minute`.
Defaults are 60/60, 120/120, 600/600 respectively. An agent's `agents.<id>.rate_limits` overrides all
three buckets. Limits take effect on the next request without resetting consumed tokens. Exhaustion
returns `429 RATE_LIMITED`, `Retry-After`, and retry guidance before Decisions or supplier work.
Throttles are audited and counted in `/v1/stats.throttles`. Supplier admission is globally bounded
within the process: 8 active calls, 16 waiting, a 5-second queue wait; overflow/timeouts yield retryable
`503 UPSTREAM_BUSY`. Unknown credentials have a separate source-address bucket before credential
resolution (60 attempts, 60/minute). A digest-membership gate lets recognized credentials bypass that
unknown-credential bucket while their principal buckets still apply. An approval inbox request costs one
read token, regardless of its pending-order count; standalone quote reads still cost a read token. Limits are
**in-process, single instance**; SQLite commands/events remain shared across processes.

Valio outbound requests share a process-wide one-token bucket refilling at 5/s, with a rolling-window
ceiling of five calls in any second. Dispatch is serialized and refills from completion so delayed
senders cannot accumulate a burst. HTTP 429 pauses uncached upstream work for `Retry-After` seconds
(delta seconds or HTTP date; 30 seconds when absent/invalid). Affected REST/MCP calls return
`503 UPSTREAM_RATE_LIMITED`, `retryable: true`, `next_action: RETRY_SAME_COMMAND`, and the remaining
`retry_after_seconds` (REST also sends `Retry-After`). Cached results remain available; explicit fresh
quote/order checks wait for recovery via the same error. One `UPSTREAM_RATE_LIMITED` audit event is
written per block and counted in `/v1/stats.upstream_rate_limits`. Public `GET /health` includes
`upstream {blocked_until, calls_last_minute}`: UTC end of the active block or null, and process-wide
outbound calls during the preceding 60 seconds. The mock has no order API; gateway-simulated orders
for human approval are sufficient and nothing is sent to a supplier.

`GET /v1/me` returns the resolved principal and current limits/remaining tokens. Principals include
kind, ID, organisation, customers, role and a digest credential reference. Policy `organisations`
maps IDs to `{customer_ids: [...]}`; `identities` maps credential IDs to `{kind, role, organisation_id}`.
Buyer roles are `buyer_admin`, `buyer_approver`, `buyer_viewer`; suppliers use `supplier_agent`.
`scripts/gen_keys.py --write` provisions every ID in `agents` and `identities`, with the same secret
format and without printing secrets. Buyer roles authorize the customer-scoped `/v1/buyer` operations
described above; global administrative operations retain their operator-only guards.

Public `GET /v1/meta` separates specification versions from installed packages and states limiter
scope. A2A 1.0, Standard Webhooks v1a and the draft MCP server card are implemented.
The server-card draft is pinned to vendored commit `526201bbc80231daa40ffcdecfc9da4e54e5dc93`;
Standard Webhooks is pinned to vendored commit `36afae792227eae8a182a7a1a5711dbdc08a4334`.

## Graduated autonomy (spec 40)

`GET /v1/agents/{id}/track-record?window_days=30` is available to the operator, the agent itself,
and buyer users in its organisation. Explicit organisation IDs must match; for older agent mandates
without one, all mandated customer IDs must belong to the caller's organisation. Unknown or
out-of-scope agents return 404. The window accepts 1–3650 days and performs no supplier calls.

The record uses orders **placed within** the inclusive UTC window, preserving historical human
approvals even if later cancelled. It counts distinct orders placed, approved unchanged, rejected,
expired, evidence-changed and cancelled. Approved unchanged means a human approval at evidence
revision 1 with no `EVIDENCE_CHANGED` audit entry; re-approved revised evidence does not qualify.
Average approval time (seconds) covers every human-approved order in the cohort, from placement
until approval. Shares divide by orders placed (zero for an empty cohort); unavailable averages are null.
Hard-constraint refusals count distinct orders (or attempts with no order) refused in the time window,
including failed approval refreshes. A placement counts only when its outcome is `REJECTED`;
an exception routed to and accepted by a human does not start a refusal cooldown.
Indexed order, quote and actor ownership queries restrict audit processing to the target agent.
Last rejection/refusal dates are SQL aggregates over that agent's lifetime history so an older
failure still enforces its cooldown without loading historical JSON into Python. Replays do not add observations.

`autonomy_recommendation {eligible, reasons[]}` checks `autonomy.promotion`:
`min_approved_unchanged` (5), `max_rejection_share` (0.1), and
`days_since_last_rejection_or_refusal` (7). Every unmet threshold is explained. With no historical
failure its cooldown passes; meeting a threshold exactly passes. Readiness never changes authority.

An operator or the agent's `buyer_admin` calls
`POST /v1/agents/{id}/autonomy` with `{level, reason}` (nonblank reason required).
An optional `Idempotency-Key` returns the original result on retry without changing authority again,
even after a later demotion; changing the payload under the same key returns `409 IDEMPOTENCY_CONFLICT`.
The level persists in the runtime policy using its existing atomic replacement and writer lock.
For a VC-only agent it persists in SQLite without creating a policy mandate, so subsequent purchases
still require a valid, unrevoked VC with its expiry and limits enforced;
`AUTONOMY_CHANGED` records the previous/new levels, reason and 30-day track-record snapshot.
The human can grant autonomy even when readiness is not eligible. A stricter buyer VC cannot be
loosened by this endpoint: the buyer must issue a new credential. `scripts/issue_mandate.py`
accepts `--autonomy` and defaults to `approval_required`.

A human rejection or hard-constraint refusal while autonomous appends an audited
`autonomy.review_suggested` domain event, delivered through the existing durable event/outbox
mechanism. This suggests review; demotion is the same endpoint with `level: approval_required`.
For a VC-bound order, this decision uses the original signed claim and current policy/grant,
not its placement-time effective autonomy snapshot. A restrictive buyer claim still wins;
expired or revoked authority does not prevent human rejection and does not suggest an autonomy review.
The approval endpoints expose buyer/operator signing. Spec 33 delivers subscribed purchasing events
through signed webhooks independently of the decision; autonomy advisory events remain in the durable
event/outbox for activity consumers.

## Signed webhooks (spec 33)

Create with an agent or reviewer Bearer credential:

```http
POST /v1/webhook-subscriptions
Idempotency-Key: subscribe-hotel-decisions
Content-Type: application/json

{"url":"https://buyer.example/hooks","customer_ids":["1"],"event_types":["order.approval_required","order.confirmed"],"audience":"agent"}
```

The receiver must answer the signed `{"type":"webhook.challenge","challenge":"…"}` POST
with HTTP 2xx and `{"challenge":"…"}` containing exactly the same challenge. Only then is the
subscription enabled. HTTPS and public addresses are required. Each attempt resolves DNS again,
rejects any nonpublic answer, and pins the connection to a checked address while retaining the
original Host and TLS SNI. Redirects are never followed and environment proxies are disabled.
Challenge responses must be uncompressed JSON, at most 16 KiB, with a five-second overall deadline
checked during streaming as well as HTTP timeouts. Challenge I/O runs before the database writer is acquired,
including keyed registration/update and A2A push registration. Delivery responses close after status
headers without reading their bodies.
Only explicit `WEBHOOK_SANDBOX=true` permits `http://localhost` resolved to loopback.

`GET /v1/webhook-subscriptions` lists owned subscriptions. `PATCH /v1/webhook-subscriptions/{id}`
changes the destination, customer/event filters or enabled state; changed destinations and re-enabling
require a new challenge. `DELETE` disables and retains history, atomically marking pending deliveries `skipped`.
Dispatch also retires disabled backlog from earlier releases. Only enabled subscriptions enter the batch,
round-robin by subscription. The generic outbox selects enabled consumers with installed handlers.
All writes accept `Idempotency-Key`.
Keys bind the principal, command route, subscription/delivery targets and payload. Exact replays
recheck current ownership, customer scope and reviewer authority before returning the saved result,
without repeating the challenge or redelivery. Previously stored command envelopes remain replayable.
An approver audience requires an operator or buyer approver/admin credential. Customer scope is checked
at subscription time and again at delivery against the current policy. Recorded aggregate ownership also
applies: two agents sharing a customer never share procurement events. Buyer subscriptions require an
explicit agent organisation assignment. Unowned historical records remain operator-only.

Supported event types: `order.approval_required`, `order.evidence_changed`, `order.confirmed`,
`order.rejected`, `order.cancelled`, `order.expired`, `mandate.revoked`, `proposal.received`, and `suggestion.shown|prepared|made_recurring|dismissed|snoozed` (each a separate `suggestion.*` event type).
The procurement core retains its existing `order.placed` domain event; the webhook projection names
the initial purchasing outcome appropriately. The envelope retains the durable event ID, customer,
aggregate ID/revision, correlation and payload. Multi-customer revocations project one scoped event
per customer and mandate without exposing another customer's IDs.

The outbox supplies at-least-once delivery. Every subscription/event pair has one immutable body,
one stable `webhook-id`, and a separate delivery record. Five attempts use exponential intervals
(2, 4, 8, 16 seconds before the next attempt) with 0–25% positive jitter. Each attempt gets a fresh
timestamp/signature. API lifespan runs the worker every 0.5 s, and commits also trigger dispatch.
`GET /v1/webhook-subscriptions/{id}/deliveries` shows status, attempt count, next retry and last
HTTP status. Exhaustion sets `failed`; `POST .../deliveries/{delivery_id}/redeliver` restarts attempts
with the same ID and body. A delivery failure never changes the order's purchasing decision.

Signatures implement the vendored Standard Webhooks revision
`36afae792227eae8a182a7a1a5711dbdc08a4334`: Ed25519 `v1a`, standard Base64 signature,
over the exact bytes `webhook-id.webhook-timestamp.raw-body`. The signing key is the gateway key
from spec 10; public keys are at `/.well-known/jwks.json`. Receiver example (verify before parsing,
then durably deduplicate on `webhook-id` before performing effects):

```python
import base64
import time
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

def verify(body: bytes, headers, trusted_jwks):
    timestamp = headers["webhook-timestamp"]
    if abs(time.time() - int(timestamp)) > 300:
        raise ValueError("Stale webhook")
    signed = headers["webhook-id"].encode() + b"." + timestamp.encode() + b"." + body
    for item in headers["webhook-signature"].split():
        version, _, signature = item.partition(",")
        if version != "v1a":
            continue
        for jwk in trusted_jwks["keys"]:
            if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
                continue
            try:
                public = base64.urlsafe_b64decode(jwk["x"] + "=" * (-len(jwk["x"]) % 4))
                Ed25519PublicKey.from_public_bytes(public).verify(
                    base64.b64decode(signature, validate=True), signed)
                return headers["webhook-id"]
            except (InvalidSignature, ValueError):
                pass
    raise ValueError("Invalid webhook signature")
```

Approver notifications include `/approvals/{order_id}?revision=N`. An authenticated GET renders the
reviewed evidence next to independent notification status; a stale link returns `EVIDENCE_CHANGED`.
The page submits approval only through the existing authenticated POST with `evidence_revision=N`.
No credential appears in the link. Fetch/display the review with the reviewer Bearer credential;
the approval form requests that credential for its POST.

## Buyer reporting and portable evidence (review corrections)

Buyer activity resolves each procurement event's agent from its order, quote, lookup or task and applies
the same organisation/customer scope as the order view. Customer-number overlap grants no ownership;
agents without an organisation assignment remain operator-only in buyer views. Historical aggregates
without context still load and resolve by their stored agent. Evidence bundles keep foreign denied
attempts as hash-only chain links; timelines omit those payloads. Safe immutable snapshots remain available;
old snapshots containing a foreign denied payload return 404 and must be regenerated from the current endpoint.
The original audit and stored orders/quotes/lookups remain intact.
The spending view expires pending orders before its snapshot and verifies issuer trust, signature,
validity and revocation before selecting a VC limit; invalid credentials leave the policy limit in effect.

## Ingredient baskets (spec 32; shared with A2A)

`POST /v1/basket-lookups` and MCP `find_basket_offers(request)` accept
`{customer_id, deliver_by, manifest:{version:1, recipe_name, servings, base_servings:1, ingredients:[...]}}`.
With explicit `base_servings`, quantities describe that many portions and scale by the target divided by
`base_servings`. Without it, `manifest.servings` is the base and optional request-level `servings` is the
target; omission keeps the supplied quantities. The scaled manifest records the quantities for its servings. Units are G, KG, ML, L and SALES_UNIT. Each ingredient supplies name, quantity, optional
search terms, claims/allergen exclusions and `substitutions_allowed`. Strict catalog search and hard
constraints remain in force; unrelated ingredient substitutes are never silently introduced. Explicit
`density_kg_per_l` permits mass/volume conversion; missing conversion evidence leaves an ingredient uncovered.
Standing constraints apply per ingredient. Distinct candidate SKUs share one price batch and one stock
batch; overlapping ingredients share a purchase line. Uncovered ingredients require partial-basket review.
The result includes ranked ingredient offers, scaled quantities, coverage, overbuy, total and policy preview.

Quote with `basket_ref:{basket_lookup_id}` instead of lines/plan_ref. The signed quote preserves per-SKU
request constraints for current-policy revalidation at placement/approval. One simulated order commits all
reservations in one transaction; its context states that atomicity applies to the local gateway, with no
supplier transmission. Historical single-need lookups, signatures and order records retain their stored shape.

Agent-side example (lasagne for 200; `AGENT_TOKEN` holds an issued agent key):

```sh
.venv/bin/python demo/run_recipe_order.py --remote --customer 1 --deliver-by YYYY-MM-DD
# Override the agent's decomposition with --manifest /path/to/manifest.json.
.venv/bin/python -m pytest tests/test_basket_recipe.py -q
```

## A2A service (spec 34)

The REST API also serves public `GET /.well-known/agent-card.json` and authenticated
`POST /a2a`. Use `Authorization: Bearer …`, `A2A-Version: 1.0`, optional `Agent-Mandate` and
`X-Correlation-Id`. The installed `a2a-sdk==1.3.0` supplies the 1.0 protobuf types, JSON-RPC routing,
validation, error details and SSE encoding. The protocol version is **1.0**; the package version is
listed separately in `/v1/meta`. The Agent Card takes tool descriptions/input/output schemas from
the shared MCP server and lists `find_offers`, `find_basket_offers`, `quote_and_order`, `order_status`,
`cancel_order`, `get_evidence`, bearer security, streaming and push.

SDK clients send a user Message with a stable `messageId` and one structured data part:

```json
{"message":{"messageId":"lookup-1","role":"ROLE_USER","parts":[{"data":{"version":1,"command":"lookup","need":{"customer_id":"1","intent":"milk","quantity":{"value":400,"unit":"L"},"deliver_by":"2026-11-17"}}}]}}
```

`SendMessage` returns a task with request/offers artifacts. Continue with `message.taskId` and
`message.contextId`, a new message ID, and `{version:1, command:"select_plan", plan_index:0}`, then
`{version:1, command:"place_order"}`. Quote and order use the message ID as their shared command
idempotency key. Repeating an identical message returns the same task; changed content with the
same ID conflicts. Exact replays use read cost and reauthorize the original command binding against current
scope, suspension and bound VC. Refusals retain failure audits without repeating purchases. Fresh commands
pay admission cost before any task/message/event is persisted, including invalid request details; admitted
core commands are not charged twice. Admission throttles return recovery data without creating a task.
Retryable failures after admission retain the task and resume on the same message ID.
Protobuf Value represents numbers as doubles, so integral numbers are restored to integers before
the shared strict validators; strings and booleans are never coerced.

A successful replacement lookup clears selected-plan/quote references and artifacts, including basket
references when changing back to a single need. Placement also rejects stale bindings from older releases.
Tasks retain principal/context and request, offers, selected-plan, quote, order and evidence references.
Artifacts contain both a structured data part and a text summary. `WORKING` is durable before supplier
I/O; plan selection/missing input gives `INPUT_REQUIRED`; purchasing approval gives `AUTH_REQUIRED`;
confirmation gives `COMPLETED`; rejection, expiry/failure and cancellation give the corresponding
1.0 states. Human approval stays in the gateway reviewer POST flow. Order outbox events update the
task and receipt artifact even when the purchasing decision arrives through another interface.

The structured `lookup` command can invoke the shared basket adapter with
`skill:"find_basket_offers", arguments:{…}`; `arguments` follows that MCP tool's published input schema.
Selection then quotes its stored `basket_ref`. `get_evidence` returns spec 29's signed manifest with
authenticated immutable artifact and ZIP links, using the same evidence core as REST and MCP
`get_order_evidence`. The bundle contains the signed quote/receipt, audit chain and checkpoint, UBL,
JWKS and verification instructions. Basket and evidence adapters remain owned by specs 32 and 29.

`GetTask` and `ListTasks` are principal-scoped (including kind); another principal receives task-not-found.
List supports context/status/timestamp filters, page size/token, history length and artifact inclusion.
`SendMessageConfiguration` supports accepted output modes, bounded history, `returnImmediately` and
task push configuration. `CancelTask` applies the existing order cancellation rules. A completed task
is terminal: cancel its confirmed order using a **new** message without taskId,
`{version:1, command:"cancel", order_id:"…", reason:"…"}`. This creates a new cancellation task.

`SendStreamingMessage` emits task status and artifact updates in durable revision order while the command
runs. `SubscribeToTask` replays the outbox-backed task events and follows pending human approval until a
terminal state; an input-required task closes so the client can supply its next message. Resume with
`Last-Event-ID` from update metadata's `event_id`, or `X-A2A-After-Revision` from `aggregate_revision`.
Without a cursor it replays all retained revisions. Subscriptions drain through the current aggregate revision
before closing, including approval committed between polling reads. Consumers can deduplicate by event ID/revision.

All 1.0 push configuration methods are installed: `CreateTaskPushNotificationConfig`,
`GetTaskPushNotificationConfig`, `ListTaskPushNotificationConfigs`, `DeleteTaskPushNotificationConfig`.
They use spec 33 ownership challenges, destination safety, retry bounds and signing. Push bodies are
SDK-compatible A2A `StreamResponse` status events, with optional `X-A2A-Notification-Token` and configured
Authorization, plus Standard Webhooks headers. Configuring push also queues the latest task snapshot.

Domain failures carry the same recovery version 1 payload in the task status data part and A2A's typed
`google.rpc.ErrorInfo.metadata` in JSON-RPC `error.data`; throttles include `retry_after_seconds`.
Authentication failures use HTTP 401 problem details before protocol parsing. Per-agent lookup,
mutation and read buckets are shared with REST/MCP. Purchasing audit entries remain identical; A2A adds
separate `A2A_TASK_UPDATED` lifecycle audit entries, not additional purchase decisions.

Reference split-order client (no operator credential, approval remains external):

```bash
.venv/bin/python demo/a2a_client.py --url http://localhost:8000 --token "$AGENT_TOKEN" --need /tmp/need.json
# Add --mandate /path/to/buyer-vc.jwt when using a buyer-issued mandate.
.venv/bin/python -m pytest tests/test_webhooks.py tests/test_a2a_service.py tests/test_cross_interface.py -q
```


## MCP discovery and connect kit (specs 37–38)

Start the API from `backend/` with `.venv/bin/python -m uvicorn app.api:app --port 8000`.
The same process now serves authenticated, stateless Streamable HTTP at `/mcp`, sharing the
REST gateway, principal, budgets and idempotency state. Standalone MCP stdio and port 8001
remain available with a separate database; never open the gateway's live database in a
second process. The machine identity is `fi.valioaimo/trusted-trade-agents`, version `0.1.0`;
the display title remains Valio Aimo Trusted Trade Agents. Tools are listed at runtime,
including `get_agent_context` (the same advisory snapshot as authenticated `GET /v1/me`) and
`get_order_evidence` (the portable evidence manifest and authenticated download links).

A client starting with only the base address follows `/.well-known/ai-catalog.json` to
`/mcp/server-card` and then `/mcp`. The card follows the vendored draft schema at commit
`526201bbc80231daa40ffcdecfc9da4e54e5dc93`, also recorded in `/v1/meta` and the namespaced
card extension. Startup validates the schema and compares runtime identity/transport.
Public discovery is read-only, has wildcard CORS, cache validators and no customer authority.
In production set `GATEWAY_PUBLIC_URL` to the HTTPS public origin; MCP's Host/Origin
protection otherwise accepts only loopback addresses. Never disable that protection to
work around a proxy configuration.

`/developers` contains the connection guide, configurations, executable REST walkthrough,
recovery and approval rules. `/llms.txt` links agents to it, OpenAPI and
`/developers/tools.json`. Author the prose in `backend/docs/developers.html.in` and
`backend/docs/llms.txt.in`; regenerate the committed static artifacts after tool changes:

```bash
.venv/bin/python scripts/build_developer_docs.py
.venv/bin/python scripts/export_openapi.py
.venv/bin/python -m pytest tests -q
```

Generation imports and registers tool schemas without opening a database or signing keys.
The divergence acceptance test compares complete runtime schemas and generated HTML.

The independent buyer is `clients/python/reference_buyer.py`, with pinned dependencies
and environment names in that directory. From the repo root, install its requirements,
export `GATEWAY_URL`, `AGENT_TOKEN`, optionally `AGENT_MANDATE` (VC-JWT), then run:

```bash
python clients/python/reference_buyer.py --scenario full_stock
python clients/python/reference_buyer.py --scenario split_stock
```

The client requires context environment `sandbox`. An operator must prepare the fixture
supplier/catalog and policy beforehand: full_stock covers 10 L autonomously; split_stock
covers 400 L with a split plan and requires human approval. It follows `next_action`, polls
pending orders, retains mutation keys on retries and verifies the Ed25519 receipt against
public JWKS. It cannot approve its own order. This kit does not call sandbox reset
or credential issuance: use a separate deployment/storage/credentials and set
`GATEWAY_ENVIRONMENT=sandbox`; spec 26 now provisions isolated fixture adapters, storage and sandbox-only credentials.
Environment credentials expire by issuer removal/rotation; connection keys expire by revocation.
A refused key returns 401 with
replacement guidance. Offline verification of a saved order is also available:

```bash
python clients/python/verify_receipt.py order.json --jwks jwks.json --customer ID --agent AGENT_ID
```

Tests use only temporary storage, mocked supplier calls and in-process ASGI, including the
real MCP SDK connection and a shell walkthrough whose HTTP transport is bridged to TestClient.
Claude Code configuration syntax is checked, Desktop stdio executes as a subprocess in the
MCP transport suite, and the installed generic CLI loader checks `app/mcp_entry.py` without
launching the optional inspector UI. To use that UI, install `mcp[cli]==2.3.0` and run
`MCP_AGENT_ID=issued-agent .venv/bin/mcp dev app/mcp_entry.py` on the gateway host.
## Hospital substitutions (spec 31)

`POST /v1/offers` / MCP `find_offers` accepts optional
`function: {product_type, intended_use, preparation?, acceptable_units: ["L"]}` and
`substitute_for: {product_id}`. Candidates must share the catalog product category and pass functional
fit screening; the fit question includes intended use and preparation. An available preferred product is best
and has no substitution proposal. If it is short, alternatives rank by category, claims superset, pack size
and comparable unit-price difference. Each alternative has `substitution: {for, differences, allergen_evidence}`.
Contains, may-contain, undeclared, contradictory and missing allergen evidence never pass an exclusion.
The wording is **compliant with the recorded procurement constraints**, not a clinical safety guarantee.

A customer's optional `substitution_profile` contains `allowed_brands`, `max_price_increase_pct`,
`pack_size_range: {min, max}` (in the need's L/KG per sales unit), and `requires_approval`.
Out-of-profile or unverifiable price changes require approval. `clinical_review: required` on the customer
is carried by quote/order and forces sign-off by a **buyer_approver scoped to that buyer**. Operator-only and
buyer-admin-only clinical approvals are refused with `BUYER_APPROVER_REQUIRED`. Hard allergen rules remain
binding even for the buyer approver. `policy.yaml` contains a hospital policy example.

## Recipe baskets (spec 32)

`POST /v1/basket-lookups` / MCP `find_basket_offers(request)` takes `customer_id`, `deliver_by`,
`manifest` and optional target `servings`. The version 1 manifest contains `recipe_name`, base `servings`,
`ingredients: [{name, quantity: {value, unit}, search_terms?, required_claims?, exclude_allergens?,
substitutions_allowed, density_kg_per_l?}]`, and optional `yield_note`. The agent supplies the recipe
and its decomposition; the gateway scales supplied quantities, searches the catalog and applies standing
constraints to every ingredient. `substitutions_allowed: false` confines retrieval to the supplied terms;
true permits the ordinary deterministic intent broadening when no explicit terms were supplied.
It does not relax hard constraints.

All candidate SKUs are deduplicated before **one price batch and one availability batch**. Per-ingredient
results hold ranked offers, `chosen_line` (the shared product ID), purchasable sales quantity, overbuy and
coverage. The basket holds `lines`, `total`, `coverage_by_ingredient`, `uncovered`, and `policy_preview`.
Quantities for a shared SKU are summed before whole-pack rounding. `scaled_manifest` shows target quantities.
G and ML normalize to KG and L. Mass/volume conversion requires explicit ingredient `density_kg_per_l` or
catalog `synkkaData.densityKgPerL` evidence (kg/L); otherwise the ingredient is uncovered with
`unit cannot be converted`. No density is guessed. Ingredient indices distinguish duplicate names.

Retrieve saved baskets with `GET /v1/basket-lookups/{lookup_id}`. Quote with
`POST /v1/quotes {customer_id, deliver_by, basket_ref: {basket_lookup_id}}` (also supported by MCP
`create_quote`). `lines`, `plan_ref`, and `basket_ref` are mutually exclusive. Basket ownership, customer/date,
mandates and per-SKU ingredient constraints are rechecked. A partial basket carries protected
`PARTIAL_BASKET` into the quote and order and needs approval. Empty baskets cannot be quoted.
One quote produces one order; SQLite commits all known-stock reservations together. If any basket line has
insufficient stock, no basket lines are reserved and approval cannot confirm until all are available.
The order's `atomicity` states **all-or-none local simulation**; no supplier order is transmitted.

Offline demos (run from `backend/`):

```sh
.venv/bin/python demo/run_hospital_substitution.py
.venv/bin/python demo/run_recipe_order.py
```

These use versioned `sandbox/scenarios/hospital_stockout.yaml` and `full_stock.yaml` through
`FixtureSupplierAdapter`, the same policy/evidence/approval/order services, and separate temporary databases,
keys, policy, budgets and reservations. Contexts are labelled `sandbox` and observations have fixture
provenance. They need no network or secrets and never touch `backend/var/`. Each prints a report path under
`$TMPDIR`. The hospital demo includes buyer approval; the recipe demo decomposes lasagne on the agent side
and orders for 200. The deployment sandbox and its admin reset API are documented below (spec 26).

## Exception journey (spec 30)

`GET /v1/orders/{id}` and MCP `get_order_status` expose a durable `order_revision`,
`approval_reason` (one sentence from the recorded rule hits), `expires_at` and recovery guidance.
`PENDING_APPROVAL` means `next_action: WAIT_FOR_APPROVAL` and `poll_after_seconds: 5`;
poll after that interval using the same order id. `CONFIRMED` has null next action;
`REJECTED` keeps the approver's comment and means `STOP`; `EXPIRED` means `LOOKUP_REQUIRED`;
`CANCELLED` means `STOP` and retains the cancellation reason. Each status or decision-evidence
change increments `order_revision`; polling and idempotent replays leave it unchanged.
`evidence_revision` still identifies the evidence a human must review (spec 17).

`GET /v1/orders/{id}/receipt` and MCP `get_order_receipt` return the decided order resource,
including its spec 10 detached signed `receipt`; the receipt id is the order id. Verify it with
`receipt_payload` and independently trusted gateway JWKS. While pending they return
`409 NOT_DECIDED`, `next_action: WAIT_FOR_APPROVAL`, and `data.poll_after_seconds`.
Both enforce the same ownership as order status. The purchase remains `gateway-simulated`.
Split and partial lookup plans include `shortage {requested, best_single_coverage, reason}`;
both quantities have `{value, unit}`, measured against eligible on-time whole-pack stock.

From `backend/`, print an offline MCP demo transcript:

```sh
.venv/bin/python demo/run_exception_journey.py
```

The `split_stock` scenario has two lactose-free 5 L products, each with 200 L available:
400 L costs 1,708.00 EUR against a 1,500.00 EUR autonomous per-order limit. A separately
labelled scripted human-side reviewer approves after the suggested polling interval.
The client then prints the outcome, signed receipt id and evidence manifest. State and all
sandbox data use temporary files outside the repository; `backend/var/` is never touched.

Against a running MCP gateway backed by the live mock (an actual human must approve in the inbox):

```sh
# AGENT_SECRET holds this server's agent credential; optionally add --mandate /path/to/vc.jwt.
.venv/bin/python demo/run_exception_journey.py --live --customer 1 \
  --mcp-url http://127.0.0.1:8001/mcp --state "$TMPDIR/exception-live.json"
```

Keep the state file and rerun the same command after interruption. The quote command and
placement key are saved before sending; uncertain responses retry the exact same command,
and a saved `order_id` resumes polling without placing again. If live stock offers no complete
split plan, the demo explains that and places no order. The sandbox is isolated per run;
cross-process restart state applies to an existing live gateway. No new backend setting is required.
# Recurring orders and supplier proposals (specs 35–36)

`POST /v1/recurring-orders` takes `approved_order_id` (a confirmed order),
`cadence: {rrule, timezone, start_at, catch_up: "once"}` and
`permitted_deviations: {price_pct, cumulative_price_pct, quantity_pct,
cumulative_quantity_pct, brand_change, pack_change}`. Percent tolerances default
to zero. The template preserves approved product lines and purchased quantities alongside the bound need or scaled recipe basket; its
delivery lead time is retained. The original agent needs policy `recurring: true`
or a signed mandate credentialSubject `recurring: true`. This permission does not
relax normal order limits, standing constraints or autonomy rules.

GET the collection and `/{id}/runs`; POST `/{id}/run-now` with an optional aware
`occurrence_at` for demos. One occurrence key per template/local date is durable
across retries and restarts. Every run performs a fresh lookup and quote, compares
against both the previous run and the approved baseline, and enters the normal
order policy. Drift adds `recurring_drift` rule hits before an order is signed or
confirmed. Missing historical comparison facts require review. Catalog facts are
preserved in signed quote context so a later catalog edit cannot rewrite the
approved baseline. `POST /{id}/baseline {approved_order_id}` requires a scoped
buyer_admin or buyer_approver and increments the baseline revision.

REST and the MCP CLI run an in-process scheduler. Supported RRULEs use DAILY,
WEEKLY (optionally BYDAY=MO,TU,...) or MONTHLY (optionally BYMONTHDAY), with an
optional positive INTERVAL. Times follow the named IANA timezone across DST;
after downtime only the latest missed occurrence executes, once. Revoked authority
or suspension produces a skipped run and a `recurring.stopped` domain event.

Buyer_admin uses `GET/PUT /v1/buyer/proposal-preferences` with optional
`?customer_id=1` for multi-customer buyers. Empty categories/interests mean no
opt-in. Preferences include `categories`, `interests` (matched against product
names/claims), `channels: ["inbox", "webhook"]`, `cadence_limit_per_week`, IANA
`timezone`, `quiet_periods: [{start: "22:00", end: "08:00", weekdays: [0,1,2,3,4]}]`,
and `muted_campaigns`. Weekdays start at Monday=0; equal start/end means all day.
Weeks start Monday in the buyer timezone. Policy `proposal_quotas` caps
`per_supplier_per_week` (UTC weeks) and `per_customer_per_week`; supplier quota
counts submitted campaigns, including campaigns held by customer filtering.

A credential whose policy identity has `kind: supplier_agent` submits
`POST /v1/proposals` with `campaign_id`, product IDs, rationale, `valid_until`,
`target` (exactly one of `categories` or `customer_ids`) and one price evidence
entry `{product_id, unit_price, currency: "EUR", fetched_at}` per product. An
unexpired supplier-owned `quote_ref` may replace the price evidence. Rationale is
untrusted content. Standing constraints and evidence rules filter each product
before delivery. Quiet periods/cadence/quota holds and excluded products are
recorded in buyer-scoped audit evidence. Equivalent unexpired campaigns from the
same supplier merge by product/price/currency fingerprint; repeated campaign IDs
also merge, with no additional delivery. Supplier responses and
`GET /v1/proposals/campaigns/{id}` expose only aggregate delivery/action counts.

Buyer agents and scoped buyer users read `GET /v1/proposals/inbox`; MCP offers
`list_proposals`. POST `/{id}/accept {need, buyer_agent_id?}` re-enters normal
lookup/quote/order checks, under the buyer agent's mandate, with fresh supplier
price and stock. Buyer users must name an agent in their own organisation.
Selection chooses the highest-ranked offered product meeting that need. Expired
proposals are refused. A changed advertised price returns `REQUOTE_REQUIRED` with
the new signed quote and old/new unit prices, without an order; a buyer can
explicitly accept that quote through the standard order endpoint. Accept retries
return the same order. POST `/{id}/decline` or `/{id}/mute` records the buyer action;
mute adds the campaign name and equivalent-content fingerprint to buyer consent.

`proposal.received` events are written atomically to the durable outbox with customer scope and
consented channels. The signed webhook service (spec 33) delivers through scoped subscriptions and
requires `"webhook"` in both `event.payload.channels` and the current buyer preferences on every attempt, including retries. Inbox-only consent never permits an external proposal webhook.

Demo: configure the supplier identity and buyer opt-in, then run
`SUPPLIER_AGENT_KEY=... .venv/bin/python demo/run_sales_agent.py` from `backend/`.
The default campaign advertises Valio Arki Eila 1 L at EUR 1.01 for customer 1;
`--product`, `--unit-price`, `--customer` and `--base-url` override it. This sends
content only; it grants no purchasing authority and creates no order.

# Supplier adapters, sandbox and observability (specs 25–27)

`GET /v1/suppliers` lists capabilities, execution atomicity and synthetic labels. Set
`NeedSpec.supplier_id` to `nordfood` to use the **synthetic** second supplier; omission keeps
the existing Valio path. A stored need binds the supplier through lookup, quote and order.
Products use `(supplier_id, supplier_sku)` identities; GTIN is packaging evidence, never a lookup key.
Valio's catalog builder and paced/cache-aware client are behind `ValioAdapter` without changing the
recorded demo's best product or total. Nordfood is a different HTTP API with 60 fixture products,
case prices, minimum case lots, finite inventory and EUR/SEK. The default EUR-only policy refuses
SEK with `UNSUPPORTED_CURRENCY`; no implicit exchange rate is used. Agent policy and buyer VC
`supplier_ids` intersect to restrict authority, including on approval and idempotent retry.

From `backend/`, run `.venv/bin/python -m suppliers.nordfood` (localhost:8002), then the gateway as
usual. `NORDFOOD_API_BASE` changes its origin. This service is synthetic and keeps its acknowledgements
and stock in process memory for demonstrations; restart resets them. Tests use its actual ASGI app,
without sockets. `SupplierAdapter` exposes capabilities/catalog ingestion/normalised price and stock
facts. `OrderExecution` exposes submit/status/cancel and declares atomic, no-partial-acceptance execution.
The normalised evidence retains supplier SKU, GTIN, sales unit/conversion, net currency/validity,
stock semantics, requested/returned dates, source and observation time.

Every new order has `execution {mode, supplier_ref, acknowledged_at, status}`. Valio remains
`commitment: gateway-simulated`; Nordfood changes to `supplier-acknowledged` only after an accepted
service response. Rejected or pending-approval orders are never submitted. An uncertain submit returns
`PENDING_EXECUTION`, execution status `unknown`, and `next_action: RETRY_SAME_COMMAND`. Repeat
`place_order` with the **same quote and idempotency key** to reconcile it. The deterministic gateway
order UUID is the supplier's idempotency key, also protecting crash-before-local-commit retries.
Cancellation requires the supplier's cancellation acknowledgement. Supplier execution is not a payment.

The sandbox runs as a **separate deployment** of the same gateway. Its database is environment-bound:
one database cannot be opened by both environments. It has independent credentials, keys, customers,
reservations, budgets, command idempotency, caches, webhooks, outbox and audit. To start it:

```sh
.venv/bin/python scripts/gen_keys.py --sandbox-agent sandbox-buyer
GATEWAY_ENVIRONMENT=sandbox .venv/bin/python -m uvicorn app.api:app --port 8010
```

The script writes only `SANDBOX_AGENT_KEYS` and `SANDBOX_ADMIN_TOKEN` into the local env file and
prints names, never secrets. Live credentials are neither reused nor accepted. Default sandbox customer:
`sandbox-customer`; new sandbox agents require approval. Defaults and generated storage live under the
ignored `backend/sandbox-state/`, or `SANDBOX_DB_PATH` can point to an isolated temporary directory.
Live `DB_PATH`, signing-key and anchoring settings do not configure the sandbox. The workspace uses
`FixtureSupplierAdapter` for both suppliers and deterministic suitability fixtures, with the same
production evidence, policy, planning, signing and approval code. Fixture time starts at the scenario's
versioned clock; use delivery dates relative to that clock (listed by the scenario endpoint and as
`fixture_clock` in `/v1/me`). The reference buyer uses that clock automatically. Grant the sandbox agent
autonomy through the existing autonomy endpoint for an automatic full-stock rehearsal, or sign off its
orders with the sandbox admin credential.

`GET /v1/sandbox/scenarios` and `POST /v1/sandbox/reset {"scenario":"split_stock"}` require the
sandbox admin credential. They are refused in a live deployment. The seven versioned YAML scenarios
are `full_stock`, `split_stock`, `missing_evidence`, `shifted_date`, `expired_quote`,
`approval_time_stock_change`, and `supplier_timeout`. The approval-change scenario lowers stock after
a pending placement; the default approval-required sandbox policy supplies its human gate. The expiry
scenario advances fixture time after quoting. Reset preserves historical evidence and audit, expires
open quotes/pending orders with `expiration_reason: sandbox_reset`, releases reservations, clears command
idempotency and cache/limit state, starts a fresh spending epoch, and disables previous webhook deliveries.
Every HTTP response carries `X-Environment`; sandbox resources, signed quote/receipt payloads and UBL
notes explicitly carry `environment: sandbox`. Provenance names the scenario version and fixture source.

OpenTelemetry is opt-in: `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` enables OTLP/HTTP traces
and metrics; `OTEL_CONSOLE=1` enables console exporters. With neither variable, no provider/exporter
threads or automatic instrumentation are started. Export failures are isolated from procurement requests.
FastAPI/httpx are auto-instrumented; business spans cover catalog, fit, price, stock, planning, policy,
quote, placement, approval, webhook delivery and anchoring. Approval/delivery link to a persisted placing
span context; delivery starts a separate trace. Trace IDs are stored as hex strings for canonical JSON.
The exported attribute allowlist is in `app/telemetry.py`; automatic HTTP attributes, exception events,
credentials, raw prompts and personal data are excluded. Metrics include request/upstream latency,
upstream calls, cache hits, candidate counts, decisions, approval wait, throttles, backlog, delivery attempts
and expiries. REST, MCP and A2A set the transport attribute at their shared command boundary.

For the optional local observability stack, run `docker compose -f observability/docker-compose.yml up`.
Collector, Jaeger (16686), Prometheus (9090) and Grafana (3000, provisioned gateway dashboard) use pinned
versions, also exposed with installed OTel package versions in `/v1/meta`. No Docker/network is needed
for backend tests. Run all checks with `.venv/bin/python -m pytest tests -q`, then regenerate the contract
with `.venv/bin/python scripts/export_openapi.py`.


## Purchase memory and replenishment (spec 41)

Confirmed gateway purchases populate customer-owned memory atomically with the
confirmation. Pending/rejected orders do not count; cancellations remove their
memory entry. Existing confirmed orders are backfilled once on migration.
Each product/request-unit series retains dates, purchased quantities in the
request unit (including pack rounding), paid line totals, currency and agent.
Unknown conversions are omitted rather than guessed; units never mix.

Three confirmed purchases form a pattern. Intervals and quantities use medians;
spread means maximum minus minimum. Confidence is low if the interval median
is zero, interval spread exceeds max(3 days, 50% of median), or quantity spread
exceeds 50% of median. High requires at least six purchases, interval spread at
most max(3 days, 10% of median), and quantity spread at most 10%; otherwise medium.
Consecutive calendar-month purchases on the same day predict that day next month;
other rhythms use the rounded median day interval. The response explains confidence.

`GET /v1/suggestions?horizon=14` returns patterns predicted from today through
the horizon, with evidence, quantity, predicted date and a plain-language sentence.
Omit horizon to use each customer's preference (default 14; supported range 1–365).
Agents see their customers, buyer users their organisation, operators all customers;
supplier agents are refused. A presented VC restricts customer/category scope.

`POST /v1/suggestions/{id}/prepare` creates only a normal lookup with plans under
the caller's mandate. Quote and order separately through the normal endpoints.
`POST /{id}/make-recurring` uses the median quantity and detected cadence with the
latest confirmed product evidence scaled to that quantity as the spec 35 baseline.
It creates no quote/order; every execution revalidates mandate, policy and drift.
Optional body: `{timezone, permitted_deviations, buyer_agent_id}`; deviations default
to zero. Buyer approvers/admins (and operators) must name a purchasing agent;
buyer viewers can read only. Preparation also accepts `{buyer_agent_id}`.
The normal 60-day lookup date window still applies when acting on a suggestion.

`POST /{id}/dismiss` hides that need permanently; a new purchase generates a new
suggestion ID. `POST /{id}/snooze {days}` hides it for 1–365 days. Making it recurring
also hides that need. States survive restarts. Buyer admins use
`GET/PUT /v1/buyer/memory-preferences?customer_id=...` with
`{enabled, excluded_products, suggestion_horizon}`. Off hides suggestions and stops
recording purchases; restarting/enabling does not import purchases from that period.
Excluded products are neither recorded nor suggested. `DELETE /v1/buyer/memory`
records a forgetting request and permanently suppresses the customer's suggestions;
turning memory on does not undo that recorded request. Audit/order evidence remains.

MCP tools: `get_replenishment_suggestions`, `prepare_suggestion`,
`create_recurring_from_suggestion`, `list_recurring_orders`, `create_recurring_order`
(body argument `request` contains spec 35's approved_order_id, cadence and deviations),
`run_recurring_order_now`, `pause_recurring_order`. All four recurring workflow tools
require current explicit recurring permission; a missing permission returns
`RECURRING_NOT_AUTHORIZED` with `CONTACT_ADMIN`. Paused workflows cannot run.
Every shown, prepared, made-recurring, dismissed and snoozed suggestion writes an
audit entry plus a customer/environment-scoped `suggestion.*` event to the durable
outbox for delivery through customer-scoped signed webhook subscriptions (spec 33).
Supplier-backed orders enter memory only after acknowledgement, including reconciliation
of an initially uncertain submission; idempotent retries do not duplicate purchases.

Offline demo (temporary state only):
`.venv/bin/python demo/run_replenishment.py`. The `monthly_milk` scenario seeds six
real confirmed sandbox purchases from `sandbox/monthly_milk_history.json` (June–November
2026, roughly 1,000 L on each month's first), displays the December 1 suggestion,
prepares it over MCP, then makes it recurring. A 60-day query horizon includes it
on the hackathon date. Neither the demo nor tests touch `backend/var/`.


## Reviewed safety and correctness guarantees

Substitution profiles are re-evaluated at placement and approval under the writer lock, using signed quote-time brand, pack and comparable-price evidence. Locked prices never change. Missing historical comparison evidence remains readable and requires review. Functional product type determines eligible categories; same-category closeness is a ranking preference. Approval still follows spec 17: revision 1 at placement, incremented when approval-time decision evidence changes.

Basket demand uses rational arithmetic through scaling, density conversion and shared-SKU aggregation, followed by a single sales-step ceiling. Ingredient coverage measures unrounded content demand, while offer coverage measures purchased packs. Known-stock shortages always hold the basket for review and reserve none, even if PARTIAL_STOCK is ignored. Spec 39 still permits unknown-stock lines without local reservations.

Recurring snapshots sign physical pack content and physical-unit cost independently of SALES_UNIT requests. Comparisons use placement-refreshed delivery evidence before policy, receipt and run persistence. Catch-up retains the missed local-date occurrence key and uses an execution-relative delivery window. Recent recurrence calculations preserve cadence phase and DST wall time, skip old history arithmetically outside the writer lock, then revalidate the cursor under the lock. Fractional-second starts round upwards to RRULE second precision.

Campaign merging deduplicates each delivered customer and rechecks previously held or newly targeted buyers. A campaign can target at most 100 distinct customers across merged submissions; repeated identical held outcomes do not append filter/audit rows. Accept screens advertised products explicitly before the general shortlist limit, under normal constraints, fit, mandate, live price and stock checks. Signed mandates are forwarded for decline and mute, too. Proposal-derived quotes bind their source and recheck current consent and expiry at placement and approval.

Free-text request values are limited to 4,000 characters; string collections to 100 entries (narrower existing limits still apply), idempotency keys to 200, and revocation JWTs to 32,768. Refusal and rate-limit audit data use a bounded structured summary. Stored lookup text is read with historical validation context, so new request bounds do not invalidate old quotes/orders. Exact keyed retries recheck authority and replay the saved result at read cost.

`demo/run_exception_journey.py` retries SDK `MCPError` only for `REQUEST_TIMEOUT`, using its existing bounded same-arguments retry; other protocol errors propagate. The review-to-test mapping and implementation decisions are in `REVIEW_FIXES.md`.
