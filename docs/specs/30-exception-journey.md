# 30 — Exception journey: the agent waits, resumes and takes its receipt

Status: in progress · Decided on 2026-11-14 · Design reviewed with the external architect (round 4).

## Purpose
When an order needs a human, the outside agent knows what to do while it waits and how to tell that the purchase
is finished. The reference journey is the real finite-stock case: 400 L against limited stock → split plan →
approval → receipt.

## Requirements
- **R-1 Status contract:** `GET /v1/orders/{id}` and MCP `get_order_status` return `order_revision`,
  `next_action`, `poll_after_seconds` (while pending), `expires_at`, and `approval_reason` — one plain sentence
  built from the rule hits (e.g. "The plan meets the quantity and the lactose-free requirement; its total of
  1,708.00 € exceeds this agent's autonomous limit of 1,500.00 €.").
- **R-2 Receipt:** `GET /v1/orders/{id}/receipt` and MCP `get_order_receipt` return the signed receipt (spec 10)
  for decided orders; `409 NOT_DECIDED` with `WAIT_FOR_APPROVAL` otherwise.
- **R-3 Shortage explanation:** split and partial plans carry `shortage {requested, best_single_coverage,
  reason}` in plain words.
- **R-4 Terminal outcomes are distinct:** CONFIRMED, REJECTED (with the approver's comment), EXPIRED, CANCELLED,
  each with its own `next_action`.
- **R-5 Reference run:** `backend/demo/run_exception_journey.py` drives the journey over MCP against the
  sandbox `split_stock` scenario and, with `--live`, against the live mock: lookup, choose the split plan, quote,
  place once with an idempotency key, poll at the suggested interval, report the outcome, fetch the receipt and
  the evidence manifest. It prints a transcript for the demo backup.
- **R-6 Uncertain placement:** after a lost placement response the client retries the same key and receives the
  same order.

## Acceptance criteria
- **AC-1** Split plan order → `PENDING_APPROVAL` with `approval_reason`, `next_action: WAIT_FOR_APPROVAL`,
  `poll_after_seconds`.
- **AC-2** After approval the status shows `CONFIRMED`, revision increased, no next action; the receipt verifies.
- **AC-3** Rejection → status `REJECTED` with the comment and `next_action: STOP`.
- **AC-4** Expiry while waiting → `EXPIRED`, `next_action: LOOKUP_REQUIRED`.
- **AC-5** Receipt before the decision → `409 NOT_DECIDED`.
- **AC-6** Evidence change during waiting → status shows the new revision and stays pending.
- **AC-7** The reference run completes against the sandbox scenario and prints the receipt id (test with the
  in-process gateway).
- **AC-8** Restarting the client while pending and polling again by order id reaches the same outcome.
