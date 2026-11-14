# 62 — Connection trust lifecycle

Status: built · Decided on 2026-11-14 with Gökalp (AI-gateway focus; drafted by Fable from Astra's round 2).
Builds on specs 40, 42, 43 and 44.

## Purpose
A connection stays tied to the client and agent the human actually approved, and loses its power the moment the human
withdraws it. Covers consent, token refresh, credential leak or copy, pause and revoke.

## Requirements
- **R-1 Client verification tier:** every OAuth client is `verified` (pre-registered, or a Client ID Metadata Document
  whose host is on the platform's verified-publisher list and matches the redirect host) or `unverified` (dynamic
  registration). Consent shows the tier, redirect host and registration date. An unverified client whose name resembles
  a verified publisher is shown as "not verified — not <publisher>" without a logo. Unverified clients receive lookup
  and quote scopes with `approval_required`; order scope needs a `tenant_admin`.
- **R-2 Authorization request binding:** each authorization is one server-side record (request id, client, redirect
  URI, PKCE challenge, resource, signed-in user, tenant, agent, customers, scopes, notification consent). Decision, code
  and token are derived from that record alone.
- **R-3 Refresh grace and recovery:** a rotated-out refresh token presented again within 30 s by the same client and
  context returns the same successor pair. Any other reuse revokes the family and sets the connection to
  `REAUTH_REQUIRED`. Intents, tasks and pending approvals stay and resume after re-consent (47 R-4).
- **R-4 Live authority:** connection status, agent pause and policy epoch are evaluated at the commit of every state
  change and before every emission on a stream, subscription or push. Pause or revoke closes open MCP streams, cancels
  subscriptions and push configurations, and fails queued calls with `423 AGENT_PAUSED` or `401` within 5 s.
- **R-5 Continuity of the governed agent:** earned autonomy (spec 40) belongs to agent + connection + client identity.
  A new connection, or a new context (network, protocol or client), for an existing agent runs at `approval_required` until the owner
  confirms it is the same agent. Simultaneous use of one credential from two contexts answers `CONCURRENT_CONTEXT`: the
  connection becomes read-only and the owner is asked. OAuth connections may use DPoP (RFC 9449) sender-constrained
  tokens. Seeded demo agents are marked as confirmed.
- **R-6 Surfaces and audit:** tier, context history and state are in connection health (60 R-8). Events
  `CONNECTION_REAUTH_REQUIRED`, `CONCURRENT_CONTEXT` and `CONNECTION_CONTINUITY_CONFIRMED` are audited and notified
  (spec 61).

## Acceptance criteria
- **AC-1** A dynamically registered client named like a verified publisher shows as unverified on consent and its
  connection cannot order until a `tenant_admin` grants it.
- **AC-2** Two authorization requests for two agents, completed in interleaved order from one browser session, each
  bind to their own agent and customers.
- **AC-3** Two workers refresh with the same token within the grace window and both get the same pair; a reuse after the
  window sets `REAUTH_REQUIRED`, and the open intent is resumed after re-consent without a duplicate order.
- **AC-4** Pausing an agent with an open stream, a resource subscription and a queued order: the stream closes, no
  further update is delivered, and the order answers `423`.
- **AC-5** A key copied to a second runtime and used in parallel answers `CONCURRENT_CONTEXT` and turns read-only; a new
  connection for an agent with earned autonomy needs approval for its first order until the owner confirms continuity.
- **AC-6** The demo path (464306, 1,010 €, CONFIRMED) is unchanged.

## Implementation

- `backend/app/connection_trust_lifecycle.py`: additive storage, publisher tiers, complete consent bindings,
  live authority, context history, connection-local continuity, owner inbox and lifecycle events.
- `backend/app/connection_trust_oauth.py`: adapters for both existing OAuth registries, transactional refresh
  rotation, encrypted successor replay and re-consent recovery of the original connection.
- `backend/app/connection_trust_transports.py`: native MCP `ListenHandler` closure, per-emission checks,
  idle HTTP stream cancellation and queued supplier/push authority checks.
- Small integration hooks: `api.py`, `auth.py`, `db.py`, `service.py`, `connections.py`, `orders.py`, `policy.py`,
  `oauth.py`, `oauth_audiences.py`, `agent_surface.py`, `webhooks.py`, `a2a.py`, `foundation.py`, `tenancy.py`,
  `mcp_server.py`, `connection_contract_surface.py` (all under `backend/app/`). `.env.example` adds the publisher
  configuration name; `docs/TASKS.md` changes only task 62.
- REST continuity confirmation: `POST /v1/connections/{id}/trust/confirm` with `{confirmed: true, context_id?}`.
  Only the agent owner (or platform administrator) can confirm; ambiguous histories require an observed context id.
- REST order grant: `POST /v1/connections/{id}/trust/order-scope`, restricted to a tenant/platform administrator.
- Health includes `trust`; the owner's `/v1/connection-notifications` inbox and `luotsi://inbox` expose lifecycle
  notices. Audit events enter the existing outbox. No frontend changes or generated artifacts are required in this lane.

## Decisions taken

- Observed context is the server-observed network peer, transport family (REST, MCP, A2A), and client User-Agent.
  Two contexts used within 30 seconds count as simultaneous. An owner selects the surviving observed context;
  another runtime using the credential again triggers the same protection. Bearer credentials cannot distinguish
  runtimes with identical observable context. Optional DPoP is not advertised or implemented.
- A new connection always starts unconfirmed, including one for an existing demo agent. Deployment-provisioned
  seed credentials retain explicitly confirmed identities, preserving the original autonomous demo path.
- Unverified clients remain `approval_required` after an administrator grants order scope or the owner confirms
  continuity; neither action verifies the publisher. A platform administrator may exercise the existing operator
  consent authority; ordinary members may consent only for their own existing agent.
- The platform-controlled verified-publisher host/name map is configured with `OAUTH_VERIFIED_PUBLISHERS` (JSON).
  With no override it contains OpenAI and Anthropic's publisher hosts. Client metadata cannot set its own tier.
  Pre-existing audience registry entries did not retain registration timestamps; migration records the import date.
  Publisher resemblance uses normalized fuzzy matching, including common Greek/Cyrillic visual confusables.
- Refresh/context grace uses wall time, independently of a sandbox's frozen procurement clock. The same successor
  response is encrypted at rest for the grace window with a separate mode-0600 key beside the database; workers and
  restarts share it. Durable token records continue to retain digests only; expired grace entries are purged on writes.
- Re-consent reuses a connection only for the same human, tenant, agent, client, resource, customers and scopes.
  Previously revoked connections never revive. Purchase records, approvals, A2A tasks and supplier idempotency
  references are retained; blocked supplier work can resume under current authority.
- SDK subscription handlers poll authority every 200 ms and check before emitting. HTTP MCP streams do likewise;
  policy epoch changes close streams so the client refetches current authority. Pause/revoke disables associated
  webhook consumers and removes A2A push configurations within the same writer transaction.
  Open streams renew their observed context lease so an idle stream still counts as concurrent credential use.
- Native driving-test tools replace the generated REST adapters under the same names, like the existing native
  acknowledgement tool. The legacy `full_surface` constructor keyword remains accepted with the current complete
  inventory. A withdrawn connection cannot record a capability proof when its subscription finishes closing.
- Spec 61's dedicated notification implementation and spec 47's dedicated intent API are absent in this tree.
  This capability uses the existing durable outbox, owner inbox, purchase/lookup intent records and A2A storage,
  leaving their records intact and providing the event seam for the notification lane.

## Acceptance mapping

Exactly one focused test per criterion in `backend/tests/test_spec62_connection_trust_lifecycle.py`:

| AC | Test |
|---|---|
| AC-1 | `test_AC_1_dynamic_publisher_impersonation_and_admin_order_grant` |
| AC-2 | `test_AC_2_interleaved_browser_authorizations_keep_complete_bindings` |
| AC-3 | `test_AC_3_parallel_refresh_grace_reauth_and_existing_purchase_resume` |
| AC-4 | `test_AC_4_pause_closes_sdk_stream_cancels_subscription_and_refuses_queued_order` |
| AC-5 | `test_AC_5_parallel_credential_contexts_and_connection_local_autonomy` |
| AC-6 | `test_AC_6_seeded_demo_remains_464306_1010_confirmed` |

Run from `backend/` with the provided venv: `python -m pytest tests/test_spec62_connection_trust_lifecycle.py -q`.

## Compatibility notes

The earlier spec-42 assertions that new connections immediately inherit earned autonomy are explicitly superseded
by R-5. Existing REST/OAuth examples that omit spec-63 contract acknowledgement/reevaluation can return
`CONTRACT_STALE`; this lane does not weaken the contract boundary or rewrite those historical tests.

## Verification (offline, 2026-11-15)

- Spec 62 acceptance file: **6 passed**, exactly one test per AC. AC-1 also checks a Unicode publisher impersonation;
  AC-3 exercises both OAuth registries; AC-4 uses the real installed MCP SDK client/listener and a mocked webhook
  receiver; AC-5 proves a post-confirmation order can be autonomous.
- Existing `test_graduated_autonomy.py`, `test_orders_policy.py`, `test_webhooks.py`: **77 passed**.
- Combined acceptance + `test_spec60_agent_surface.py`, `test_spec63_connection_contract_driving_test.py`,
  `test_api_mcp.py`: **26 passed, 3 failed** (the 26 includes all six spec-62 tests). The SDK-only remainder is
  20 passed / 3 historical expectation failures:
  - Spec 60 AC-8 changes one key from MCP to REST within the context lease and expects no continuity challenge.
  - Spec 63 AC-6 creates a new connection for the autonomous demo agent and expects immediate autonomy.
    R-5 now requires owner confirmation; the existing deployment-seeded demo path still passes spec 62 AC-6.
  - `test_contract_all_routes_have_response_schema` rejects routes outside its historical path whitelist;
    the new connection trust/notification routes require merge reconciliation.
- Earlier combined compatibility attempts were interrupted or timed out; no full suite was run. Earlier spec-42
  inheritance assertions and stale contract examples were left unchanged. Native driving-tool registration and the
  legacy constructor keyword were reconciled; the affected spec-63 AC-3/AC-5 checks also passed independently.
- No network calls, git commands, commits, frontend edits, generated artifact edits or `backend/var/` writes.
  Generated tool/developer/OpenAPI artifacts and historical test expectations remain merge-time work.
