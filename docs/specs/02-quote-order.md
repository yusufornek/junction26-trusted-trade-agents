# 02 — Quote → order (Core 2)

Status: built · Challenge text: "a defined interaction for moving from inquiry to commitment, based on fixed
prices rather than a negotiated quote."

## Purpose
The mock has no order endpoint. The gateway provides the commitment layer: a fixed-price quote, then an order.

## Requirements
- **R-1 Interfaces:** MCP `create_quote`, `place_order`, `get_order_status`; REST `POST /v1/quotes`,
  `GET /v1/quotes/{id}`, `POST /v1/orders` (header `Idempotency-Key`, optional `X-Agent-Id`), `GET /v1/orders`,
  `GET /v1/orders/{id}`.
- **R-2 Quote:** lines `{product_id, quantity_sales_units}` (1–10, unique products), `deliver_by`, optional
  `need_ref` (lookup id). Price and availability are re-checked live for those lines only; unit prices are locked.
- **R-3 Validity:** `expires_at` = earliest of price `validTo`, a future cut-off, now + 30 min.
- **R-4 Fixed price:** the order total equals the quote total; an expired quote is refused (`EXPIRED_QUOTE`)
  and the agent asks for a new quote.
- **R-5 Trusted evidence:** fit scores and hard constraints come from the stored lookup via `need_ref`; a client
  cannot inject them. A line without a verified fit carries `NO_FIT_SCORE`.
- **R-6 Order:** `place_order(quote_id, idempotency_key)` re-checks availability, runs the policy and returns
  `CONFIRMED`, `PENDING_APPROVAL` or `REJECTED` with every rule hit. One order per quote.
- **R-7 Idempotency:** the same key returns the same order; the same key with another quote or agent is a conflict.
- **R-8 States:** quote `OPEN → ACCEPTED | EXPIRED`; order `PENDING_APPROVAL → CONFIRMED | REJECTED | EXPIRED`,
  `CONFIRMED → CANCELLED` (see [04](04-order-cancel.md)).
- **R-9 Reservations:** Stock-model quantities of pending and confirmed orders are reserved locally (mock stock
  never decreases) and released on reject / expire / cancel.
- **R-10 Audit:** quote, order, replay and failures are audited with actor, inputs and live-call counts.

## Acceptance criteria → tests
| AC | Given / when / then | Test |
|---|---|---|
| AC-1 | Quote re-checks live and carries the lookup's fit | `test_quote_live_recheck_and_fit_carry` |
| AC-2 | Expiry is the earliest bound | `test_quote_expiry_earliest_bound` |
| AC-3 | Expired quote is refused and audited | `test_expired_quote_persists_and_audits` |
| AC-4 | Retries (also concurrent) return one order at the fixed price | `test_idempotency_concurrent_retries_and_fixed_price` |
| AC-5 | Multi-line quote sums and reserves each line | `test_multiline_quote_sums_and_reserves_each_line` |
| AC-6 | Reservations are held and released; two orders cannot both take the last stock | `test_stock_reservations_and_release`, `test_pending_expiry_releases_stock`, `test_concurrent_stock_orders_do_not_both_auto_confirm` |
| AC-7 | Stock change between quote and order changes the policy outcome | `test_stock_recheck_changes_policy_after_quote` |
| AC-8 | Unknown product / bad price cannot be quoted or is rejected | `test_unknown_upstream_rejected_and_bad_price_cannot_quote`, `test_quote_without_known_price_fails_audited` |
| AC-9 | Full flow 1,000 L → quote → order → approval → confirmed over REST and MCP | `test_full_rest_flow_1000_l`, `test_mcp_tools_use_shared_core` |

Verified live 2026-11-13 over REST and with Claude as MCP buyer agent (`backend/demo/run_buyer_agent.sh`).

## Open points for the review
- Signed quotes (JWS) so an agent can verify the offer came from the gateway.
- Field mapping to UBL / Peppol BIS Ordering for the path to production.
- Meaning of a passed `closingTime` (mentor question); today it is a warning that needs a human.
