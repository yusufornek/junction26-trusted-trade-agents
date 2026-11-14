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
| 10 | [Signed quotes](10-signed-quotes.md) | JWS-signed quotes and a public key set — trust protocol | in progress |
| 11 | [Tamper-evident audit](11-tamper-evident-audit.md) | Hash-chained audit entries with a verify endpoint | in progress |
| 12 | [Verifiable mandate](12-verifiable-mandate.md) | Buyer-issued, signed agent authority (W3C VC as JWT) | in progress |
| 13 | [Open-standard mapping](13-open-standard-mapping.md) | Peppol / UBL mapping and export | in progress |
| 14 | [Demo reset](14-demo-reset.md) | Archive the local state and rebuild a clean database for the demo | built |
| 15 | [Evidence completeness](15-evidence-completeness.md) | Missing or unknown data never counts as proof; unknown codes are refused | built |
| 16 | [Standing constraints](16-standing-constraints.md) | Buyer-owned constraints per customer; every quote bound to a stored request | built |
| 17 | [Approval evidence](17-approval-evidence.md) | Approval applies to the evidence the human saw | built |
| 18 | [Lookup fallback](18-lookup-fallback.md) | Deterministic recovery from a too-narrow query; screening counts | built |
| 19 | [Authentication](19-authentication.md) | Credentials for agents and the operator; object ownership | in progress |
| 20 | [Total-cost ranking](20-total-cost-ranking.md) | Offers ranked by what the packs needed cost, overbuy shown | built |
| 21 | [Audit anchoring](21-audit-anchoring.md) | Head hash of the audit chain written to Polygon | in progress |

Status values: `draft` (written, waiting for team review) → `in progress` → `built` (all ACs pass) .
