# 00 — Core audit: challenge requirements vs. what is built

Checked on 2026-11-14 against the Junction challenge page (re-read that night) and the printed brief.
Living document: update the status column when a spec is built.

## Core — "your solution must enable"
| Requirement (challenge text) | Where it is met | Evidence | Status |
|---|---|---|---|
| **Lookup:** structured way for an external agent to search and retrieve what is available and at what price | MCP `search_products`, `find_offers`; REST `/v1/offers`, `/v1/products/{id}`, `/v1/lookups/{id}` | [01](01-lookup.md), live run 13.11 | met · exact pack counts [05](05-size-reconciliation.md), stable best price [09](09-price-complete-shortlist.md) |
| **Request, then order:** from inquiry to commitment at fixed prices | MCP `create_quote`, `place_order`, `get_order_status`; REST `/v1/quotes`, `/v1/orders` | [02](02-quote-order.md), live REST + MCP demo | met · incl. cancellation [04](04-order-cancel.md) |
| **Permission & human approval:** what the agent decides alone, what goes to a human | Policy (`/v1/policy`), approval inbox (`/v1/approvals`), audit (`/v1/audit`) | [03](03-permissions-approval.md), live policy change | met · per-agent mandates and cumulative limit [06](06-agent-mandate.md) |

## Example request from the brief
"1,000 litres of lactose-free milk by Monday … based on price and availability" — run end to end by Claude as an
outside MCP buyer agent: 1,000 × 1 L Valio Arki Eila, 1,010 €, in stock for Monday, confirmed within policy,
every step audited (`backend/demo/run_buyer_agent.sh`).

## Constraints stated in the brief
| Constraint | How we respect it |
|---|---|
| Mock today's webshop/backend, not a marketplace | One wholesaler gateway over the given mock API |
| Not a chatbot | Structured MCP / REST interface; people get a control console |
| Query the APIs only when needed | Catalog pool screened locally; 1 price + 1 availability call per lookup; counts in the audit log |
| Fixed prices | Quote locks the price; the order uses it unchanged |

## Judging criteria
| Criterion | Weight | Where we stand | Next lever |
|---|---|---|---|
| Working end-to-end demo | 30% | REST + MCP flows run live; console shows them | demo video as backup |
| Solving the user need | 20% | Right product, exact pack counts, stable cheapest suitable offer; sour milk screened out | preferences ([08](08-exceptions.md)) |
| Autonomy and controls | 20% | Editable policy, per-agent mandates, cumulative limit, approval inbox, cancellation, append-only audit, order timeline | signed mandate, approver roles |
| Robustness to exceptions | 15% | Warnings for partial stock, date shift, cut-off; fulfilment plans (single / split / partial) with policy preview; substitutes outside preferences need sign-off ([08](08-exceptions.md)) | recipe-aware substitution, stock-out after confirmation |
| Openness & path to production | 15% | MCP + OpenAPI contract, documented assumptions | signed quotes, Peppol mapping, verifiable mandate |

## Optional directions
| Direction | Status |
|---|---|
| Delivery time & sustainability comparison | reliability, CO2 and labels on every offer and in ranking |
| Substitution & exception handling | warnings, ranked alternatives, split / partial plans, marked substitutes ([08](08-exceptions.md)) |
| Traceability & explainability | audit trail, rule hits, fit scores, provenance · full story per order [07](07-order-timeline.md) |
