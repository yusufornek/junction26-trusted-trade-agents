# Connect an agent: backend contract (proposal)

The console page `/connect` (`frontend/src/pages/Connect.tsx`) lets an operator pick the tool an agent works in
(Claude Desktop, Claude Code, Cursor, VS Code, ChatGPT, another MCP client, or plain REST), create a key for that
agent, and copy the tool's own setup. The page is built and deployed. It needs the endpoints below from the
backend (owner: Gökalp). Until they exist the page says so and shows `<YOUR_AGENT_KEY>` in every snippet.

Frontend types: `ClientKind`, `Connection` and `NewConnection` in `frontend/src/api.ts`.
Fixtures with the same shapes: `frontend/src/mock.ts` (`npm run dev:mock`).

## Endpoints (operator only)

### `POST /v1/connections`

Headers: `Authorization: Bearer <operator token>`, `Idempotency-Key: <uuid>`.

```json
{ "agent_id": "central-kitchen-agent", "client": "claude_code", "customer_ids": ["1"], "label": "optional" }
```

- `agent_id`: `^[a-z0-9][a-z0-9-]{1,47}$` (the console validates the same pattern).
- `client`: `claude_desktop | claude_code | cursor | vscode | chatgpt | mcp_other | rest`. Informational: it is
  shown in the list and the audit entry, it does not change what the key can do.

`201` returns a `NewConnection`:

```json
{
  "connection_id": "con_5d1e7a2b",
  "agent_id": "central-kitchen-agent",
  "client": "claude_code",
  "key_prefix": "vak_3f9c",
  "created_at": "2026-11-14T18:02:11Z",
  "last_used_at": null,
  "revoked_at": null,
  "key": "vak_3f9c…",
  "mcp_url": "https://luotsi.online/mcp",
  "rest_base_url": "https://luotsi.online/v1",
  "mandate": { "customer_ids": ["1"], "max_auto_daily_total_eur": 1000, "autonomy": "approval_required" }
}
```

Rules:

1. The key is returned once. Store only a hash of it and `key_prefix` (its first 8 characters).
2. A new `agent_id` gets a mandate for `customer_ids` with `autonomy: approval_required`, as spec 40 requires for
   agents without a track record. An existing agent keeps its mandate and autonomy unchanged.
3. `mcp_url` is `null` when this process does not serve MCP; the console then steers people to REST.
4. Audit entry `CONNECTION_CREATED` by `human:operator` with `agent_id`, `client`, `key_prefix`. Never the key.
5. `422` for a bad `agent_id`, `client` or unknown customer.

### `GET /v1/connections`

`200` with `Connection[]`, newest first, revoked ones included (`revoked_at` set). The console hides revoked keys.

### `DELETE /v1/connections/{connection_id}`

`200` with the `Connection`, `revoked_at` set. The key is refused (`401`) from its next call on. The agent's mandate,
autonomy level and history stay. Audit entry `CONNECTION_REVOKED`.

`Connection` = `{ connection_id, agent_id, client, key_prefix, created_at, last_used_at | null, revoked_at | null }`.

## Authentication

A key issued here authenticates exactly like an `AGENT_KEYS` entry: `resolve_principal` maps it to its `agent_id`.
Update `last_used_at` on authenticated calls (once a minute per key is enough).

## MCP on the public address

Claude Desktop, Claude Code, Cursor and VS Code connect to `https://<host>/mcp` with
`Authorization: Bearer <agent key>`. Serve MCP (streamable HTTP) inside the gateway process, against the same
database, with the same keys. Caddy already forwards `/mcp*`. `docs/DEPLOY.md` rules out a second process on the
same database. The console probes with a JSON-RPC `ping` to `/mcp`; any answer other than `404` counts as served.

## Not covered: claude.ai on the web and ChatGPT

Both add remote MCP servers only through an OAuth sign-in window. The console marks them "needs sign-in" and points
people to the clients above. Supporting them means OAuth 2.1 with dynamic client registration on the gateway, a
separate decision.

## How the console checks a connection

Step 3 polls `GET /v1/audit` and shows the first entry with actor `agent:<agent_id>` after the key was created.
No new endpoint is needed for this.
