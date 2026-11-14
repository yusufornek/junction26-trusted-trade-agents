# Frontend to-do from the backend (14.11 evening)

What the console can add or change now that the backend serves it. Data and behaviour only; screens, layout and
wording are the frontend owner's call. Each item names the endpoints and the codes to handle. Field-level contracts:
`contracts/openapi.yaml` and the spec in `docs/specs/`. Status: **live** = on https://luotsi.online now;
**next push** = built and tested, lands on main tonight (endpoint names may still move slightly — the OpenAPI file in
that push is the truth).

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

## Error codes worth a friendly message

`UNAUTHENTICATED` (401), `OPERATOR_REQUIRED`, `CUSTOMER_NOT_IN_MANDATE`, `AGENT_NOT_REGISTERED`, `APPROVAL_LIMIT`,
`FOUR_EYES` (403), `LAST_TENANT_ADMIN`, `EVIDENCE_CHANGED`, `CONSTRAINTS_REQUIRED` (409), `OUTSIDE_GUARDRAIL` (422),
`AGENT_PAUSED` (423), `UPSTREAM_BUSY` / `UPSTREAM_RATE_LIMITED` (503, retry). Every error is RFC 9457 problem+json
with `next_action`.
