# 60 — Agent surface: full MCP feature set and rich agent cards

Status: in progress · Decided on 2026-11-14 with Gökalp: the product is an AI gateway, not a dashboard; agent connection
is the focus. Builds on specs 22, 34, 37, 38, 42 and 43.

## Purpose
An agent that connects to Luotsi finds everything a human could do in the console as agent-native capabilities —
tools, readable resources, ready workflows, human questions — described well enough that the agent uses them correctly
on the first try, over MCP and A2A alike.

## Requirements
- **R-1 Tool parity:** every buyer-scoped REST capability has an MCP tool in the buyer audience (and tenant/operator
  tools in their audiences): including product detail, lookup detail, plan comparison, quote read, order list and
  history, timeline, audit query, cancellation, standing constraints read, spend and remaining authority, approvals
  status, webhook subscriptions, proposal preferences, memory preferences. A generated parity test fails when a REST
  capability has no tool.
- **R-2 Tool quality:** every tool carries MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`,
  `openWorldHint`), an output schema (`structuredContent`), a title, and a description that states when to use it, the
  next tool to call, and the recovery codes it can return.
- **R-3 Resources:** readable MCP resources with URIs and templates: `luotsi://agent/context`,
  `luotsi://policy/effective`, `luotsi://orders/{id}`, `luotsi://orders/{id}/timeline`, `luotsi://orders/{id}/evidence`,
  `luotsi://catalog/products/{id}`, `luotsi://approvals/pending`, `luotsi://inbox` (proposals, stock-out alerts,
  suggestions). Clients can subscribe; the gateway sends `notifications/resources/updated` when they change (spec 61).
- **R-4 Prompts:** MCP prompts for the common jobs, each a short guided workflow that names the tools in order:
  `order_for_need`, `event_tomorrow`, `replace_out_of_stock`, `weekly_replenishment`, `review_my_spend`,
  `explain_an_order`. Arguments are typed; the prompt text uses the buyer's own policy (limits, constraints).
- **R-5 Human questions (elicitation):** when a step needs the human behind the agent — choosing between good/better/best
  tiers, confirming a substitute, the two-step confirmation of a human decision — the MCP server uses elicitation with a
  typed schema where the client supports it, and falls back to the existing `input_required` / confirmation-token flow
  where it does not.
- **R-6 Agent cards:** the A2A cards (buyer, tenant, operator) list every skill with input/output schemas, at least one
  example per skill, security schemes per audience (bearer key, OAuth with exact resource), supported extensions (signed
  receipts, verifiable mandate, Standard Webhooks push) and the documentation links. The MCP server card lists tools,
  resources, prompts and capabilities from the same source, so cards and server never drift (test).
- **R-7 One-click client setup:** `GET /v1/connect/clients/{client}` returns ready configuration for Claude Desktop,
  Claude Code, ChatGPT, Cursor, VS Code and a generic MCP client (deep link or JSON snippet with the exact URL, auth
  method and audience), used by the Connect page and by agents themselves.
- **R-8 Connection health:** per connection: last used, calls in the last 24 h by tool, error codes seen, current scope;
  `POST /v1/connections/{id}/test` runs a harmless read through the connection and reports what the agent can do.

- **R-9 Plain-language requests:** an A2A message with a text part (and an MCP tool `interpret_request {text}`) is
  turned into a structured need or basket by the configured language model (same model settings as the fit score),
  using the caller's customers, units and standing constraints as context. The gateway answers with the interpreted
  request and asks the agent to confirm it (`INPUT_REQUIRED` with the structured draft over A2A; elicitation or a
  `confirm_interpretation` step over MCP); nothing is looked up or ordered before the confirmation. Missing fields are
  asked for, never guessed. The model only interprets; prices, rules and approvals stay deterministic. Without a model
  configured, a text-only request answers `INPUT_REQUIRED` with the structured schema to fill.

- **R-10 Signed cards:** agent cards and the MCP server card are JWS-signed with the gateway key (JWKS) and state the
  canonical origin; setup configurations (R-7) contain only that origin.
- **R-11 Issuer on every result:** every tool result carries `_meta.issuer` with the canonical origin, so an agent with
  several servers can tell whose answer it holds (tool names stay as they are).
- **R-12 Private discovery:** authenticated discovery (`tools/list`, resources, prompts, authenticated cards) is computed
  per audience, tenant and policy version and served `Cache-Control: private, no-store`; the public card holds no
  tenant identifiers.

## Acceptance criteria
- **AC-1** The parity test lists every buyer REST capability and finds an MCP tool for each.
- **AC-2** Every tool has annotations and an output schema; `cancel_order` is `destructiveHint: true`, read tools
  `readOnlyHint: true`.
- **AC-3** An MCP client reads `luotsi://orders/{id}` and `luotsi://approvals/pending`, subscribes to the order, and
  receives `notifications/resources/updated` when the order is approved.
- **AC-4** `prompts/list` returns the six prompts; `order_for_need` renders with the buyer's limit in its text.
- **AC-5** With an elicitation-capable test client, choosing a tier and confirming a substitute happen through
  elicitation; with a client without it, the same flow completes through `input_required`.
- **AC-6** The buyer agent card validates against the A2A 1.0 schema, every skill has an example, and the MCP server card
  and live `tools/list` match.
- **AC-7** `GET /v1/connect/clients/claude-desktop` and `/chatgpt` return working configuration for the live URL.
- **AC-8** A connection test reports scope and a successful read; the demo path (464306, 1,010 €, CONFIRMED) is unchanged.
- **AC-9** "Need 1000 litres of lactose-free milk by Monday" sent as A2A text yields a structured draft (customer,
  intent, 1000 L, the Monday date) awaiting confirmation; confirming continues to offers; a request missing the quantity
  asks for it; with no model configured the schema is returned (fake model in tests).
- **AC-10** A copied card with a changed endpoint fails verification in the reference client; every tool result carries
  the issuer.
- **AC-11** Buyer and operator tokens against one URL get different tool lists, and the response headers forbid shared
  caching.
