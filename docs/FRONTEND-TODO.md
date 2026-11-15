# Frontend and deploy to-do from the backend (14.11 evening)

What the console can add or change now that the backend serves it. Data and behaviour only; screens, layout and
wording are the frontend owner's call. Each item names the endpoints and the codes to handle. Field-level contracts:
`contracts/openapi.yaml` and the spec in `docs/specs/`. Status: **live** = on https://luotsi.online now;
**next push** = built and tested, lands on main tonight (endpoint names may still move slightly — the OpenAPI file in
that push is the truth).

## Latest changes

- **15.11 (claude.ai autonomy):** the gateway advertises CIMD again, so claude.ai connects as a *verified* client. After the owner presses "Yes, this is the same agent" on Connect, orders within limits confirm without approval. Reconnect claude.ai once.
- **Policy page:** the agent mandate has a `recurring` boolean (tekrarlayan sipariş izni) that the Agent mandates card doesn't show. A toggle on each agent card is enough; it saves through `PUT /v1/policy` like the other fields.
— read this first after every pull

Newest on top. Each entry says what landed, what the console can build on it, and any server step. Continue from the
top entry; the numbered sections below hold the details.

- **Done by the console (15.11): approvals now send the confirmation token automatically (api.ts).** Was: approvals did not complete in the console. Every human decision (approve, reject, cancel,
  policy reset, autonomy and governance writes) is two requests: the first answers `confirmation_required` with a
  summary and `confirmation_token`; repeat the identical request with that token within five minutes. The console's
  approve/reject sends only the first request (`frontend/src/pages/Approvals.tsx`), so approvals never finish. Handle
  `CONFIRMATION_INVALID` and `EVIDENCE_CHANGED`. This blocks the brief's third core requirement in the demo.

- **Push 5 (15.11, early morning) — the AI-gateway set: specs 47, 61, 62, 63, 64.**
  - Purchase intents (47): `POST /v1/intents`, amendments, resume by another agent, partial fulfilment; retries over
    any protocol give one order; `POSSIBLE_DUPLICATE_DEMAND` when the same need arrives twice.
  - Notifications to connected agents (61): consent per connection (`GET/PUT /v1/connections/{id}/notifications`;
    approval and order decisions on by default), delivered over the MCP stream, A2A push and webhooks; agents without a
    stream read `get_notifications`. Worth a consent checklist on the Connect flow and a "notifications" tab per connection.
  - Connection trust (62): OAuth clients show verified/unverified; pausing an agent closes its live streams at once;
    a key used from two places turns read-only (`CONCURRENT_CONTEXT`) and asks the owner; a new connection for an
    agent with earned autonomy waits for the owner to confirm it is the same agent. Show these states in connection health.
  - Contract and driving test (63): `GET /v1/trading-profile`; strict arguments (`UNKNOWN_ARGUMENT`,
    `ARGUMENT_REQUIRED`); `POST /v1/connections/{id}/driving-test` runs the sandbox test and order scope opens only
    after the unit, retry and approval checks pass (`DRIVING_TEST_REQUIRED`). A "run driving test" button and the
    signed result per connection fit the Connect page.
  - Handoff and delegation (64): `POST /v1/intents/{id}/handoff` and `/v1/handoffs/{code}/accept`; one holder at a
    time (`INTENT_HANDED_OVER`); research-only delegations with an actor chain shown in the timeline.
  - MCP now lists 109 tools; contracts/openapi.yaml has 259 paths.
- **Push 4 (15.11, ~00:15) — agent surface (spec 60) + recurring pause.**
  - MCP now exposes 79 tools across buyer/tenant/operator with annotations (read-only, destructive) and output schemas,
    readable resources (`luotsi://orders/{id}`, `luotsi://approvals/pending`, `luotsi://inbox`, …) with subscriptions,
    six prompts, and human questions inside the agent (elicitation) with the `input_required` fallback.
  - Signed agent cards and MCP server card; every tool result names the issuer; authenticated discovery is private.
  - One-click client setup: `GET /v1/connect/clients/{client}` (claude-desktop, claude-code, chatgpt, cursor, vscode,
    generic) returns ready configuration for the live URL — the Connect page can show it per client.
  - Connection health: `GET` health per connection (last used, calls by tool, errors, scope) and
    `POST /v1/connections/{id}/test` — worth a "Test connection" button and a small health panel.
  - Plain-language requests: A2A text or MCP `interpret_request` → structured draft the agent confirms.
  - Recurring orders: `POST /v1/recurring-orders/{id}/pause` and `/resume` now exist (section 11 correction resolved).
- **Push 3 (14.11, ~22:30) — brief scenarios (spec 46) + fixes.**
  - Basket lines accept `on_hand {value, unit}`; responses show needed / on hand / to buy per line.
  - Near `deliver_by`: lines carry `arrives_in_time` and, when late, `cannot_source_in_time` with in-time alternatives.
  - Substitutes carry a `trade_off` object (price per required quantity, pack change, allergen/claim difference,
    delivery date difference, one-line explanation); substitutes always need approval.
  - Stock-out watch: `POST /v1/stockout-watch/run`, `GET /v1/stockout-watch/inbox` (`STOCKOUT_PREDICTED` items with a
    substitute and trade-off; also a signed webhook).
  - Proactive selling: `POST /v1/proposals/suggest` (never-bought products in categories the buyer buys).
  - Nutrition limits in standing constraints (`max_salt_g`, `max_sugar_g`, `max_saturated_fat_g`, `min_protein_g`);
    products without nutrition data are excluded with a reason.
  - Five scripted demos: `backend/demo/scenario_1..5_*.py`; plain-language walkthrough in `docs/SCENARIOS.md`.
  - Fix: an agent whose customer grant was withdrawn now gets `403 CUSTOMER_NOT_IN_MANDATE` on suggestions.
- **Push 2 (14.11, 21:33, `76d5f1b`) — accounts, OAuth audiences, governance, guardrail profiles (specs 43–45).**
  Sections 2, 3, 4 and 5 below are now **live** (they still say "next push" in places).
  - Sign-in and sign-up: `GET /v1/auth/signup/options`, `POST /v1/auth/signup`, `POST /v1/auth/login`,
    `GET /v1/auth/session`, `POST /v1/auth/logout`, invites and password resets under `/v1/auth/...`.
  - Team and agents of a business: 56 endpoints under `/v1/tenant/...` (members, roles, agents, pause/resume,
    policy versions, simulate, activity, mandates, connections).
  - Connect page per audience: `GET /v1/connect/catalog?audience=buyer|tenant|operator`, `GET /v1/connect/tool-pack`.
  - Server: demo accounts need the six `DEMO_*_PASSWORD` values and one `reset_demo.py` run (section 8); done on
    luotsi.online per TASKS.
- **Next (being built one at a time):** capability specs 47–58 (purchase intents, untrusted input containment,
  constraint engine, commitment ceiling, resource ledger, document reconciliation, neutral ranking and RFQ, supplier
  fact integrity, decision authority, agent conduct guard, audit narrative, make-or-buy). Each lands with an entry here.

## 1. Fix now (live)

| What | Why | Backend |
|---|---|---|
| Buyer flow: treat offers carrying `NOT_AVAILABLE` as not orderable (disabled or clearly marked "out of stock") | A live order on 474971 was rejected because Valio has no stock for it; the lookup already ranks such items last and warns | warning code on each offer in `POST /v1/offers` |
| Buyer flow: request unit must match the product (L, KG or SALES_UNIT); chocolate in litres gives `SIZE_UNKNOWN` | the same rejected order also carried `SIZE_UNKNOWN` | `quantity.unit` in `POST /v1/offers` |
| Connect page: ChatGPT and claude.ai now connect through OAuth — replace "needs sign-in" with the steps: add `https://luotsi.online/mcp` as a connector, sign in on the page that opens, approve | OAuth 2.1 is live; no key is issued for these clients | `/.well-known/oauth-authorization-server`; consent page served by the backend at `/oauth/authorize` |
| Connect page (optional): render your own consent screen instead of the backend's plain page | the backend page is functional but unstyled | `GET /v1/oauth/requests/{id}`, `POST /v1/oauth/requests/{id}/decision` (operator Bearer today); set `FRONTEND_CONSENT_URL` on the server to redirect there |
| Connect list: show OAuth connections (`client: chatgpt` / `mcp_other`) with revoke | revoking kills the token family | `GET /v1/connections`, `DELETE /v1/connections/{id}` |

## 2. Accounts, team, sign-in (spec 43 — next push)

- **Sign-in / sign-out / session:** `POST /v1/auth/login` sets an HttpOnly session cookie and returns a CSRF token;
  send it as `X-CSRF-Token` on every state-changing cookie request. `POST /v1/auth/logout`, `GET /v1/auth/session`
  (user, role, tenant, customer numbers). The role decides what the console shows: `platform_admin` (Valio staff,
  all tenants), `tenant_admin`, `approver`, `viewer`, and later `member` (runs agents).
- **Sign-up with business profile (spec 45):** `GET /v1/auth/signup/options` (business types, size bands, labels),
  `POST /v1/auth/signup`; tenant profile `GET/PUT /v1/tenant/profile`. The response names the preset that produced
  each guardrail value — show it next to the value.
- **Invites and password resets:** `POST /v1/auth/invites/{token}/accept`, `POST /v1/auth/password-resets/{token}/accept`
  (set password once; a second use answers `410`).
- **Team management (tenant_admin):** users list, invite, role change, disable, reset link (`/v1/tenant/users` per spec
  43 R-19), roles with their permissions as data `GET /v1/tenant/roles`. Handle `409 LAST_TENANT_ADMIN`.
- **Tenant and staff management (platform_admin):** `GET/POST /v1/operator/tenants`, `PATCH /v1/operator/tenants/{id}`,
  `POST /v1/operator/tenants/{id}/users`.
- **Connections per tenant:** `GET/POST /v1/tenant/connections`, `.../{id}/rotate`, `DELETE .../{id}`,
  `.../{id}/instructions?client=…`; operator side `/v1/operator/connections` with the same shape.
  Connection catalog for the tabs: `GET /v1/connect/catalog` (types, clients, credential methods, audience).
- **Three audiences on every connection type:** the same MCP / API / A2A / webhook / tool-pack tabs appear on the
  buyer side and the Valio side; the catalog and instructions take the audience (`buyer`, `tenant`, `operator`).
  Tool packs for OpenAI, Anthropic and Gemini: `GET /v1/connect/tool-pack?format=openai|anthropic|gemini` — this is
  where a "Gemini" option can live (the business teammate asked for provider choice).

## 3. Agent governance (spec 44 — next push)

- **Agent register:** `GET /v1/tenant/agents` — owner, status, connections, autonomy, budget use, orders, approvals,
  refusals, last activity; activity feed `GET /v1/tenant/agents/{id}/activity`.
- **Effective policy with sources:** `GET /v1/tenant/agents/{id}/policy/effective` — every field has value, source
  layer (platform / tenant / agent), version and author. Show the source; it is the governance story.
- **Edit policy:** `PUT/PATCH /v1/tenant/agents/{id}/policy`. Tightening applies at once; loosening returns a change
  request. `422 OUTSIDE_GUARDRAIL` names the limit and its source.
- **Change requests:** `GET /v1/tenant/policy/change-requests`, `POST .../{id}/approve|reject`; the requester cannot
  approve its own. Each request carries a simulation result: `POST /v1/tenant/agents/{id}/policy/simulate`.
- **Versions and rollback:** `.../policy/versions`, `.../versions/{v}/rollback`; tenant guardrails
  `GET/PUT /v1/tenant/guardrails` with versions; platform policy `/v1/operator/governance/policy`.
- **Pause:** `POST /v1/tenant/agents/{id}/pause|resume`, tenant-wide `POST /v1/tenant/agents/pause|resume`, Valio
  `POST /v1/operator/tenants/{id}/pause|resume`. A paused agent's calls answer `423 AGENT_PAUSED`.
- **Transfer:** `POST /v1/tenant/agents/{id}/transfer` (when a person leaves).
- **Alerts:** `GET /v1/tenant/alerts` — budget 80 % / 100 %, refusal bursts, off-hours activity, new client.
- **Governance report:** `GET /v1/tenant/governance/report?from&to` — JSON and a signed export to download.

## 4. Approvals (specs 17, 45 — next push)

- Approval inbox shows who may approve each order. Handle `403 APPROVAL_LIMIT` (amount above the approver threshold;
  a tenant admin approves) and `403 FOUR_EYES` (the person who connected or promoted the agent cannot approve).
  Single-restaurant profile: self-approval is allowed and the order carries `self_approved: true` — mark it.
- Keep handling `409 EVIDENCE_CHANGED` (price or stock moved since the quote; show the new evidence).

## 5. Anomalies and exceptions (specs 30, 31, 45 — next push)

- **Anomaly warnings** on offers and orders: `QUANTITY_ANOMALY`, `PRICE_ANOMALY`, `VELOCITY_ANOMALY`, `NEW_CATEGORY`,
  each with expected band, observed value, history window, `band_source` (`statistical` / `forecast`) and proposed
  fixes (corrected quantity, cheaper ranked alternative, split). Applying a fix re-quotes. `insufficient_history`
  means no warning.
- **Exception journey:** orders carry the next step (`next_action`) and options for partial stock, date shift, out of
  stock (alternatives instead of a bare rejection); receipt `GET /v1/orders/{id}/receipt`.
- **Hospital substitution:** substitutes keep allergen and diet constraints and always go to approval; show the
  constraint evidence. Hospital profile requires constraints before the first order (`409 CONSTRAINTS_REQUIRED`).

## 6. Proactive and repeat buying (specs 32, 35, 36, 41 — next push)

- **Suggestions (purchase memory):** `GET /v1/suggestions`, `POST .../{id}/dismiss|prepare|make-recurring`,
  `PUT /v1/buyer/memory-preferences`. A suggestion never buys by itself.
- **Recurring orders:** list, create, run now, pause; runs show drift (price, pack content, stock) that needs approval:
  `/v1/recurring-orders/...`.
- **Supplier proposals inbox:** `GET /v1/proposals/inbox`, `POST /v1/proposals/{id}/accept|decline|mute`; respect the
  consent channel (inbox only vs webhook).
- **Recipe to basket:** `POST /v1/basket-lookups` → one basket offer with shared items combined.

## 7. Trust and demo helpers

- **Evidence bundle** `GET /v1/orders/{id}/evidence`, **UBL** `GET /v1/orders/{id}/ubl` (download), **audit verify**
  `GET /v1/audit/verify`, **anchors** `GET /v1/audit/anchors` — show the polygonscan link once anchoring is switched on
  (TASKS #44).
- **Sandbox scenarios** for the demo: `GET /v1/sandbox/scenarios`, `POST /v1/sandbox/reset {scenario}` (next push).
- **Suppliers:** `GET /v1/suppliers` (Valio + synthetic second supplier) (next push).

## 9. Backend capabilities not listed above yet (audit 14.11 22:10)

Found by comparing this file with the routes the backend serves. Data and behaviour only.


- **Complete a purchase:** create/read a fixed-price quote, then place the order: `POST /v1/quotes`, `GET /v1/quotes/{id}`, `POST /v1/orders`. Bind quotes using `need_ref`, `plan_ref`, or `basket_ref`; preserve the placement `Idempotency-Key`. Handle `OPEN`, `ACCEPTED`, `EXPIRED`, `EXPIRED_QUOTE`, `QUOTE_ALREADY_ACCEPTED`, `NEED_REF_REQUIRED`, and `IDEMPOTENCY_CONFLICT`. **User:** kitchen buyer through a purchasing agent; current console uses the operator’s agent. **Specs:** 02, 10, 16, 22.

- **Compare fulfillment plans and soft preferences:** `POST /v1/offers` supports brands, price caps, pack ranges and labels, and returns `single`, `split`, `partial` plans with `policy_preview`; quote a selected plan through `POST /v1/quotes`. Handle `OUTSIDE_PREFERENCE`, uncovered quantity, and preview outcomes `CONFIRMED` / `PENDING_APPROVAL` / `REJECTED`. **User:** kitchen buyer. **Spec:** 08.

- **Understand search coverage and ranking:** `POST /v1/offers`, `GET /v1/lookups/{id}` expose retrieval terms, matched/screened/priced counts, constraint exclusions, whole-pack purchase totals, overbuy, conversion evidence and score contributions. Handle `RETRIEVAL_BROADENED`, `POOL_TRUNCATED`, `UNKNOWN_CODE`, `SIZE_MISMATCH`, `SIZE_UNCONFIRMED`, and honest empty results. **User:** kitchen buyer. **Specs:** 01, 05, 09, 15, 18, 20.

- **Inspect product facts:** `GET /v1/products/{id}?supplier_id=…` returns GTINs, units, pack quantities, allergens, claims, labels, nutrition and provenance-related facts. Missing declarations differ from proven absence; handle `PRODUCT_NOT_FOUND`. **User:** kitchen buyer, approver. **Specs:** 01, 13, 15, 46.

- **Review purchase history and its full explanation:** `GET /v1/orders`, `GET /v1/orders/{id}`, `GET /v1/quotes/{id}`, `GET /v1/lookups/{id}`, `GET /v1/orders/{id}/timeline`, `GET /v1/audit?order_id=…&quote_id=…`. Preserve rejection/cancellation reasons, evidence revisions, failed attempts and call totals; foreign resources can return 404. **User:** kitchen buyer, approver, Valio operator within their scope. **Specs:** 02, 03, 07, 19, 22.

- **Cancel a confirmed purchase:** `POST /v1/orders/{id}/cancel`. Only `CONFIRMED → CANCELLED`, before every known cutoff; reservations are released. Handle `INVALID_TRANSITION`, `CUTOFF_PASSED`, `ORDER_NOT_FOUND`, and the confirmation handshake below. **User:** authorized buyer/approver, Valio operator. **Spec:** 04.

- **Edit/reset the procurement runtime policy:** `GET/PUT /v1/policy`, `POST /v1/policy/reset` cover warning handling, ranking weights, shortlist settings, mandates and rate limits—additional controls beyond `/v1/operator/governance/policy`. Handle validation failures and returned update metadata/diffs. **User:** Valio operator. **Specs:** 03, 06, 09, 15, 24.

- **Maintain standing procurement constraints:** `GET/PUT /v1/policy` → `customers`, and `GET/PUT /v1/tenant/policy` for the tenant overlay. Manage claims, allergen exclusions and substitution/clinical rules; requests cannot relax standing constraints. Handle `UNKNOWN_CODE`, hard violations, and constraint source `request` versus `customer_policy`. **User:** tenant admin, Valio operator. **Specs:** 16, 31, 43.

- **Complete human decisions with two requests:** approval/rejection/cancellation, policy/reset, autonomy and governance writes first return `confirmation_required`, a summary and `confirmation_token`; repeat the identical request with that token within five minutes. Handle `CONFIRMATION_INVALID` and `EVIDENCE_CHANGED`. **User:** approver, tenant admin, Valio operator. **Specs:** 43–44. This is enforced even for the legacy operator token. [Implementation](backend/app/tenant_api.py:42)

- **Review readiness and grant/withdraw autonomy:** `GET /v1/agents/{id}/track-record?window_days=…`, `POST /v1/agents/{id}/autonomy`, including tenant/operator aliases. Levels are `approval_required` and `autonomous_within_limits`; recommendations never promote automatically. Tenant changes use governance change control. Handle `AGENT_NOT_FOUND`, `BUYER_ADMIN_REQUIRED`, and confirmation responses. **User:** tenant admin, Valio operator. **Spec:** 40.

- **Create, rename and disable agents:** `POST /v1/tenant/agents`, `PATCH /v1/tenant/agents/{id}`. New agents start approval-first; disabling revokes existing connections. Handle `AGENT_EXISTS`, `AGENT_NOT_FOUND`, and active/disabled state. The to-do lists the register, but not these lifecycle actions. **User:** tenant admin/authorized agent owner. **Specs:** 43–44.

- **Issue and revoke signed purchasing authority:** `POST/GET /v1/tenant/agents/{id}/mandates`, `DELETE /v1/tenant/agents/{id}/mandates/{mandate_id}`; signed external revocations use `POST /v1/mandates/revocations`. Handle expiry/revocation, `MANDATE_NOT_FOUND`, `MANDATE_INVALID`, `REVOCATION_INVALID`, and customer scope. “Mandates” appears only in the push headline, without this workflow. **User:** tenant admin. **Specs:** 12, 43.

- **Inspect organization spending and activity:** `GET /v1/buyer/{agents,mandates,spend,orders,approvals,activity}`, plus `GET /v1/tenant/spend` and `/v1/tenant/audit`. Spending separates autonomous spend, pending commitments, human-approved spend, remaining authority and limit source. **User:** kitchen buyer, approver, tenant admin; Valio operator can read buyer views. **Specs:** 28, 43.

- **Apply a buyer-local emergency stop/revocation:** `POST /v1/buyer/agents/{id}/suspend|resume`, `POST /v1/buyer/mandates/{jti}/revoke`. This suspension is separate from governance pause; subsequent commands return `AGENT_SUSPENDED` or merged `AGENT_PAUSED`. Handle `BUYER_ADMIN_REQUIRED`, `AGENT_NOT_FOUND`, and revoked mandates. **User:** tenant admin; operator credentials cannot perform these buyer-admin controls. **Spec:** 28.

- **Finish team lifecycle management:** `DELETE /v1/tenant/users/{id}`, `POST …/{id}/resend-invite|cancel-invite`; equivalent staff management exists under `/v1/operator/users`. Handle `invited`, `active`, `disabled`, `removed`, `ALREADY_ACTIVE`, `EMAIL_EXISTS`, `LINK_EXPIRED`, and `LAST_TENANT_ADMIN`. Removing/disabling owners also affects their agents/connections. **User:** tenant admin, Valio operator. **Specs:** 43–44.

- **Request functionally suitable replacements and clinical review:** `POST /v1/offers` accepts `function` and `substitute_for`; quotes/orders expose `clinical_review`. Handle `CLINICAL_REVIEW`, `SUBSTITUTION_PROFILE`, and `BUYER_APPROVER_REQUIRED`: a platform operator alone cannot approve a clinical order. **User:** kitchen buyer, tenant approver/admin. **Spec:** 31.

- **Purchase from a selected supplier and understand execution:** send `supplier_id` through `POST /v1/offers`; quotes/orders retain it. Handle synthetic Nordfood labeling, supplier minimum quantities, `UNKNOWN_SUPPLIER`, `SUPPLIER_NOT_IN_MANDATE`, `UNSUPPORTED_CURRENCY`, and `PENDING_EXECUTION`. Ambiguous execution retries must retain the same quote/key; distinguish `gateway-simulated` from `supplier-acknowledged`. **User:** kitchen buyer, approver. **Spec:** 25.

- **Manage notification delivery:** `POST/GET /v1/webhook-subscriptions`, `PATCH/DELETE …/{id}`, `GET …/{id}/deliveries`, `POST …/{id}/deliveries/{delivery_id}/redeliver`; tenant/operator aliases exist. Handle destination challenge failures, unsafe URLs, enabled/disabled subscriptions and pending/delivered/failed/skipped delivery states. Delivery failure does not change an order. **User:** tenant admin, authorized approver, Valio operator. **Specs:** 33, 43.

- **Control proposal consent:** `GET/PUT /v1/buyer/proposal-preferences?customer_id=…` manages opted-in categories/interests, channels, cadence, quiet periods and muted campaigns. Empty interests/categories mean no opt-in. Handle `CUSTOMER_REQUIRED`, `CUSTOMER_FORBIDDEN`, `BUYER_ADMIN_REQUIRED`. **User:** tenant admin function, but currently these routes require legacy `buyer_admin` credentials. **Spec:** 36.

- **Submit and monitor supplier campaigns:** `POST /v1/proposals`, `GET /v1/proposals/campaigns/{id}` expose submission and aggregate delivery/action counts, beyond the listed generation endpoint. Handle `PROPOSAL_EXPIRED`, `INVALID_PRICE_EVIDENCE`, campaign deduplication and filtering/quota holds. **User:** Valio operator through a `supplier_agent` credential; an ordinary operator token receives `SUPPLIER_REQUIRED`. **Specs:** 36, 46.

- **Snooze replenishment:** `POST /v1/suggestions/{id}/snooze {days}` hides a suggestion for 1–365 days. Handle persisted `snoozed_until`, `SUGGESTION_NOT_FOUND`, and reviewer permissions. **User:** kitchen buyer/approver. **Spec:** 41. Already called by the console.

- **Read preferences and permanently forget purchase memory:** `GET /v1/buyer/memory-preferences`, `DELETE /v1/buyer/memory?customer_id=…`. Forgetting permanently suppresses suggestions; enabling memory later does not reverse it, and audit/order evidence remains. **User:** tenant admin function; currently legacy `buyer_admin` credentials only. **Spec:** 41.

- **Explicitly approve a new recurring baseline:** `POST /v1/recurring-orders/{id}/baseline {approved_order_id}` increments the baseline revision. Handle `APPROVED_TEMPLATE_REQUIRED`, `RECURRING_FORBIDDEN`, `BUYER_APPROVER_REQUIRED`, and `RECURRING_NOT_FOUND`. **User:** approver/tenant admin via supported buyer credentials. **Spec:** 35. Already called by the console.

- **Keep and verify an independent audit checkpoint:** `GET /v1/audit/head`, `GET /v1/audit/verify?checkpoint_entries=…&checkpoint_hash=…`, optionally `&anchor=latest|<tx_hash>`; historical keys are at `/.well-known/jwks-history.json`. Handle `INVALID_CHECKPOINT`, chain/checkpoint mismatch and anchor verification state. **User:** approver, tenant admin, Valio operator. **Specs:** 10–11, 21, 29.

- **Export the decision response document:** `GET /v1/orders/{id}/ubl?document=response`, alongside the already-listed default Order export. Preserve confirmed/rejected response semantics and simulation labeling. **User:** kitchen buyer, approver. **Spec:** 13.

- **Inspect operational context and recover correctly:** `GET /v1/me`, `/v1/meta`, `/v1/stats?since=…`, `/health` expose scope, limits, remaining tokens, standards, decision metrics, catalog coverage and upstream blocking. Handle `RATE_LIMITED` (429), `Retry-After`, correlation IDs and the full recovery actions, including `REFRESH_QUOTE`, `FIX_REQUEST`, `CONTACT_ADMIN`, and `STOP`. **User:** Valio operator; scoped context is available to other authenticated users. **Specs:** 03, 22–24, 38–39.

- **Pre-register an OAuth client:** `POST /v1/oauth/clients`; the client secret is returned once. Handle OAuth `invalid_client_metadata` errors rather than assuming the business RFC 9457 envelope. **User:** Valio operator. **Spec:** 43. [Route](backend/app/oauth.py:649)

## 10. Already done in the console (verified in frontend/src)


Verified in [API calls](frontend/src/api.ts:492), [basket calls](frontend/src/basket.ts:51), [repeat calls](frontend/src/repeat.ts:73) and their page callers:

- **Unorderable offers:** Buyer uses `GET /v1/policy` → `reject.on` to block rejected offers; the default list includes `NOT_AVAILABLE`.
- **Quantity units:** Buyer offers L, KG and `SALES_UNIT` (“packs”), and clears the example quantity when changing intent. This is unit selection, not automatic per-product unit validation.
- **OAuth connection flow:** Connect provides OAuth instructions; Consent calls `GET /v1/oauth/requests/{id}` and `POST …/decision`; connection list/revoke calls are wired. This remains the legacy operator flow, not account-based tenant consent.
- **Anomaly warnings and corrections:** Buyer, approvals and order detail render anomaly data; corrections call `POST /v1/quotes/{quote_id}/anomaly-fixes/{fix_id}`.
- **Recipe basket:** `POST/GET /v1/basket-lookups`, followed by basket quotation and placement, are wired.
- **Suggestions:** list, prepare, dismiss and make-recurring are wired; snooze is also implemented.
- **Recurring reads/run-now:** list, runs and run-now have actual page callers. **Partial:** the operator-only console cannot access the intended owned workflows reliably; do not mark recurring buying fully complete.
- **Suppliers:** Trust calls `GET /v1/suppliers`.
- **Trust exports/checks:** UBL download, audit verification and anchors are wired. **Evidence ZIP and dedicated receipt retrieval are not.**
- **Approval links and changed evidence:** `/approvals/{id}` routes to the approval inbox; approval requests include `evidence_revision`, and `EVIDENCE_CHANGED` reloads evidence. **Partial:** approve/reject omit the required confirmation-token round trip, so current completion is broken. [Caller](frontend/src/pages/Approvals.tsx:17)

## 11. Corrections to the sections above


- **Recurring pause:** §6 lists pause under `/v1/recurring-orders/...`, but the REST router has **no pause endpoint**. The implemented operation is MCP `pause_recurring_order`. [REST routes](backend/app/recurring.py:475)
- **Signed governance report:** the path matches, but a signed result requires `GET /v1/tenant/governance/report?from=…&to=…&signed=true`; `signed` defaults to `false`. [Route](backend/app/governance_api.py:243)
- **Otherwise:** no renamed concrete REST paths found. Placeholder differences such as `{id}` versus `{agent_id}` are not mismatches.

## Error codes worth a friendly message

`UNAUTHENTICATED` (401), `OPERATOR_REQUIRED`, `CUSTOMER_NOT_IN_MANDATE`, `AGENT_NOT_REGISTERED`, `APPROVAL_LIMIT`,
`FOUR_EYES` (403), `LAST_TENANT_ADMIN`, `EVIDENCE_CHANGED`, `CONSTRAINTS_REQUIRED` (409), `OUTSIDE_GUARDRAIL` (422),
`AGENT_PAUSED` (423), `UPSTREAM_BUSY` / `UPSTREAM_RATE_LIMITED` (503, retry). Every error is RFC 9457 problem+json
with `next_action`.

## 8. Deploy and server (DevOps)

Server file: `~/app/deploy/.env` (never in git, never in chat). After changing it: `cd ~/app && docker compose
--env-file deploy/.env -f deploy/docker-compose.yml up -d backend`.

### Variables

| Variable | Needed | What to set |
|---|---|---|
| `VALIO_API_KEY`, `OPERATOR_TOKEN`, `AGENT_KEYS` | yes, already set | keep |
| `OPENAI_API_KEY` | yes | the fit score; without it every order is held with `NO_FIT_SCORE` (live orders now show a score, so it looks set — confirm) |
| `GATEWAY_PUBLIC_URL` | automatic | the deploy workflow passes `SITE_URL`; nothing to add. MCP and OAuth only accept this host, so keep `SITE_URL` the canonical address |
| `DEMO_PLATFORM_ADMIN_PASSWORD`, `DEMO_HOTEL_ADMIN_PASSWORD`, `DEMO_HOTEL_APPROVER_PASSWORD`, `DEMO_HOTEL_VIEWER_PASSWORD`, `DEMO_HOSPITAL_ADMIN_PASSWORD`, `DEMO_HOSPITAL_APPROVER_PASSWORD` | yes, with spec 43 (next push) | strong random values; `reset_demo.py` creates the demo users with them. Generate locally with `backend/scripts/gen_keys.py --write` on a copy of the env file, or `openssl rand -base64 24` each |
| `FRONTEND_CONSENT_URL` | optional | the console route of your own OAuth consent screen (https); empty = backend page |
| `ANCHOR_RPC_URL`, `ANCHOR_CHAIN_ID=137`, `ANCHOR_PRIVATE_KEY`, `ANCHOR_EXPLORER_TX_URL=https://polygonscan.com/tx/`, `ANCHOR_EVERY_N_ENTRIES` | optional (TASKS #44) | wallet key from Gökalp, privately |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_CONSOLE` | optional | an OTLP collector URL if you want traces; empty = off |
| `FORECAST_ENABLED`, `FORECAST_MODEL_PATH` | optional, keep off | Timestral forecast band; needs torch (`backend/requirements-forecast.txt`, several hundred MB) and the `mistralai/Timestral-28M` weights on disk. The statistical anomaly band works without it |
| `SANDBOX_DB_PATH`, `SANDBOX_ADMIN_TOKEN`, `SANDBOX_AGENT_KEYS` | optional | only for a separate sandbox gateway instance (`GATEWAY_ENVIRONMENT=sandbox`); the in-process sandbox scenarios work without it |
| `NORDFOOD_API_BASE` | no | the synthetic second supplier runs in-process by default |

`AGENT_SECRET`, `SUPPLIER_AGENT_KEY` and `VTG_CONNECTION_KEY` in `.env.example` belong to the reference clients and
examples, not to the server.

### After the next push

1. Let the deploy run (it builds, restarts, checks `/health` and the developer pages).
2. The database schema migrates on start (tenants, users, connections, governance tables). Take a copy of
   `/data/app.db` from the volume before the deploy if you want a rollback point.
3. Seed the demo once: `docker compose --env-file deploy/.env -f deploy/docker-compose.yml exec backend python
   backend/scripts/reset_demo.py` (demo tenants Hotel Aurora chain and Kaari Hospital Kitchen, users, synthetic
   history). It resets demo state, so run it before a rehearsal, not during one.
4. Smoke: `curl -s https://luotsi.online/health`, `curl -si https://luotsi.online/v1/connections` (401),
   `curl -s https://luotsi.online/.well-known/oauth-authorization-server` (200), sign in to the console with a demo
   user.

### Routing to check

- Caddy forwards `/v1 /health /.well-known/* /oauth/* /mcp* /a2a* /developers* /llms.txt /docs /openapi.json` to the
  backend (`/oauth/*` was added today). `/mcp/tenant`, `/mcp/operator` and `/a2a/tenant` are covered by the
  existing patterns.
- Webhook approval links point to `/approvals/{order_id}?revision=…`. That path is a console route today; either the
  console opens the approval for that id, or Caddy sends `/approvals/*` with a `revision` query to the backend page.
  Pick one.
