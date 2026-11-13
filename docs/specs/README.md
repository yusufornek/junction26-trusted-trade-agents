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
| 06 | [Agent mandate](06-agent-mandate.md) | Per-agent authority, cumulative spend limit, unregistered agents | built |
| 07 | [Order timeline](07-order-timeline.md) | One call tells the whole story of an order | built |
| 09 | [Price-complete shortlist](09-price-complete-shortlist.md) | Every suitable candidate is priced before ranking; stable best offer | built |
| 08 | [Exceptions](08-exceptions.md) | Fulfilment plans (single / split / partial), policy preview, substitutes outside preferences | built |
| 10 | [Signed quotes](10-signed-quotes.md) | JWS-signed quotes and a public key set — trust protocol | draft |
| 11 | [Tamper-evident audit](11-tamper-evident-audit.md) | Hash-chained audit entries with a verify endpoint | draft |
| 12 | [Verifiable mandate](12-verifiable-mandate.md) | Buyer-issued, signed agent authority | draft |
| 13 | [Open-standard mapping](13-open-standard-mapping.md) | Peppol / UBL mapping and export | draft |

Status values: `draft` (written, waiting for team review) → `in progress` → `built` (all ACs pass) .
