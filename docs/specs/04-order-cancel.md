# 04 — Order cancellation

Status: built (81 tests, verified live 2026-11-13) · Source: [issue #1](https://github.com/yusufornek/junction26-trusted-trade-agents/issues/1) · Owner: backend

## Purpose
A human can approve or reject an order before the commitment. Cancellation is the step after it: a person (or the
agent that placed the order) can undo a confirmed order while the logistics cut-off is still open. It answers
"what if the agent gets it wrong?" and makes the cut-off visible.

## Requirements
- **R-1 REST:** `POST /v1/orders/{order_id}/cancel` with body `{"cancelled_by": "<human name>", "reason": "<text>"}`
  (`cancelled_by` required, non-blank; `reason` optional, default `""`). Returns the `Order`.
- **R-2 MCP:** tool `cancel_order(order_id, reason)`. The actor is the agent identity the server was launched with
  (`--agent-id`). An agent can cancel only orders it placed (`order.agent_id` equals its identity).
- **R-3 Allowed state:** only `CONFIRMED` orders can be cancelled.
- **R-4 Cut-off:** cancellation is allowed while every known `closing_time` of the order's lines is in the future.
  Lines without a `closing_time` do not block.
- **R-5 Effects:** status becomes `CANCELLED`; local stock reservations of the order are released; the order
  records who cancelled and why (`cancelled_by`, `cancel_reason`, additive optional fields on `Order`).
- **R-6 Audit:** one `CANCEL` event with actor `human:<name>` (REST) or `agent:<id>` (MCP), the order and quote
  ids, and the reason in `data`. Refused attempts are audited as `CANCEL_FAILED` with the error code.
- **R-7 Errors** (`{code, message}` like every other endpoint):
  `404 ORDER_NOT_FOUND` unknown order · `409 INVALID_TRANSITION` order is not `CONFIRMED` (including already
  cancelled) · `409 CUTOFF_PASSED` a cut-off has passed · `403 NOT_ORDER_OWNER` an agent cancels another agent's
  order (MCP) · `422 VALIDATION_ERROR` blank `cancelled_by`.
- **R-8 Contract + docs:** `contracts/openapi.yaml` is regenerated; `backend/README.md` and the MCP tool
  description explain the rule (confirmed orders, before the cut-off, fixed prices unchanged).
- **R-9 Stats:** a cancelled order counts under `orders_by_status.CANCELLED` and is excluded from
  `confirmed_spend_total_eur`.

## Acceptance criteria
- **AC-1** Given a `CONFIRMED` order whose cut-off is in the future, when a human posts cancel with a name and
  reason, then the response is 200 with status `CANCELLED`, `cancelled_by` and `cancel_reason` set.
- **AC-2** Given a cancelled Stock-model order, when availability is checked again for the same product, then the
  cancelled quantity is available again (reservation released).
- **AC-3** Given a `CONFIRMED` order with a line whose `closing_time` is in the past, when cancel is requested,
  then the response is `409 CUTOFF_PASSED` and the order stays `CONFIRMED`.
- **AC-4** Given a `PENDING_APPROVAL`, `REJECTED`, `EXPIRED` or already `CANCELLED` order, when cancel is
  requested, then the response is `409 INVALID_TRANSITION`.
- **AC-5** Given an unknown order id, then `404 ORDER_NOT_FOUND`.
- **AC-6** Given a blank `cancelled_by`, then `422 VALIDATION_ERROR`.
- **AC-7** After a successful cancel, `GET /v1/audit?order_id=…` contains a `CANCEL` event by `human:<name>` with
  the reason; after a refused cancel it contains `CANCEL_FAILED` with the error code.
- **AC-8** Over MCP, the agent that placed the order can cancel it (`CANCEL` by `agent:<id>`); a different agent
  identity gets `NOT_ORDER_OWNER` and the order is unchanged.
- **AC-9** `GET /v1/stats` shows the order under `CANCELLED` and no longer in confirmed spend.
- **AC-10** `contracts/openapi.yaml` contains `POST /v1/orders/{order_id}/cancel` with request and response models.

## Out of scope
Cancelling `PENDING_APPROVAL` orders (the approver rejects them), partial cancellation of single lines, and any
upstream call (the mock has no order endpoint; orders live in this gateway).
