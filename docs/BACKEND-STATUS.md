# Backend status for frontend planning (14.11, afternoon)

What the backend serves on `main` now, what lands next, and the data each part gives the console. Field-level
contracts: `contracts/openapi.yaml`, `docs/FRONTEND.md`, and the spec named in each row (`docs/specs/`). Screens are
the frontend owner's call; this page lists data and behaviour only.

## On main now (live after this push)

| Area | Endpoints / surfaces | Spec |
|---|---|---|
| Buying flow | `POST /v1/offers`, `GET /v1/lookups/{id}`, `POST /v1/quotes`, `GET /v1/quotes/{id}`, `POST /v1/orders`, `GET /v1/orders[/{id}]`, `POST /v1/orders/{id}/cancel`, `GET /v1/orders/{id}/timeline` | 01–05, 18, 20 |
| Approvals (operator) | `GET /v1/approvals`, `POST /v1/approvals/{id}/approve | reject` (evidence revision, `409 EVIDENCE_CHANGED`) | 17 |
| Buyer view | `GET /v1/buyer/agents | mandates | spend | orders | approvals | activity`, suspend / resume an agent, revoke a mandate, approve / reject from the buyer side | 28 |
| Evidence bundle | `GET /v1/orders/{id}/evidence` (quote, rule hits, approvals, receipts in one document) | 29 |
| Trust and audit | `GET /.well-known/jwks.json`, `/.well-known/jwks-history.json`, `GET /v1/orders/{id}/ubl`, `GET /v1/audit`, `/v1/audit/head`, `/v1/audit/verify`, `/v1/audit/anchors` | 10, 11, 13, 21 |
| Agents and autonomy | `GET /v1/agents/{id}/track-record`, `POST /v1/agents/{id}/autonomy`, `POST /v1/mandates/revocations` | 12, 40 |
| Policy and stats | `GET /v1/policy`, `POST /v1/policy/reset`, `GET /v1/stats` | 15–17 |
| Webhooks | `/v1/webhook-subscriptions` (create, list, read, deliveries, redeliver), Standard Webhooks signatures | 33 |
| A2A | `/.well-known/agent-card.json`, `POST /a2a` | 34 |
| MCP | streamable HTTP at `/mcp` inside the gateway; Bearer agent key; JSON-RPC `ping` answers; server card at `/mcp/server-card`, AI catalog at `/.well-known/ai-catalog.json` | 37 |
| Developer surface | `/developers`, `/developers/tools.json`, `/llms.txt`, `GET /v1/me`, `GET /v1/meta` | 38 |

Pack sizes: case counts come from the comparison-price ratio or agreeing catalog data; disagreeing data adds a
`SIZE_*` warning and needs approval (spec 05, 15).

## Lands next (built or building, in this order of arrival)

| What | Endpoints / data for the console | Spec | State |
|---|---|---|---|
| Connect page keys | `POST /v1/connections` (key once), `GET /v1/connections`, `DELETE /v1/connections/{id}`; `NewConnection.mcp_url` filled | 42, `docs/CONNECT-API.md` | building; next push. Until then `/v1/connections` answers `404` and the page stays in "no keys yet" mode |
| Fixes for buyer view, evidence, A2A, webhooks | same endpoints; activity and evidence show only the caller's own agents' events; webhook queue no longer blocked by disabled subscriptions | 28, 29, 33, 34 | building; next push |
| Supplier adapters, sandbox, tracing | `GET /v1/suppliers`; `GET /v1/sandbox/scenarios`, `POST /v1/sandbox/reset {scenario}`; OpenTelemetry trace ids in responses | 25, 26, 27 | built, merging |
| Exception journey | `GET /v1/orders/{id}/receipt`, order status with exception steps (partial stock, date shift, substitute) | 30 | built, merging |
| Hospital substitution, recipe to basket | allergen-safe substitute proposals; `POST /v1/basket-lookups` (recipe or list → basket offer) | 31, 32 | built, merging |
| Recurring orders, supplier proposals | `/v1/recurring-orders/{id}/runs | baseline | run-now` (drift between runs); `GET /v1/proposals/inbox`, accept / decline / mute | 35, 36 | built, merging |
| Purchase memory | `GET /v1/suggestions`, dismiss / prepare / make-recurring, `PUT /v1/buyer/memory-preferences` | 41 | building |
| Accounts, tenants, team | sign-in / sign-out / session with role (cookie + `X-CSRF-Token`); invites, role change (`tenant_admin`, `approver`, `viewer`), disable, reset link, `GET /v1/tenant/roles`; tenant and staff management for Valio; connections per tenant; OAuth for remote MCP / A2A; buyer / tenant / operator audiences on MCP, REST, A2A, webhooks and tool packs | 43 | queued after 42 |
| Agent governance | agent register with owners, effective policy with source per field, change requests for loosening, versions, simulation, pause (`423 AGENT_PAUSED`), alerts, signed governance report | 44 | queued after 43 |
| Guardrail profiles and anomalies | sign-up with business type and size → preset guardrails; approver amount threshold (`403 APPROVAL_LIMIT`); four-eyes (`403 FOUR_EYES`); anomaly warnings `QUANTITY_ANOMALY`, `PRICE_ANOMALY`, `VELOCITY_ANOMALY`, `NEW_CATEGORY` with expected band and one-step fixes | 45 | queued after 44 |

Each queued spec adds its own section to `docs/FRONTEND.md` when it is built.

## Known on the live server
- `OPENAI_API_KEY` is still empty on the live server (TASKS #36), so every order is held for approval with
  `NO_FIT_SCORE`. Gökalp adds it to `~/app/deploy/.env`.
- The fixes for buyer view, evidence, A2A and webhooks (row 2 above) arrive with the next push; until then activity
  and evidence can include events of other agents buying for the same customer.
