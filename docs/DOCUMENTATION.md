# Luotsi documentation

Luotsi is the gateway through which external AI purchasing agents work with Valio Aimo: look up products, prices and availability, commit at fixed prices, and order within their authority or hand the decision to a human. Live at https://luotsi.online. This document describes every capability, the human sign-off model, the trust model, the interfaces and the choices behind them. Each section links the spec (`docs/specs/NN-*.md`) that defines its requirements (`R-n`) and acceptance criteria (`AC-n`); every acceptance criterion has a test named after it in `backend/tests/`.

Related documents: [README](../README.md) · [Specs index](specs/README.md) · [Connect an agent](CONNECT-API.md) · [Scenarios](SCENARIOS.md) · [Deploy](DEPLOY.md) · [Challenge](CHALLENGE.md)

---

## 1. Architecture

```
 AI agents (claude.ai, ChatGPT, Claude Desktop/Code, A2A agents, scripts)
        │  MCP /mcp · A2A /a2a · REST /v1 · OAuth 2.1
        ▼
 ┌───────────────────────── Luotsi gateway (FastAPI) ─────────────────────────┐
 │ identity & mandates · policy engine · lookup & ranking · quotes & orders    │
 │ exceptions & substitution · anomalies · connection trust · notifications    │
 │ signing (Ed25519/JWS) · hash-chained audit · Polygon anchoring · outbox     │
 └──────────────┬───────────────────────────────┬──────────────────────────────┘
                │ shortlist only, rate-limited  │
                ▼                               ▼
     Valio Aimo mock APIs                 SQLite (orders, quotes, audit)
     (price, availability)                + synthetic catalog, reliability, CO₂ files
        ▲
 Console (React) for operators, buyer admins and approvers: approvals, orders, policy,
 governance, connections, trust, audit
```

- Backend: Python, FastAPI, pydantic models (OpenAPI generated from them), SQLite, MCP Python SDK, a2a-sdk.
- Console: Vite, React, TypeScript, Tailwind; talks to the backend only through `contracts/openapi.yaml`.
- Deploy: Docker Compose (gateway, console behind Caddy with TLS, Jaeger), GitHub Actions to a VPS.

## 2. The core flow

`Request → Lookup → Fixed-price quote → Order → Agent decides, or a human signs off`

1. **Request.** The agent sends a structured need: customer, intent, quantity (`L`, `KG` or sales units), deliver-by date, optional preferences and hard constraints. Plain language can be interpreted first (section 5.9).
2. **Lookup** ([01](specs/01-lookup.md), [09](specs/09-price-complete-shortlist.md), [18](specs/18-lookup-fallback.md), [20](specs/20-total-cost-ranking.md)). Full-text catalog screening builds a candidate pool; only a shortlist (policy `live_max`, at most 40) is priced and availability-checked live. Pack sizes are reconciled ([05](specs/05-size-reconciliation.md)) so "1,000 L" becomes the right number of sales units. Each offer carries price, total cost, `arrives_in_time`, cut-off, delivery reliability, CO₂, labels and the ranking contribution of each factor. Plans (single, split, partial) are built from the offers.
3. **Quote** ([02](specs/02-quote-order.md), [10](specs/10-signed-quotes.md)). Re-checks the chosen plan live and locks a fixed price in an Ed25519-signed, time-limited quote.
4. **Order.** Accepts the quote unchanged, re-checks stock, evaluates policy and returns `CONFIRMED`, `PENDING_APPROVAL` or `REJECTED` with every rule hit. Idempotency keys make retries safe. Cancellation before cut-off is a human decision ([04](specs/04-order-cancel.md)).
5. **Approval** ([03](specs/03-permissions-approval.md), [17](specs/17-approval-evidence.md)). Pending orders appear in the approval inbox with their evidence; humans approve or reject in two steps.

Example from the brief, as run live: *1,000 L of lactose-free milk by Monday* → Valio Eila lactose-free 1 L ranked first, €1,010, arrives in time → order `CONFIRMED` without approval. *3,000 L* → `PENDING_APPROVAL`, "net total €3,030 exceeds €1,500".

## 3. Human sign-off model

| The agent decides alone when… | A human must sign off when… | Luotsi refuses when… |
|---|---|---|
| The order is inside the agent's mandate: its customers and suppliers, under the per-order limit (€1,500) and the rolling 24-hour autonomous limit (€5,000) | A limit is exceeded | A hard constraint fails: allergen, missing allergen declaration, customer standing rule ([16](specs/16-standing-constraints.md)) |
| The quote is fresh, fixed-price and unchanged | A warning needs review: partial stock, date shift, cut-off passed or unknown, unclear pack size, unit mismatch, low or missing fit score, outside preferences | The product is sales-blocked, unknown or not available |
| The connection is a verified client confirmed by its owner | Any substitute is proposed | The agent is outside its mandate, paused, or its credential or mandate is revoked |
| The agent's autonomy is `autonomous_within_limits` | An anomaly is found, or the agent's autonomy is `approval_required` | |

- **Approvers and audiences** ([43](specs/43-accounts-tenants-audiences.md)): buyer admins, buyer approvers and viewers per tenant; Valio operators platform-wide. Approvals work in the console, over REST, and over MCP/A2A for human assistants, always with a two-step confirmation bound to the approver and the evidence revision.
- **Changed evidence:** if price or stock changes before the decision, the order stays pending with `EVIDENCE_CHANGED` and must be reviewed again.
- **Earned autonomy** ([40](specs/40-graduated-autonomy.md)): new agents start at "approval first"; autonomy is granted per agent from its track record and withdrawn or paused at any time.
- **Governance** ([44](specs/44-agent-governance.md), [45](specs/45-guardrail-profiles-and-anomalies.md)): layered policy (platform, tenant, agent), guardrail profiles, change history with the human responsible, pause/resume (`423 AGENT_PAUSED`).

## 4. Identity, mandates and trust

- **Authentication** ([19](specs/19-authentication.md)): agent keys (`vak_…`) per connection, OAuth 2.1 for assistants, scrypt-hashed human logins with HttpOnly cookies and CSRF protection.
- **Agent mandates** ([06](specs/06-agent-mandate.md)): customers, suppliers, limits, autonomy, recurring permission.
- **Verifiable mandates** ([12](specs/12-verifiable-mandate.md)): a buyer-signed W3C Verifiable Credential 2.0 (VC-JWT, EdDSA) states customer, amount, autonomy and supplier scope; revocation by signed request.
- **Agent connections** ([42](specs/42-agent-connections.md), [38](specs/38-connect-kit.md)): the Connect page issues keys or OAuth set-up for Claude Desktop, Claude Code, claude.ai, ChatGPT, Cursor, VS Code, A2A and REST, with a live first-call check and connection health.
- **Connection trust lifecycle** ([62](specs/62-connection-trust-lifecycle.md)): clients are verified (pre-registered, or OAuth client ID metadata documents from known publishers such as claude.ai) or unverified; the owner confirms each new connection ("Yes, this is the same agent"); the same credential used from a second runtime context locks mutations until confirmed; re-consent after expiry.
- **Trading contract and driving test** ([63](specs/63-connection-contract-driving-test.md)): every connection acknowledges a versioned trading contract (units, price basis, cut-offs) by digest; new agents pass a driving test (sales-unit trap, expired quote, dropped response, invented field, waiting for approval) before their order scope opens.
- **Handoff and delegated authority** ([64](specs/64-handoff-delegated-authority.md)): one agent of a business hands a purchase intent to another with bounded, revocable delegation.
- **Purchase intents** ([47](specs/47-purchase-intent-integrity.md)): a need saved as an intent keeps its identity; resubmitting it with different content returns `INTENT_AMENDMENT_REQUIRED` and the difference.

## 5. Capabilities beyond the core

1. **Exceptions** ([08](specs/08-exceptions.md), [30](specs/30-exception-journey.md)): partial availability, date shifts, cut-off passed, price changes, each with a machine-readable code and a next step.
2. **Substitution with trade-offs:** ranked good/better/best substitutes inside the assortment with price difference, pack, allergens, delivery date and a one-line explanation; substitutes always need approval.
3. **Allergen- and nutrition-safe substitution** ([31](specs/31-hospital-substitution.md)): for hospital and school kitchens; products without declarations are excluded and shown as excluded; clinical orders require buyer review.
4. **Recipe baskets** ([32](specs/32-basket-recipe.md)): need → recipe → order; servings scaling, on-hand deduction (needed / on hand / to buy), shared-product aggregation.
5. **Brief scenarios** ([46](specs/46-brief-scenarios.md)): last-minute event (late lines flagged with earliest date and in-time alternatives), stock-out watch (`STOCKOUT_PREDICTED` with substitute and trade-off), proactive selling.
6. **Supplier proposals** ([36](specs/36-supplier-proposals.md)): Valio's own agent proposes new or never-bought products to the buyer's agent, with consent, quotas and normal purchasing controls.
7. **Purchase memory** ([41](specs/41-purchase-memory.md)): learns cadence ("you buy this every month") and suggests replenishment; preparing a suggestion only sets up a lookup.
8. **Recurring orders and drift** ([35](specs/35-recurring-drift.md)): recurring orders from an approved order; every run checks price, quantity and pack drift against the baseline before ordering; pause and resume.
9. **Plain-language interpretation** ([60](specs/60-agent-surface.md) R-9): a sentence becomes a structured draft that the agent must confirm before any lookup or order.
10. **Anomalies** ([45](specs/45-guardrail-profiles-and-anomalies.md)): quantity, price, order speed and new category checks against the customer's history (median ± k·MAD), with expected band and fixes; an optional Timestral time-series model can supply the band.
11. **Notifications** ([61](specs/61-connection-notifications.md), [33](specs/33-webhooks.md)): in-band notifications for agents, MCP resource subscriptions, signed Standard Webhooks with retries and redelivery.
12. **Rate limits** ([24](specs/24-rate-limits.md)): per-agent call budgets; at most 5 requests per second to the Valio API; `429` with `Retry-After`.
13. **Recovery contract** ([23](specs/23-recovery-contract.md)): every error is RFC 9457 problem+json with `code`, `retryable` and `next_action` (for example `FIX_REQUEST`, `NEW_QUOTE`, `CONTACT_ADMIN`).
14. **Second supplier** ([25](specs/25-supplier-adapters.md)): a synthetic supplier (Nordfood) behind the same adapter interface shows the core is not tied to one API.
15. **Sandbox** ([26](specs/26-sandbox.md), [14](specs/14-demo-reset.md)): reproducible scenarios with real rules and resettable fixtures.

## 6. Explainability and audit

- **Order timeline** ([07](specs/07-order-timeline.md)): lookup → quote → approval → order in plain words, with the rules that applied.
- **Signed receipts and evidence bundles** ([15](specs/15-evidence-completeness.md), [29](specs/29-evidence-bundle.md)): a downloadable bundle with the order, quote, decision evidence, audit slice, UBL documents and anchor; verifiable offline.
- **Tamper-evident audit** ([11](specs/11-tamper-evident-audit.md), [00](specs/00-core-audit.md)): every action appends to a hash chain with a signed head; `/v1/audit/verify` recomputes the whole chain.
- **Public anchoring** ([21](specs/21-audit-anchoring.md)): the chain head hash is written to Polygon PoS; only hashes leave the system, never commercial data. The console's Trust page links each anchor on polygonscan.
- **Buyer view** ([28](specs/28-buyer-view.md)): the buyer sees their own agents' orders, approvals and evidence.

## 7. Interfaces

| Interface | Address | Notes |
|---|---|---|
| REST | `/v1/...` (229 paths) | OpenAPI 3.1 in `contracts/openapi.yaml`; docs at `/docs` |
| MCP | `/mcp` (buyer agents), `/mcp/tenant`, `/mcp/operator` (human assistants) | Streamable HTTP; 100+ tools, `luotsi://` resources with subscriptions, 6 prompts (for example `order_for_need`, `event_tomorrow`), elicitation; server card ([37](specs/37-mcp-server-card.md)) |
| A2A | `/a2a` and audience addresses | A2A 1.0 tasks, streaming, push; signed agent cards ([34](specs/34-a2a-service.md)) |
| OAuth 2.1 | `/.well-known/oauth-authorization-server`, `/oauth/*` | PKCE S256, dynamic client registration, client ID metadata documents, resource indicators, refresh rotation |
| Documents | UBL 2.1 | Order and order response, validated against the OASIS schema |
| Events | Standard Webhooks v1a | Signed deliveries |

Open-standard mapping: [13](specs/13-open-standard-mapping.md). Shared foundation: [22](specs/22-shared-foundation.md). Agent surface: [60](specs/60-agent-surface.md).

## 8. Where AI is used, and where it is not

| Component | Model | Does | Cannot |
|---|---|---|---|
| Fit score | OpenAI Decisions API | Probability that a candidate is the kind of product the buyer asked for; catalog text is marked as data, not instructions | Approve anything; below 0.80 the order goes to a human; missing score adds `NO_FIT_SCORE` and sends it to a human |
| Request interpretation | OpenAI Responses API | Turns a sentence into a structured draft | Look up or order before the agent confirms; it asks instead of guessing |
| Anomaly band (optional) | Timestral-28M, local CPU | Forecast band from order history | Decide; outside the band still goes to a human |

Hard rules (allergens, declarations, limits, mandates) are enforced in code and never asked of a model. AI suggests and interprets; deterministic code decides.

## 9. Data and mock APIs

- Synthetic catalog, delivery reliability and CO₂ files from the challenge are built into `var/app.db` (`python -m app.catalog build`).
- The Valio Aimo mock APIs are called only for the shortlist, through one rate-limited adapter.
- There is no upstream order endpoint, so orders are recorded by the gateway and marked `simulated`; a `supplier_backed` mode is ready for a real endpoint.
- No real, live or confidential data is used.

## 10. Console

Operator and buyer console at https://luotsi.online: Dashboard, Approvals (two-step), Orders (timeline, receipt, UBL, cancel), Agents and Governance (policy layers, autonomy, pause/resume, change history), Policy, Connect (client set-up, connections, health, trust confirmation), Trust (chain verification, Polygon anchors), Audit, Changes, Repeat orders.

## 11. Security

- Secrets only in environment files outside git; keys stored hashed; credentials never appear in responses, audit or logs (tested).
- Tokens are bound to their resource (audience) and rotated on refresh; consent pages show the client's verification status.
- Tenant isolation on every call; humans and agents have separate audiences.
- Catalog and supplier text is treated as data in model prompts.

## 12. Operations and testing

- `docker compose` runs the gateway, the console (Caddy, TLS) and Jaeger for OpenTelemetry traces ([27](specs/27-opentelemetry.md)).
- Every push is built locally, health-checked in Docker, deployed by GitHub Actions and checked live.
- About 1,500 tests (`pytest tests -q`), no network required; each acceptance criterion has a test named after it.

## 13. Known limits

- Orders are simulated; the mock has no order endpoint.
- Supplier certification and origin claims are recorded and signed as received, not independently verified.
- CO₂ is a category average, not a range.
- UBL party identifiers are placeholders, not Peppol-registered.
- E-mail delivery for invitations and password resets is not set up; links are returned by the API.
