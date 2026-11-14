# 62 — Connection trust lifecycle

Status: in progress · Decided on 2026-11-14 with Gökalp (AI-gateway focus; drafted by Fable from Astra's round 2).
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
