# Specs

Spec-driven development: every feature has a short spec here **before** it is built. A spec states the purpose,
the requirements (`R-n`) and acceptance criteria (`AC-n`, given / when / then). Code and tests follow the spec;
each AC names the test that proves it. After the MVP the team reviews every spec one by one and decides what to
change or extend.

| # | Spec | Scope | Status |
|---|---|---|---|
| 00 | [Core audit](00-core-audit.md) | Challenge core requirements vs. what is built | living document |
| 01 | [Lookup](01-lookup.md) | Core 1 — product, price, availability | built |
| 02 | [Quote → order](02-quote-order.md) | Core 2 — inquiry to fixed-price commitment | built |
| 03 | [Permissions & approval](03-permissions-approval.md) | Core 3 — what the agent decides alone, what a human signs | built |
| 04 | [Order cancellation](04-order-cancel.md) | Human or agent undoes a confirmed order before the cut-off | built |
| 05 | [Size reconciliation](05-size-reconciliation.md) | Exact pack counts: catalog / name size confirmed by the live price ratio | built |
| 06 | [Agent mandate](06-agent-mandate.md) | Per-agent authority, cumulative spend limit, ordering requires a mandate | built |
| 07 | [Order timeline](07-order-timeline.md) | One call tells the whole story of an order | built |
| 08 | [Exceptions](08-exceptions.md) | Fulfilment plans (single / split / partial), policy preview, substitutes outside preferences | built |
| 09 | [Price-complete shortlist](09-price-complete-shortlist.md) | Every suitable candidate is priced before ranking; stable best offer | built |
| 10 | [Signed quotes](10-signed-quotes.md) | JWS-signed quotes and a public key set — trust protocol | built |
| 11 | [Tamper-evident audit](11-tamper-evident-audit.md) | Hash-chained audit entries with a verify endpoint | built |
| 12 | [Verifiable mandate](12-verifiable-mandate.md) | Buyer-issued, signed agent authority (W3C VC as JWT) | built |
| 13 | [Open-standard mapping](13-open-standard-mapping.md) | Peppol / UBL mapping and export | built |
| 14 | [Demo reset](14-demo-reset.md) | Archive the local state and rebuild a clean database for the demo | built |
| 15 | [Evidence completeness](15-evidence-completeness.md) | Missing or unknown data never counts as proof; unknown codes are refused | built |
| 16 | [Standing constraints](16-standing-constraints.md) | Buyer-owned constraints per customer; every quote bound to a stored request | built |
| 17 | [Approval evidence](17-approval-evidence.md) | Approval applies to the evidence the human saw | built |
| 18 | [Lookup fallback](18-lookup-fallback.md) | Deterministic recovery from a too-narrow query; screening counts | built |
| 19 | [Authentication](19-authentication.md) | Credentials for agents and the operator; object ownership | built |
| 20 | [Total-cost ranking](20-total-cost-ranking.md) | Offers ranked by what the packs needed cost, overbuy shown | built |
| 21 | [Audit anchoring](21-audit-anchoring.md) | Head hash of the audit chain written to Polygon | built |
| 22 | [Shared foundation: one procurement state machine](22-shared-foundation.md) | Shared foundation: one procurement state machine | in progress |
| 23 | [Machine-readable recovery contract](23-recovery-contract.md) | Machine-readable recovery contract | in progress |
| 24 | [Per-agent rate limits](24-rate-limits.md) | Per-agent rate limits | in progress |
| 25 | [Supplier adapters and a second supplier](25-supplier-adapters.md) | Supplier adapters and a second supplier | in progress |
| 26 | [Deterministic developer sandbox](26-sandbox.md) | Deterministic developer sandbox | in progress |
| 27 | [OpenTelemetry traces and metrics](27-opentelemetry.md) | OpenTelemetry traces and metrics | in progress |
| 28 | [Buyer-side view](28-buyer-view.md) | Buyer-side view | in progress |
| 29 | [Downloadable transaction evidence](29-evidence-bundle.md) | Downloadable transaction evidence | in progress |
| 30 | [Exception journey: the agent waits, resumes and takes its receipt](30-exception-journey.md) | Exception journey: the agent waits, resumes and takes its receipt | in progress |
| 31 | [Allergen-safe substitution for a hospital kitchen](31-hospital-substitution.md) | Allergen-safe substitution for a hospital kitchen | in progress |
| 32 | [Recipe to order: ingredient baskets](32-basket-recipe.md) | Recipe to order: ingredient baskets | in progress |
| 33 | [Signed webhooks and approver notifications](33-webhooks.md) | Signed webhooks and approver notifications | in progress |
| 34 | [A2A service next to MCP](34-a2a-service.md) | A2A service next to MCP | in progress |
| 35 | [Recurring orders with drift detection](35-recurring-drift.md) | Recurring orders with drift detection | in progress |
| 36 | [Proactive supplier proposals](36-supplier-proposals.md) | Proactive supplier proposals | in progress |
| 37 | [MCP server card and discovery](37-mcp-server-card.md) | MCP server card and discovery | in progress |
| 38 | [Connect your agent](38-connect-kit.md) | Connect your agent | in progress |
| 39 | [Organiser answers](39-organiser-answers.md) | Whole units, unknown availability, not available, cut-off text, honour 429 | in progress |
| 40 | [Graduated autonomy](40-graduated-autonomy.md) | Approval first, autonomy earned from a measured track record | in progress |
| 41 | [Purchase memory](41-purchase-memory.md) | Remember purchase rhythms, suggest replenishment, turn patterns into recurring workflows over MCP | in progress |
| 42 | [Agent connections and MCP on the public address](42-agent-connections.md) | Operator issues, lists and revokes agent keys for the console's Connect page; MCP served in the gateway at `/mcp` | in progress |
| 43 | [Accounts, tenants and audiences](43-accounts-tenants-audiences.md) | Buyer businesses sign in, manage their team and bind their own agents; buyer / tenant / operator audiences on every connection type; strict tenant isolation | in progress |
| 44 | [Agent governance inside a buyer business](44-agent-governance.md) | Agent ownership, three policy layers with provenance, change requests for loosening, versions and simulation, agent register, pause, alerts, governance report | in progress |

Status values: `draft` (written, waiting for team review) → `in progress` → `built` (all ACs pass) .
