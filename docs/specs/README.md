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
| 05 | [Size reconciliation](05-size-reconciliation.md) | Exact pack counts: catalog / name size confirmed by the live price ratio | draft |
| 06 | [Agent mandate](06-agent-mandate.md) | Per-agent authority, cumulative spend limit, unregistered agents | draft |
| 07 | [Order timeline](07-order-timeline.md) | One call tells the whole story of an order | draft |
| 08 | [Exceptions](08-exceptions.md) | Fulfilment plans (single / split / partial), policy preview, substitutes outside preferences | draft |

Status values: `draft` (written, waiting for team review) → `in progress` → `built` (all ACs pass) .
