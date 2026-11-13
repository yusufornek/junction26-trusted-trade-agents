# 07 — Order timeline (traceability)

Status: draft → in progress · Found in the frontend review on 2026-11-14.

## Purpose
`GET /v1/audit?order_id=…` returns only events that carry the order id, so the order page shows "place order"
and "approve" but not the search and the quote that led there. One call should tell the whole story of an order:
what was asked, what was found, what was locked, what the policy decided, what people did.

## Requirements
- **R-1 Endpoint:** `GET /v1/orders/{order_id}/timeline` → ordered list of steps, oldest first.
- **R-2 Steps included:** the lookup behind the quote (via `quote.need_ref`), quote creation, order placement
  and replays, human approve / reject, expiry, cancellation and failed attempts tied to the order or its quote.
- **R-3 Step shape:** `ts`, `actor`, `action`, `step` (one of `lookup`, `quote`, `order`, `decision`,
  `cancellation`, `system`), `summary`, `data` (the audit payload), and for lookup / quote / order steps the
  `live_calls` and `decision_calls` used.
- **R-4 Totals:** the response also carries `order_id`, `quote_id`, `lookup_id` (nullable), `agent_id`,
  current `status`, and total live Valio calls and Decisions calls for the whole order.
- **R-5 No upstream calls;** unknown order → `404 ORDER_NOT_FOUND`.
- **R-6 MCP:** `get_order_status` stays as is; the timeline is a REST read for people.

## Acceptance criteria
- **AC-1** For an order created from a lookup → quote → order → approval, the timeline lists `lookup`, `quote`,
  `order`, `decision` in that order with the right actors (`agent:…`, `human:…`).
- **AC-2** For a quote without `need_ref`, the timeline starts at `quote` and `lookup_id` is null.
- **AC-3** An idempotent retry appears as a replay step; a cancelled order ends with `cancellation`.
- **AC-4** Totals equal the sum of the steps' call counts.
- **AC-5** Unknown order → 404; the contract contains the endpoint with response model.
