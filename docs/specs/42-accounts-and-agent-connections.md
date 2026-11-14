# 42 — Accounts, tenants and agent connections

Status: in progress · Decided on 2026-11-14 with Gökalp.

## Purpose
A buyer business (a hotel, a restaurant, a hospital kitchen) signs in, connects its own procurement agent over the
protocol it already uses — MCP, REST, A2A, webhooks or a tool-calling pack — and every call that agent makes is
bound to that business's account: its customer numbers, its mandates, its approvers, its data. Valio's own staff
work in a separate platform role that sees every tenant. One gateway serves many buyer businesses with strict
isolation between them.

## Roles
| Role | Who | Sees and does |
|---|---|---|
| `platform_admin` | Valio staff (today's operator; `OPERATOR_TOKEN` keeps working as this role) | All tenants; platform policy ceilings; supplier health; global audit, anchoring and stats; can approve any order |
| `tenant_admin` | Owner or manager of a buyer business | Own tenant: users, agents, connections, mandates, tenant policy, approvals, orders, audit view |
| `approver` | Buyer staff who sign off purchases | Own tenant: approvals inbox, orders, evidence |
| `viewer` | Buyer staff who follow purchases | Own tenant: orders, evidence, audit view (read only) |
| `agent` | A connected procurement agent | Buyer operations for its tenant's customers within its mandate |

The frontend teammate decides how the screens are arranged; the backend exposes the role in the session and serves
each role only its own data.

## Requirements
- **R-1 Tenants:** a `tenants` table (id, name, created_at) and a mapping of Valio customer numbers to exactly one
  tenant. Every stored business object — lookups, quotes, orders, approvals, mandates, policy overlays, webhooks,
  purchase memory, connections, track records — carries `tenant_id`. Every read and write filters by the caller's
  tenant; another tenant's object answers `404`. `platform_admin` reads across tenants. The audit chain stays one
  global hash chain; each entry carries `tenant_id` and tenant views filter it.
- **R-2 Human accounts:** users (tenant_id or platform, email unique, role, scrypt password hash with per-user
  salt, created_at, disabled_at). `POST /v1/auth/login` sets an `HttpOnly`, `Secure` (except on localhost),
  `SameSite=Lax` session cookie and returns a CSRF token; state-changing cookie-authenticated requests carry it in
  `X-CSRF-Token`. `POST /v1/auth/logout`, `GET /v1/auth/session` (user, role, tenant, customer numbers). Login
  attempts are throttled per email and per client address. Bearer credentials keep working on every endpoint.
- **R-3 Demo seed:** `scripts/reset_demo.py` creates tenants "Hotel Aurora" (customer 1, agent
  `hotel-chain-agent` and the console agent `web-console`) and "Bistro Sisu" (customer 2, new agent
  `bistro-sisu-agent` with `approval_required` and its own mandate), one `tenant_admin` and one `approver` per
  tenant, and a `platform_admin`. Agents without a mandate (for example `bistro-agent`) keep answering
  `403 AGENT_NOT_REGISTERED`. `scripts/gen_keys.py --write` adds the demo user passwords to `.env.local`
  without printing them. Existing `AGENT_KEYS` entries map to their seeded tenants and keep working.
- **R-4 Agents per tenant:** `tenant_admin` creates, renames and disables agents of its tenant
  (`/v1/tenant/agents`). Agent ids are unique platform-wide. Graduated autonomy (spec 40) applies per agent;
  promotion beyond `approval_required` needs a `tenant_admin` of that tenant (and stays within platform ceilings).
- **R-5 Mandates per tenant:** each tenant has its own Ed25519 issuer key; `tenant_admin` issues and revokes the
  W3C VC mandates (spec 12) of its agents from the API (`/v1/tenant/agents/{id}/mandates`). A mandate can only name
  customer numbers that belong to the issuing tenant.
- **R-6 Connection catalog (public):** `GET /v1/connect/catalog` lists connection types and, per type, the
  supported clients, the credential methods and the documentation links. Types: `mcp` (remote streamable HTTP and
  local stdio), `rest`, `a2a`, `webhook`, `tool_pack`. Clients include Claude Desktop, Claude Code, ChatGPT,
  Cursor, VS Code, OpenAI Agents SDK, Anthropic API, LangChain, generic MCP, curl, Python, TypeScript and A2A
  clients.
- **R-7 Connections:** `tenant_admin` creates a connection (`POST /v1/tenant/connections` with type, agent id,
  allowed customers ⊆ tenant customers, scopes, optional expiry). API-key connections return the secret once, in
  the creation response only, formatted `vtg_<env>_<public id>_<secret>` (env `sbx` or `live`); the server stores a
  SHA-256 digest of the high-entropy secret and the public id. List, read (prefix only, created_by, created_at,
  last_used_at, expires_at, revoked_at), rotate (new secret, old one valid for a stated overlap of at most 24 h)
  and revoke (effective on the next request). Each connection has its own rate-limit bucket.
- **R-8 Instructions:** `GET /v1/tenant/connections/{id}/instructions?client=<client>` returns ordered steps and
  a ready configuration snippet for that client with the gateway URLs and connection id filled in and the secret
  shown as a named placeholder. Snippets are generated from the same tool schemas as the MCP server; a test runs
  each REST, Python and MCP snippet against the in-process gateway.
- **R-9 OAuth 2.1 for remote MCP and A2A:** the gateway is an OAuth 2.1 authorization server and resource server
  following the vendored MCP authorization specification (`backend/vendor/mcp-authorization/`, revision
  2026-07-28, accepting 2025-11-25 clients): protected resource metadata (RFC 9728) at
  `/.well-known/oauth-protected-resource` and its `/mcp` path form, with `WWW-Authenticate` on `401`;
  authorization server metadata (RFC 8414); authorization code with PKCE `S256` only; resource indicators
  (RFC 8707) binding each token to the gateway's MCP or A2A resource; `iss` in the authorization response
  (RFC 9207); client registration through Client ID Metadata Documents, pre-registration and Dynamic Client
  Registration (RFC 7591, `application_type` required); exact redirect URI matching; refresh-token rotation with
  reuse detection that revokes the token family. Access tokens are opaque, stored as digests, live 1 h; refresh
  tokens live 30 days.
- **R-10 Consent:** `GET /oauth/authorize` needs a signed-in human of the tenant. The consent step shows the client
  name and redirect host, lets a `tenant_admin` pick an existing agent of the tenant or create one, choose allowed
  customers and scopes, and approve or deny. The backend serves a plain consent page and also exposes
  `GET /v1/oauth/requests/{id}` + `POST /v1/oauth/requests/{id}/decision` so the frontend can render its own
  screen (`FRONTEND_CONSENT_URL` selects it). An approved consent creates an `oauth` connection visible in the
  connection list and revocable like any other.
- **R-11 A2A:** the Agent Card (spec 34) advertises an OAuth 2.0 authorization-code security scheme pointing at
  the same authorization server and an HTTP bearer scheme for API-key connections; A2A calls resolve to the
  connection's tenant and agent.
- **R-12 Webhook and tool pack types:** a `webhook` connection registers a tenant endpoint for the signed events of
  spec 33, scoped to the tenant. `GET /v1/connect/tool-pack?format=openai|anthropic|gemini` returns function
  declarations generated from the MCP tool schemas plus a short executor snippet that calls the REST API with a
  connection key.
- **R-13 Binding is strict:** a credential resolves to exactly one tenant, agent and connection; its customers are
  the intersection of the connection's allowed customers and the agent's mandate; tokens presented to a resource
  other than their audience answer `401`; the gateway never forwards a client's token upstream; revoked, expired
  or rotated-out credentials answer `401` with recovery guidance (spec 23). Credential secrets, digests, session
  ids and passwords appear in no response after creation, log line, audit entry or error.
- **R-14 Approvals per tenant:** a tenant's `approver` and `tenant_admin` approve or reject that tenant's pending
  orders; `platform_admin` can too. Approvals record the human user id and role. Tenant policy overlays may only
  tighten the platform policy.
- **R-15 Audit:** login success and failure (email digest only), connection create / rotate / revoke, consent
  approve / deny, token family revocation, mandate issue / revoke and role changes are audited with the tenant.
- **R-16 Developer surface:** `/developers`, `/llms.txt` and `GET /v1/me` (spec 38) describe accounts,
  connection types and both credential methods; `GET /v1/me` shows the connection id, tenant and allowed
  customers.

## Acceptance criteria
- **AC-1** Tenant A's agent cannot read, quote from, order from or approve tenant B's objects (`404`), and cannot
  look up for tenant B's customer (`403 CUSTOMER_NOT_IN_MANDATE`, zero upstream calls); `platform_admin` reads both.
- **AC-2** Login with the right password sets the session cookie; wrong password answers `401` and repeated
  failures are throttled; a cookie request without `X-CSRF-Token` on a state-changing endpoint answers `403`.
- **AC-3** `tenant_admin` creates a REST connection; the secret works once copied; the list shows only its prefix;
  revoke makes the next call `401`.
- **AC-4** Rotation: the old secret works during the overlap and answers `401` after it; the new one works.
- **AC-5** A connection limited to customer 1 cannot act for another customer of the same tenant.
- **AC-6** Full OAuth flow in-process: discovery via `401` + protected resource metadata, Client ID Metadata
  Document registration (fetched URL mocked), authorize with PKCE, consent by `tenant_admin`, token for the MCP
  resource, `tools/list` and a full sandbox purchase over MCP HTTP as that tenant's agent.
- **AC-7** Token checks: wrong `code_verifier`, mismatched redirect URI, token issued for another resource and a
  reused refresh token each fail; refresh reuse revokes the whole family.
- **AC-8** Dynamic Client Registration without `application_type` answers `400`; with it, the flow of AC-6 works.
- **AC-9** Instructions for each listed client return a snippet; the curl, Python and MCP snippets run against
  the in-process gateway and complete a sandbox lookup.
- **AC-10** The A2A Agent Card lists both security schemes; an A2A call with a tenant B token cannot touch tenant
  A's order.
- **AC-11** Tenant B's approver cannot approve tenant A's order (`404`); tenant A's approver can, and the order
  records the user id.
- **AC-12** No secret, digest, session id or password in any response after creation, the audit log or the
  logs (scan test).
- **AC-13** Existing `AGENT_KEYS` and `OPERATOR_TOKEN` callers keep working as their seeded tenant agent and as
  `platform_admin`; the demo path (customer 1, 1000 L lactose-free milk → 464306, 1,010 €) is unchanged.
