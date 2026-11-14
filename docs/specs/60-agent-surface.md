# 60 — Agent surface: full MCP feature set and rich agent cards

Status: implemented · Decided on 2026-11-14 with Gökalp: the product is an AI gateway, not a dashboard; agent connection
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
  several servers can tell whose answer it holds (tool names stay as they are; commits need gateway-issued refs, 48 R-7).
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

## Implementation and decisions taken
- `backend/app/agent_surface*.py` owns this capability. Existing MCP tool names and input shapes remain valid.
  Additional tools are generated from effective FastAPI route contexts, including included-router prefixes and
  dependencies. Reserved Python argument names use a trailing underscore (for example `from_`) and map back to the
  original REST parameter. Request bodies are under `body` for generated tools. Local `$defs` retain reusable schemas.
- The REST adapter uses FastAPI's dependency solver with the already verified principal. It reruns scope, target,
  role and input guards and calls the original endpoint, without a network loopback or a second purchasing engine.
  Downloads use a structured base64 envelope; the existing evidence tool remains the preferred manifest reader.
- Resources, URI templates, prompts, annotations, structured results, elicitation and subscriptions use the installed
  MCP SDK. Both SDK `subscriptions/listen` and legacy `resources/subscribe` handlers are registered. An outbox consumer
  supplies the spec 61 change-notification seam. Notification fan-out is isolated by connection and rechecks access.
  SDK JSON responses remain the ordinary HTTP path; subscription streams and legacy duplex elicitation use SDK SSE.
  Stateful sessions bind their identity and re-resolve current credentials on each HTTP message.
- The six prompts include the caller's current limits and constraints. Human decision previews retain the existing
  five-minute, single-use, evidence-bound confirmation token. Capable legacy clients receive SDK form elicitation;
  modern clients use SDK elicitation requests inside `input_required`, including manual request-state round trips.
- Interpretation is non-authoritative. It uses the fit provider's configured key, HTTP client and model (currently
  `gpt-6-luna`) through structured Responses output; the Decisions fit API remains unchanged. A sole permitted
  customer is bound from authenticated scope. Missing or invalid fields are returned for correction. Need and basket
  drafts expire after five minutes, bind their owner/tenant, and only confirmation invokes the ordinary lookup.
  No-model requests also receive a draft reference, so a filled schema can continue through the same confirmation.
- Cards and setup configurations use `GATEWAY_PUBLIC_URL`, with the fixed development origin
  `http://localhost:8000` when unset. Request Host is never a source for signed card/configuration endpoints. Existing
  local OAuth resource validation still uses the request origin when no public origin is configured. No new variables
  or dependencies are needed. The new client-setup endpoint requires authentication; the existing Connect catalog
  remains public for onboarding.
- A2A cards use the protocol's JWS signatures; the draft MCP card stores its detached JWS in the existing gateway
  extension. The Python reference buyer verifies the card against its configured origin and trusted-origin JWKS before
  opening the advertised endpoint. Authenticated cards (Bearer or session cookie) and MCP discovery are private and
  never shared-cacheable; public cards contain capability schemas without tenant identities.
- Buyer, tenant and operator discovery uses the same registry, with audience/tenant/policy-version metadata and scope
  filtering. A human token can use the shared `/mcp` URL for its own audience; audience-specific URLs remain supported.
  Connection health stores tool names, timestamps and error codes only. Probes perform an ordinary scoped context or
  order read, without supplier calls or purchases; scope reflects the current account/mandate intersection.

## Acceptance mapping
All eleven focused tests are in `backend/tests/test_spec60_agent_surface.py`.

| AC | Test |
|---|---|
| AC-1 | `test_AC_1_generated_rest_parity_and_real_reads` |
| AC-2 | `test_AC_2_all_tools_describe_annotations_and_structured_schemas` |
| AC-3 | `test_AC_3_sdk_resource_subscription_receives_order_approval` |
| AC-4 | `test_AC_4_six_sdk_prompts_render_current_buyer_limit` |
| AC-5 | `test_AC_5_sdk_elicitation_and_input_required_tier_substitute_and_decision` |
| AC-6 | `test_AC_6_a2a_schema_examples_and_cards_match_live_sdk` |
| AC-7 | `test_AC_7_client_setup_uses_only_canonical_live_origin` |
| AC-8 | `test_AC_8_connection_health_harmless_probe_and_unchanged_demo` |
| AC-9 | `test_AC_9_text_a2a_confirmation_missing_fields_and_no_model` |
| AC-10 | `test_AC_10_reference_verifies_signed_cards_rejects_tampering_and_result_issuer` |
| AC-11 | `test_AC_11_same_url_private_discovery_varies_by_audience_tenant_and_scope` |

Run from `backend/` with the supplied Python environment:
`/Users/daption-ciray/Desktop/junction/.claude/worktrees/selam-junction-challenge-44cf24/backend/.venv/bin/python -m pytest tests/test_spec60_agent_surface.py -q`.

Generated `static/tools.json`, `static/developers.html` and `contracts/openapi.yaml` are deliberately left for merge
regeneration. No live state, frontend files, secrets, network services or commits are part of this change.

## Validation
- Final acceptance/reference run: **22 passed, 1 failed** in 48.95 s. All **11 spec 60 acceptance tests passed**;
  the other 11 passing checks exercise the existing reference buyer, receipt verification, approval recovery,
  published walkthrough and malformed-mandate refusal. The remaining failure is the generated developer-page
  equality assertion; its static artifacts must be regenerated at merge as required by the brief.
- Focused compatibility run: **192 passed, 6 failed** in 513.24 s, covering this acceptance file and the existing
  `test_api_mcp.py`, `test_a2a_service.py`, `test_mcp_server_card.py`, `test_spec42_agent_connections.py`,
  `test_spec43_oauth_audiences.py`, `test_spec43_oauth_operator.py` and `test_authentication.py` files.
  The six remaining assertions encode the pre-spec-60 surface: three exact old MCP tool sets (including stdio),
  the old REST route allowlist, exactly six A2A skills, and identical public/authenticated MCP cards without tools.
  These inventory assertions need reconciliation with R-1/R-6/R-8/R-12 at merge. Existing test files were not edited.
- The targeted OAuth/onboarding and human-confirmation check passed **11 tests** after verifying exact resource
  binding on buyer, tenant and operator tokens. The final acceptance run also confirms the 464306 / EUR 1,010 /
  CONFIRMED demo path. No full suite, live API calls, deployment, catalog rebuild or artifact regeneration was run.
