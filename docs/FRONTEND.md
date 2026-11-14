# Backend notes for the frontend

Written from the backend side for the frontend/UX owner: what the API offers and what we noticed while testing
it end to end. Screens, flow and look are the frontend owner's call — use whatever here is useful.

Contract: `contracts/openapi.yaml` (live docs at `http://localhost:8000/docs` when the backend runs).
Background: `docs/CHALLENGE.md`, `docs/DECISIONS.md`.

## What the backend exposes for people (the agent itself uses MCP)
| Need | Endpoint |
|---|---|
| Orders waiting for a human | `GET /v1/approvals` → `{order, quote}` with `order.status = PENDING_APPROVAL` |
| Approve / reject | `POST /v1/approvals/{order_id}/approve` or `/reject` with `{approver, comment}` |
| The agent's search behind a quote, incl. alternatives | `GET /v1/lookups/{quote.need_ref}` → `need`, `offers[]` |
| Orders | `GET /v1/orders`, `GET /v1/orders/{id}` |
| Step-by-step history | `GET /v1/audit?order_id=…` / `?quote_id=…` → `{ts, actor, action, summary, data}` |
| Rules the agent runs under | `GET /v1/policy`, `PUT /v1/policy` `{policy, updated_by, comment}`, `POST /v1/policy/reset` |
| Overview numbers | `GET /v1/stats` (optional `?since=`) |

## Observed while testing the backend
- **Why an order needs a human** is in `order.policy.hits[]` as `{rule, detail}`, e.g. `max_auto_total_eur` →
  "Net total 1708.00 exceeds 1500.00 EUR", `CUTOFF_PASSED` → order cut-off already passed. This was the most
  useful information when judging an order by hand.
- **Pending orders expire**: `order.expires_at` is at most 30 minutes after the quote (fixed-price lock). In our
  runs the remaining time mattered for whoever approves.
- **Every offer explains itself**: `matched_because[]` (e.g. "FREE_FROM_LACTOSE from catalog nutritionalClaim"),
  `fit` (0–1 from OpenAI Decisions, the question in `fit_question`), `warnings[]` with `code` + plain `text`,
  and `provenance` (which Valio endpoint, fetched at, valid until).
- **Alternatives exist for every order**: the lookup behind a quote keeps up to 10 ranked offers with
  `rank_label` best / better / good — handy when someone wants to see what else the agent considered.
- **Actors in the audit log** are `agent:<id>` (e.g. `agent:hotel-chain-agent`), `human:<name>` and `system`;
  `data.live_calls` shows how few Valio calls a request needed (typically 1 price + 1 availability).
- **Policy changes take effect immediately** and are audited as `POLICY_UPDATED` with a before/after diff.
  Lowering `max_auto_total_eur` from 1,500 to 1,000 turned the same 1,010 € order from CONFIRMED into
  PENDING_APPROVAL — a clear way to show that people set the agent's limits.
- **Common warnings in real data**: `CUTOFF_PASSED` appears on most Terminal products (meaning still to be
  confirmed with a mentor), `PARTIAL_STOCK` on some Stock products, `SIZE_UNKNOWN` on items without litre data.
- **Status values**: `CONFIRMED`, `PENDING_APPROVAL`, `REJECTED`, `EXPIRED`, `CANCELLED`.
- **Timing**: a lookup with live calls + Decisions takes a few seconds; approvals and audit reads are instant.
  There are no push events yet — polling every few seconds is what we used while testing.

## New on the backend since 14.11 (specs in `docs/specs/`, all in `contracts/openapi.yaml`)
| What | Where | Observed while testing |
|---|---|---|
| Cancel a confirmed order | `POST /v1/orders/{order_id}/cancel` `{cancelled_by, reason}` → `Order` with `cancelled_by`, `cancel_reason` | Stock-model orders (cut-off 15.11 17:00) cancel; Terminal-model orders answer `409 CUTOFF_PASSED`; not-confirmed → `409 INVALID_TRANSITION` (spec 04) |
| Whole story of an order | `GET /v1/orders/{order_id}/timeline` → `steps[]` with `step` = lookup / quote / order / decision / cancellation / system, plus call totals | One call replaces stitching `/v1/audit` + `/v1/lookups`; includes the search and the quote that led to the order (spec 07) |
| Fulfilment plans | `LookupResult.plans[]`: `kind` single / split / partial, `lines[]`, `total`, `coverage`, `uncovered`, `warnings[]`, `policy_preview {outcome, hits[]}` | The preview tells before ordering whether the agent may confirm alone or a human will be asked, and why; a 400 L request with limited stock produced a split plan (33 cases + 4 cartons) (spec 08) |
| Quote from a plan | `POST /v1/quotes` with `plan_ref {lookup_id, plan_index}` instead of `lines` | Order outcome matched the plan's preview in our runs |
| Buyer preferences | `NeedSpec.preferences {brands[], max_price_per_unit, pack_size {min, max}, labels[]}` | Offers outside a preference carry `OUTSIDE_PREFERENCE` with the differences in the text ("brand Valio, preferred Arla; 5 L pack, preferred <= 1 L") and rank below the others; they need sign-off by default |
| Nothing fits | `LookupResult.no_match_reason` | Names which hard constraint removed how many candidates |
| Exact pack sizes | `OfferCard.size_source` is now mostly `catalog` / `name_parse`, new `size_confirmed_by_price`; new warning `SIZE_MISMATCH` | "1,000 L ÷ 5 L/KI = 200 KI" instead of 5.01 and 201 (spec 05) |
| How many were considered | `LookupResult.screened_count`, `priced_count` | Typical milk lookup: 40 screened, 13–17 priced, 10 returned, still 1 + 1 Valio calls (spec 09) |
| Agent mandates | `GET /v1/policy` → `agents {<id>: {customer_ids, max_auto_total_eur?, max_auto_daily_total_eur, note}}`, `shortlist.live_max`; `PUT` without them keeps the current values | New rule hit: `max_auto_daily_total_eur`; an agent without a mandate can look and quote, its order answers `403 AGENT_NOT_REGISTERED` and its plan previews show `REJECTED` with `unregistered_agent` (changed 14.11); `403 CUSTOMER_NOT_IN_MANDATE` for a foreign customer (spec 06) |
| Who is registered | `GET /v1/stats` → `agents_seen[].registered` | The console's own flow is `agent:web-console` (registered); lookups now carry that id too |

## ChatGPT and claude.ai OAuth (spec 43 R-9/R-10, operator stage)

On Connect, show the public `/mcp` URL to paste and "Sign in with OAuth"; this path has no key to copy.
The operator signs in and consents in the gateway window, choosing an agent, allowed customers and scopes.
OAuth connections carry `credential_kind: "oauth"` and `key_prefix: "oauth"` in the existing connection
list; DELETE revokes the grant immediately. API-key connection shapes are unchanged.

The backend serves the consent page until the console supplies one. `FRONTEND_CONSENT_URL` selects an HTTPS
console page receiving `request_id` only. Operator-authenticated `GET /v1/oauth/requests/{request_id}` supplies
the client name, redirect host, scopes, resource, expiry, status and agent/customer choices. POST its `/decision`
with `{decision: "approve", agent_id, customer_ids, scopes?}` or `{decision: "deny"}`, then navigate to
the returned one-time `redirect_url`. Never store or log that URL; it carries the authorization code.
Full field descriptions are in `CONNECT-API.md` and generated OpenAPI. Accounts and tenant sign-in remain
future work; the current consent authority is the operator token.

## Authentication and newer backend behaviour (14.11, specs 15–20 — details in `docs/specs/`)
| What | Where | Observed while testing |
|---|---|---|
| Every call needs a credential | `Authorization: Bearer <token>`; without it `401 UNAUTHENTICATED` (`/health` stays open) | Keys are generated once with `backend/.venv/bin/python scripts/gen_keys.py --write` (adds `OPERATOR_TOKEN` and `AGENT_KEYS` to the repo-root `.env.local`); the API refuses to start without them |
| Console sign-in | `frontend/src/api.ts`, `Shell.tsx`, `Approvals.tsx` carry a small patch from the backend side for review | The operator token is typed into a field and kept in memory only — a full page reload asks again; the buyer page runs as `agent:web-console` under that agent's mandate; `X-Agent-Id` is ignored |
| Operator actions | approve / reject / cancel / policy / audit / stats / approvals need the operator token | Audit actor is `human:operator`; the typed name stays on the order (`approver`, `cancelled_by`) and in audit `data.stated_name` |
| Approval may come back | `POST …/approve` accepts `evidence_revision`; `409 EVIDENCE_CHANGED` with `data {evidence_revision, changes[]}` | Happens when stock or rule hits changed since the order was placed; the order stays pending with the refreshed warnings (`order.evidence_revision`) |
| Evidence wording | `matched_because[]` now names the source: "AE not listed in the catalog allergen declaration (request)", "reliability: no data (neutral)" | Products without allergen data no longer pass an allergen exclusion; unknown codes answer `422 UNKNOWN_CODE` with the valid values |
| Totals and ranking | `OfferCard.purchase_total`, `overbuy {amount, unit}`, `score_breakdown`; `rank_label` is empty for offers that cannot be ordered | Cards rank by what the packs cost in total |
| How much was looked at | `LookupResult.matched_count`, `retrieval {mode, terms_used}`, lookup `warnings[]` (`POOL_TRUNCATED`, `RETRIEVAL_BROADENED`) | "40 of 1,443 matching products screened" |
| New warning codes | `SIZE_UNCONFIRMED`, `UNIT_MISMATCH`, `STOCK_MODEL_UNKNOWN`, `CUTOFF_UNKNOWN`, `NO_STOCK`, `VARIABLE_WEIGHT`, `DEPOSIT` | Policy has `approval.ignored_warnings` (informational codes) and `customers` (standing constraints per customer, shown in `LookupResult.effective_constraints`) |
| Quotes | `need_ref` or `plan_ref` is required (`422 NEED_REF_REQUIRED` otherwise) | The console already sends `need_ref` |

## Demo context (from `docs/DECISIONS.md`)
The pitch opens by giving the challenge sentence ("1,000 L lactose-free milk by Monday …") to A.I.mo in My Aimo
and then to our agent; what our agent did is then shown through the UI.

## Running the backend locally
```
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # once
.venv/bin/python -m app.catalog build                                 # once
.venv/bin/python -m uvicorn app.api:app --port 8000
```
Needs the repo-root `.env.local` (VALIO_API_KEY, OPENAI_API_KEY — ask Gökalp). CORS allows `http://localhost:*`.
Sample data: `backend/demo/run_buyer_agent.sh` (Claude as buyer agent) or quotes/orders via `/docs`.
Need another field or endpoint? Ask the backend side — it is quick to add.
## Anomalies (spec 45)

Quotes and orders now include `anomalies[]`, also visible inside the existing
approval inbox, buyer order surfaces and MCP results. Each check contains `code`
(`QUANTITY_ANOMALY`, `PRICE_ANOMALY`, `VELOCITY_ANOMALY`, `NEW_CATEGORY`), `status`
(`normal`, `anomaly`, `insufficient_history`), optional `product_id`, `observed`,
`unit`, `expected_band`, `history_window`, `band_source` (`statistical` or
`forecast`), optional `explanation_text`, and `fixes[]`. `history_window.points`
is the actual sample count; start/end are evidence timestamps. Quantity may use a
weekday bucket (reported as `history_window.weekday`); category checks list known
categories in the window. No warning is raised with fewer than four observations.
The four anomaly warnings route the order through the usual human-approval status,
reason, polling and receipt journey. Anomalies are rechecked at placement, so the
order can have additional warnings compared with the original quote.

Fixes carry `id`, `kind`, `description`, and where relevant `product_id`,
`quantity_sales_units`, `remainder_sales_units` or `action_url`.
`POST /v1/quotes/{quote_id}/anomaly-fixes/{fix_id}` returns
`{source_quote_id, fix_id, quotes: [...]}`. Agent owners and scoped buyer approvers
can apply corrections, cheaper ranked alternatives and splits. Refresh the source
order after a fix: its pending promise is superseded (CANCELLED), and the returned
quotes need normal placement. Fresh prices and stock can change the offered fix;
constraint or authority failures are ordinary recovery responses. The remainder
of a split always requires human approval. Reapplying a consumed fix returns an
expired-quote response. No fix approves an order.

At twice the velocity upper band the pending order is retained and the agent is
paused with an audited reason. Further mutations answer `423 AGENT_PAUSED` with
`paused_by`, `paused_at`, `reason` and `next_action: CONTACT_ADMIN`. Reads remain
available. Velocity fixes expose the buyer-admin suspend/resume `action_url`;
send the normal `{reason}` body. The scoped `agent.anomaly_alert` event is queued
in the outbox for buyer administration; tenant ownership/alert screens arrive with
the later governance work. Current `policy.anomalies` fields are editable policy
settings; business-profile presets are outside this change. Synthetic reset
history is labelled `synthetic: true`; the normal hotel-chain milk demo stays
confirmed without anomaly warnings.


## Accounts and connections (spec 43)

This section describes backend data and behaviour; the frontend owner designs the screens.
Accounts, tenant isolation and REST audiences are implemented, together with operator-consented OAuth
for the buyer MCP resource. Tenant-account OAuth consent and tenant/operator MCP, A2A, webhook and
tool-pack audiences remain pending. Read `available` and
`oauth_available` from the connection catalog rather than assuming that every catalog entry is live.

**Sign-in and session.** `POST /v1/auth/login` takes `{email,password}` and returns
`{csrf_token,user,role,tenant_id,tenant,customer_ids,permissions}`. The server sets `vtg_session`
(`HttpOnly`, `SameSite=Lax`, `Secure` except on localhost). Use `credentials: 'include'` and keep the
CSRF token in memory or sessionStorage (clear it on sign-out). Send `X-CSRF-Token` on every cookie-authenticated POST, PUT, PATCH and DELETE.
`GET /v1/auth/session` returns the same account data without the CSRF token; it reads the current
role on every request. `POST /v1/auth/logout` requires CSRF and ends the current session. Retain the CSRF token in sessionStorage across a page
reload; sign in again if it was lost. Bearer keys remain available and do not require CSRF.
Use the session `role`, not a role cached on a connection, to choose available actions.

**Invites and password set.** Inviting returns `{user_id,link,token,expires_at}` once. An invite lasts
seven days. Submit `{password}` (10–1024 characters) to `POST /v1/auth/invites/{token}/accept`, then
sign in normally. Resending invalidates the old link. Password reset links last one day; submit
`{password}` to `POST /v1/auth/password-resets/{token}/accept`. A successful reset ends existing
sessions. Used/expired links return `410 LINK_EXPIRED`; link secrets are redacted in errors/logs.
The gateway returns links for the admin to share; it does not send email.

**Tenant team.** A `tenant_admin` uses the following `/v1/tenant/users` endpoints:

| Method and suffix | Data / behaviour |
|---|---|
| `GET /` | People: id, tenant_id, email, role, status (`invited`, `active`, `disabled`, `removed`), last_sign_in, invited_by, created_at, disabled_at |
| `POST /` | `{email,role}` → single-use invite; roles: tenant_admin, approver, viewer |
| `GET /{user_id}` | One person in the same tenant |
| `PATCH /{user_id}` | `{role?,disabled?}`; `disabled:false` re-enables |
| `DELETE /{user_id}` | Remove person; history remains |
| `POST /{user_id}/resend-invite` | Replace pending invite, return new link once |
| `POST /{user_id}/cancel-invite` | Invalidate pending invite and disable the pending account |
| `POST /{user_id}/password-reset` | Return a reset link once |

`GET /v1/tenant/roles` returns `{role,permissions[]}` entries for display. The last active tenant
admin cannot be demoted, disabled or removed (`409 LAST_TENANT_ADMIN`). Disabling/removing another
person ends their sessions and revokes every connection they created, including buyer keys.
Role changes take effect on the next request. Foreign users/objects return 404.

**Platform management.** Only `platform_admin` uses `/v1/operator/tenants`: GET lists tenants;
POST `{name,customer_ids}` creates one; PATCH `/{tenant_id}` accepts `{name?,customer_ids?,disabled?}`.
The PATCH customer list replaces that tenant's assignments and explicitly moves numbers from their
previous tenant. Each number has one owner. Disabling ends sessions and revokes connections.
`POST /v1/operator/tenants/{tenant_id}/users` invites the first tenant admin (`{email,role:'tenant_admin'}`).
`/v1/operator/users` exposes the same people endpoints as above for Valio staff; its role is platform_admin.

**Agents and authority.** Tenant admins list/create agents at `/v1/tenant/agents` (POST
`{agent_id,name}`), PATCH `/{agent_id}` with `{name?,disabled?}`, and issue/list mandates at
`/{agent_id}/mandates`. Issue body: `{customer_id,max_auto_total_eur?,max_auto_daily_total_eur?,autonomy?,valid_days?}`;
the signed VC-JWT is returned on issue. DELETE `/{agent_id}/mandates/{mandate_id}` revokes it.
Customer numbers must belong to the tenant. New agents start approval-first; creation alone does not
give purchasing authority. A connection grants a minimal approval-first policy mandate as in spec 42.
`GET /v1/tenant/agents/{agent_id}/track-record` and POST `/{agent_id}/autonomy` retain spec 40's
readiness and grant/withdraw behaviour; only the tenant admin can change autonomy.

**Connect tabs.** Public `GET /v1/connect/catalog?audience=buyer|tenant|operator` gives connection
types, clients, credential methods, documentation links and current availability. The shared client
registry also feeds instructions. Tenant admins use `/v1/tenant/connections`, platform admins can
use `/v1/operator/connections` and set `tenant_id` in the create body. POST takes
`{type,agent_id,client,customer_ids,scopes?,audience?,expires_at?,label?,tenant_id?}`. An optional
`Idempotency-Key` makes creation replayable, but a replay has `key:null`. API keys have form
`vtg_live_<connection_id>_<secret>` (or `vtg_sbx_...`) and are shown only on creation/rotation.
GET list and GET `/{connection_id}` return prefix, tenant, agent, audience, type, allowed customers,
scopes, creator and timestamps, never the secret. `GET /{connection_id}/instructions?client=...&audience=...`
returns ordered `steps`, `snippet`, `language`, `secret_placeholder`, URLs and MCP-derived tool schemas.
The named key placeholder is `VTG_CONNECTION_KEY`; the server cannot redisplay a lost secret.
POST `/{connection_id}/rotate` takes `{overlap_hours:0..24}` and returns a new key and
`old_key_valid_until`. DELETE `/{connection_id}` revokes immediately. Rotation/revocation do not
change the agent's mandate. Each connection has an independent rate bucket.

**REST permissions and decisions.** `/v1/tenant/orders` and its detail/receipt/UBL/timeline/evidence routes,
`/v1/tenant/audit`, `/v1/tenant/spend` and `/v1/tenant/suggestions` are tenant-scoped reads for
viewer, approver and tenant_admin. Approver/admin use `/v1/tenant/approvals` and POST
`/{order_id}/approve` or `/reject`; the body is `{approver,comment?,evidence_revision?,confirmation_token?}`.
They can also POST `/v1/tenant/orders/{order_id}/cancel`. Only tenant_admin manages people,
agents, connections, mandates and `/v1/tenant/policy`. The tenant policy PUT accepts an
`{approval:{...},reject:{on:[...]},confirmation_token?}` overlay which can only tighten platform ceilings.
Platform admins use the corresponding `/v1/operator/orders`, `/approvals`, `/stats`, `/audit`,
`/policy`, `/suppliers`, `/health` routes across tenants. Existing operator paths remain available.
Buyer-agent credentials use `/v1` procurement routes; they cannot use tenant/operator audiences.
Tenant/operator connection keys carry their creating human's user id and enforce the human's live role.
Cross-audience human keys return 401; agent credentials at human audiences return 403.

For human approve/reject/cancel and policy/autonomy changes, first submit the action without a
confirmation token. The response has `{confirmation_required:true,confirmation_token,expires_at,summary}`
and makes no business decision. Display that summary and resubmit the identical body plus the token
after the human confirms. Tokens are single-use, user/tenant/action/payload/evidence-bound, valid five
minutes. Orders record `approver_user_id` and `approver_role`. `409 EVIDENCE_CHANGED` requires reading
fresh evidence and starting a new confirmation. `409 CONFIRMATION_INVALID` requires starting again.
Existing `OPERATOR_TOKEN` callers keep their platform-admin identity; human decisions on the existing
paths also require this confirmation handshake.

**OAuth consent contract.** The operator flow reads
`GET /v1/oauth/requests/{id}` to show the client name and redirect host, agent choices, allowed
customers and requested scopes, then calls `POST /v1/oauth/requests/{id}/decision` to approve or deny.
These endpoints and `FRONTEND_CONSENT_URL` are available for the platform operator. The plain consent
page also remains available. Tenant-user consent is still pending; catalog availability describes
the tenant-managed connection subset.

All failures use the existing RFC 9457 problem shape (`code`, `next_action`, `detail`, correlation id).
Relevant codes: `401 UNAUTHENTICATED`/`WRONG_AUDIENCE`, `403 CSRF_REQUIRED`, `HUMAN_REQUIRED`,
`TENANT_ADMIN_REQUIRED`, `OPERATOR_REQUIRED`, `INSUFFICIENT_SCOPE`, `CUSTOMER_NOT_IN_MANDATE`,
`POLICY_CEILING`; `404 *_NOT_FOUND`; `409 LAST_TENANT_ADMIN`, `EMAIL_EXISTS`, `AGENT_EXISTS`,
`CUSTOMER_ASSIGNED`, `CONFIRMATION_INVALID`, `EVIDENCE_CHANGED`; `410 LINK_EXPIRED`;
`429 LOGIN_THROTTLED`/`RATE_LIMITED` with `Retry-After`. Login throttles email and client address.
