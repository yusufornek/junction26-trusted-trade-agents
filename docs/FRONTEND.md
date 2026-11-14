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

## Accounts and connections (spec 43)

This section describes backend data and behaviour; the frontend owner designs the screens.
Part 1 implements accounts, tenant isolation and REST audiences. OAuth and the tenant/operator MCP,
A2A, webhook and tool-pack audiences are part 2 and are not implemented yet. Read `available` and
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

**OAuth consent contract (part 2).** The planned frontend flow reads
`GET /v1/oauth/requests/{id}` to show the client name and redirect host, tenant agents, allowed
customers and requested scopes, then calls `POST /v1/oauth/requests/{id}/decision` to approve or deny.
These endpoints and `FRONTEND_CONSENT_URL` are not available in part 1. Do not render a working
consent button until the part 2 contract lands; use catalog availability for current choices.

All failures use the existing RFC 9457 problem shape (`code`, `next_action`, `detail`, correlation id).
Relevant codes: `401 UNAUTHENTICATED`/`WRONG_AUDIENCE`, `403 CSRF_REQUIRED`, `HUMAN_REQUIRED`,
`TENANT_ADMIN_REQUIRED`, `OPERATOR_REQUIRED`, `INSUFFICIENT_SCOPE`, `CUSTOMER_NOT_IN_MANDATE`,
`POLICY_CEILING`; `404 *_NOT_FOUND`; `409 LAST_TENANT_ADMIN`, `EMAIL_EXISTS`, `AGENT_EXISTS`,
`CUSTOMER_ASSIGNED`, `CONFIRMATION_INVALID`, `EVIDENCE_CHANGED`; `410 LINK_EXPIRED`;
`429 LOGIN_THROTTLED`/`RATE_LIMITED` with `Retry-After`. Login throttles email and client address.


## Agent governance (spec 44)

All routes below are relative to the gateway origin. Human sessions use the spec 43 cookie and CSRF token;
tenant connections use a Bearer key with `audience=tenant`. A `member` owns and manages its agents only;
`tenant_admin` manages the tenant. An `approver` can also own agents while retaining approval permissions.
Foreign or another member's agent targets return `404`.

| Endpoint | Data / behaviour |
|---|---|
| `GET /v1/tenant/agents?owner_user_id=&status=` | Register: `agent_id`, `owner_user_id`, `status` (`active`, `paused`, `disabled`), connections/clients, autonomy, `budget_use.day_eur/month_eur`, order/approval counts, refusals by code, last activity, effective version |
| `POST /v1/tenant/agents` | Create an agent owned by the caller; operator-created and seeded agents belong to the tenant's first admin |
| `GET /v1/tenant/agents/{id}/policy` | Current agent layer version, author, time, reason, diff, policy data |
| `GET /v1/tenant/agents/{id}/policy/effective` | `version_id` and `fields`: each field has `value`, `source_layer`, `version_id`, `author`, and all contributing `sources` |
| `PATCH` or `PUT /v1/tenant/agents/{id}/policy` | Partial `{policy, reason}`; tightening applies, loosening becomes a pending request |
| `POST /v1/tenant/agents/{id}/policy/simulate` | `{policy}` → last 30 days' stored quote/order counts: `passed`, `needed_approval`, `refused`, examples with rule identifiers; no supplier calls |
| `GET /v1/tenant/agents/{id}/policy/versions[/{version_id}]` | Immutable agent policy versions |
| `POST /v1/tenant/agents/{id}/policy/versions/{version_id}/rollback` | `{reason}`; same tightening/loosening rules as an edit, creates a new version |
| `GET /v1/tenant/policy/change-requests?agent_id=` | Visible requests, status, requester, base version, proposed policy, classification, simulation, reviewer/reason/time when decided |
| `GET /v1/tenant/policy/change-requests/{id}` | One visible request |
| `POST /v1/tenant/policy/change-requests/{id}/approve` or `/reject` | `{reason}`; tenant admin, never the requester; approval rechecks guardrails and the unchanged base policy |
| `GET /v1/tenant/agents/{id}/activity` | Audit entries newest first, with `policy_version_id` (historical entries may have no version) |
| `POST /v1/tenant/agents/{id}/pause` or `/resume` | `{reason}`; owner or admin; pending approvals and quote history remain |
| `POST /v1/tenant/agents/pause` or `/resume` | `{reason}`; tenant stop is independent of individual pauses |
| `POST /v1/tenant/agents/{id}/transfer` | `{owner_user_id, reason}`; admin transfers to an active member, preserving all history; transfer does not automatically resume |
| `GET` / `PUT /v1/tenant/guardrails` | Current guardrail version / `{policy, reason}` change; admin only |
| `GET /v1/tenant/guardrails/versions[/{id}]` | Guardrail history |
| `POST /v1/tenant/guardrails/versions/{id}/rollback` | `{reason}`; platform bounds still apply |
| `GET /v1/tenant/alerts` | Alerts visible to the caller's agents, newest first |
| `GET /v1/tenant/governance/report?from=<ISO>&to=<ISO>&signed=true` | `{report, signature, jwks_url}`; without signed=true returns the report JSON directly |

Policy fields: `allowed_customers`, `allowed_operations` (`lookup`, `quote`, `order`),
`per_order_ceiling_eur`, `daily_budget_eur`, `monthly_budget_eur`, `allowed_categories`,
`blocked_categories`, `required_claims`, `excluded_allergens`, `substitutes_allowed`, `ordering_hours`,
`delivery_day_window: {min_days,max_days}`, `autonomy_level` (`approval_required` or
`autonomous_within_limits`), `approval_routing: {approver_user_ids,tenant_admin_above_eur}`.
Nested fields in the effective response use dotted names, e.g. `approval_routing.tenant_admin_above_eur`.
Null bounds/allowlists inherit the parent restriction; empty allowlists permit nothing. Clearing a restriction is a loosening and follows change review.
Ordering hours are UTC integer hours 0..23, delivery days are offsets from the operation's UTC date.
Governance budgets count confirmed/pending-execution spend for the UTC calendar day/month, including human-approved
spend; calendar budgets default to unrestricted until explicitly set. The earlier rolling 24-hour autonomous mandate limit remains enforced as well.

Every governance mutation uses the spec 43 two-step confirmation: the first response is
`{confirmation_required:true, confirmation_token, expires_at, summary}` with no applied change;
retry the same endpoint and body plus `confirmation_token` after human confirmation. The token is single-use,
bound to user, target and payload, and valid for five minutes. A loosening then returns
`{status:"pending", change_request:{...}}`; tightening returns `{status:"applied", version:{...}}`.
Requests transition `pending` → `approved` or `rejected`. The requester cannot decide their own request, even when
also an admin. A changed base produces `409 POLICY_CHANGED`; submit a fresh proposal with a new simulation.
Disabling/removing a user returns `agents_to_transfer` and pauses those agents; resume requires an active owner.

Errors: `422 OUTSIDE_GUARDRAIL` contains `field`, `limit`, `source_layer`, `version_id`, `author`;
`423 AGENT_PAUSED` contains `agent_id`, `paused_by`, `paused_at`, with `next_action=CONTACT_ADMIN`;
`403 SELF_APPROVAL_FORBIDDEN`, `403 APPROVAL_ROUTING_REQUIRED` (above the routing threshold, the approver must be a tenant admin and satisfy the named approver list), `409 OWNER_UNAVAILABLE`,
`409 CONFIRMATION_INVALID`, `409 POLICY_CHANGED`; foreign targets are `404`.
Paused agents cannot lookup, quote or order, including from existing open quotes; humans can still review pending approvals.

Alerts have `id`, `type`, `agent_id`, `tenant_id`, `created_at` and type-specific data:
`budget.80` / `budget.100` (`period`, `used_eur`, `limit_eur`, policy version), `refusal.burst`
(`count`, `window_minutes=10`), `activity.outside_hours`, `connection.new_client` (`connection_id`, `client`),
`connection.new_address` (`connection_id`, `address`), `policy.change_pending` (`request_id`).
Threshold alerts are deduplicated per agent, period, threshold and limit.
Tenant admins create signed subscriptions at `/v1/tenant/webhook-subscriptions` with `audience:"tenant"` and
`event_types:["governance.alert", ...]`; Standard Webhooks Ed25519 verification/retries are unchanged.
Other governance event types: `governance.agent_registered`, `governance.policy_changed`, `governance.guardrails_changed`,
`governance.change_requested`, `governance.request_decided`, `governance.rolled_back`,
`governance.agent_paused`, `governance.agent_resumed`, `governance.tenant_paused`,
`governance.tenant_resumed`, `governance.agent_transferred`.

The tenant MCP resource is `/mcp/tenant`, with the `agent_governance(operation, arguments)` tool exposing the
same operations. Mutations use the MCP 2026-07-28 `input_required` result: echo `requestState` and supply
`inputResponses.confirmation={action:"accept",content:{confirm:true}}` only after the human's answer.
The tenant A2A 1.0 card is `/a2a/tenant/agent-card.json` (also `/.well-known/agent-card/tenant.json`),
endpoint `/a2a/tenant`, `SendMessage` with one JSON data part containing `operation`, `arguments`, and an optional
`confirmation_token`; the same two-step confirmation applies. Buyer credentials cannot use either tenant surface.
The frontend owner chooses the screens; this section defines data and behaviour only.
