# 42 — Agent connections and MCP on the public address

Status: in progress · Decided on 2026-11-14. Contract: `docs/CONNECT-API.md` (the console's `/connect` page is built
and deployed against it; frontend types `ClientKind`, `Connection`, `NewConnection` in `frontend/src/api.ts`).

## Purpose
An operator connects a procurement agent from the console in one step: pick the tool the agent works in, receive a
key once, paste the tool's setup, and see the agent's first call arrive. The key authenticates the agent on REST
and on MCP served at the gateway's public address.

## Requirements
- **R-1 Create:** operator-only `POST /v1/connections` with `Idempotency-Key`, through the existing command
  framework (replays return the stored result, a different body with the same key answers `409`). Body
  `{agent_id, client, customer_ids, label?}`: `agent_id` matches `^[a-z0-9][a-z0-9-]{1,47}$`; `client` is one of
  `claude_desktop | claude_code | cursor | vscode | chatgpt | mcp_other | rest` (informational: shown in the list
  and the audit entry, it does not change what the key can do); every customer id is a known customer. Invalid
  input answers `422`. `201` returns a `NewConnection`: `connection_id` (`con_` + 8 hex), `agent_id`, `client`,
  `key_prefix`, `created_at`, `last_used_at: null`, `revoked_at: null`, `key`, `mcp_url`, `rest_base_url`,
  `mandate {customer_ids, max_auto_daily_total_eur, autonomy}`.
- **R-2 Key handling:** the key is high-entropy, starts with `vak_`, and is returned once, in the `201` response
  only (an idempotent replay returns the stored response without the key and with `key: null`). The database
  keeps its SHA-256 digest and `key_prefix` (its first 8 characters). The key appears in no other response, log
  line, audit entry or error.
- **R-3 Agent and mandate:** a new `agent_id` is registered with a mandate for `customer_ids` and
  `autonomy: approval_required` (spec 40) and the policy's default daily limit; an existing agent keeps its mandate,
  autonomy and history unchanged, and the response reports its current mandate.
- **R-4 Audit:** `CONNECTION_CREATED` by `human:operator` with `connection_id`, `agent_id`, `client`, `key_prefix`.
- **R-5 List:** operator-only `GET /v1/connections` returns `Connection[]` (`connection_id, agent_id, client,
  key_prefix, created_at, last_used_at, revoked_at`), newest first, revoked ones included.
- **R-6 Revoke:** operator-only `DELETE /v1/connections/{connection_id}` sets `revoked_at` and returns the
  `Connection`; the key answers `401` from its next call on; the agent's mandate, autonomy and history stay.
  Audit `CONNECTION_REVOKED`. Unknown id answers `404`; revoking twice returns the same `revoked_at`.
- **R-7 Authentication:** `resolve_principal` accepts connection keys exactly like `AGENT_KEYS` entries (constant-time
  digest comparison, principal `agent:<agent_id>`), and refuses revoked keys. `last_used_at` is updated at most once
  a minute per key.
- **R-8 MCP in the gateway:** the gateway process serves MCP over streamable HTTP at exactly `/mcp` (not
  `/mcp/mcp`), against the same database and keys; the MCP session manager runs inside the gateway lifespan. Each
  request resolves its agent from the Bearer key (`PRINCIPAL` contextvar) — no agent bound at start-up — and audit
  entries keep the actor `agent:<agent_id>`. A request without a valid key answers `401`. A JSON-RPC `ping` to
  `/mcp` gets a non-`404` answer. `docs/DEPLOY.md` keeps one process per database.
- **R-9 URLs:** `mcp_url` is the public origin + `/mcp` when this process serves MCP, otherwise `null`;
  `rest_base_url` is the public origin + `/v1`. The public origin comes from the configured public URL, falling
  back to the request's origin.
- **R-10 Contract:** `contracts/openapi.yaml` regenerated; field names match `frontend/src/api.ts`.

## Acceptance criteria
- **AC-1** Operator creates a connection for a new agent → `201`, key once, `key_prefix` = first 8 characters,
  mandate `approval_required` for the given customers; the database holds the digest, not the key.
- **AC-2** Creating a connection for an existing agent leaves its mandate and autonomy unchanged.
- **AC-3** Same `Idempotency-Key` + same body → same `connection_id`, no second key; same key + different body →
  `409`.
- **AC-4** Bad `agent_id`, unknown `client`, unknown customer → `422`; agent credential → `403 OPERATOR_REQUIRED`;
  no credential → `401`.
- **AC-5** The new key places a lookup and a quote over REST as `agent:<agent_id>`; the audit shows that actor.
- **AC-6** `GET /v1/connections` lists newest first, revoked included; no key or digest in the body.
- **AC-7** `DELETE` revokes: next REST and MCP call with the key → `401`; audit `CONNECTION_REVOKED`; mandate kept.
- **AC-8** `last_used_at` is set on first use and not rewritten by a second call within the same minute.
- **AC-9** MCP at `/mcp` in the gateway app: `ping` answers non-`404`; `tools/list` and a lookup with the
  connection key run as that agent (audit actor `agent:<agent_id>`); two different keys in turn act as two
  different agents; no key → `401`.
- **AC-10** `mcp_url` equals public origin + `/mcp`; `null` when MCP is not served.
- **AC-11** The key appears in no audit entry, log record or response other than the `201` (scan test).
