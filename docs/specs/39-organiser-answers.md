# 39 — Align with the organiser's answers about the mock API

Status: in progress · Source: Valio Aimo's answers on the Junction Discord, 2026-11-14 (recorded in
`docs/CHALLENGE.md`).

## Purpose
The organiser clarified five behaviours of the mock API. The gateway follows them exactly.

## Answers (as given)
1. No order API exists; producing orders for human approval is enough — nothing has to be sent anywhere.
2. `closingTime` is the order cut-off for the related `availableDate`. Mock data stays the same all weekend.
3. Availability ≥ 999999 means availability is unknown and Valio's own logic allows ordering any quantity
   (typical for Terminal products that Valio does not stock). An empty availability response means not
   available.
4. `allowedLotSize` 0.01 is to be treated as 1; fractional crates do not exist.
5. Rate limit: exceeding it returns HTTP 429 and blocks the caller for 30 seconds. A few hundred calls per
   minute is fine; more than 10 per second is not. Honour 429.

## Requirements
- **R-1 Whole units:** an `allowedLotSize` below 1 counts as 1 for every sales unit (including KG). Offers,
  plans, quotes and size reconciliation use whole sales units; a fractional quantity in a quote answers
  `422 INVALID_LOT_SIZE` naming the whole-unit step.
- **R-2 Unknown availability:** a schedule line with `availableAmount` ≥ 999999 means "availability unknown,
  ordering any quantity allowed". The offer covers the requested quantity, carries the informational warning
  `AVAILABILITY_UNCONFIRMED` ("Valio does not know this product's stock; its ordering logic accepts any
  quantity"), shows `available_amount: null` and `availability_known: false`, and no local reservation is
  counted against it. `AVAILABILITY_UNCONFIRMED` is in `approval.ignored_warnings` by default. The rule depends
  on the amount, whatever the `logisticModel` says.
- **R-3 Not available:** an empty `scheduleLines` list gives `NOT_AVAILABLE` ("Valio reports this product as not
  available"), which is in `reject.on` by default. A product missing from the response stays
  `AVAILABILITY_ERROR`. `UNKNOWN_PRODUCT` remains a valid policy code for stored data and is no longer emitted.
- **R-4 Cut-off:** `closingTime` is the order cut-off for the `availableDate` it accompanies. When it lies before
  now, the offer carries `CUTOFF_PASSED` with the text "Order cut-off for delivery on <date> passed at <time>"
  and the date and time filled in; it keeps needing approval. Cancellation keeps using the cut-off (spec 04).
- **R-5 Honour 429:** the Valio client paces outbound calls to at most 5 per second across all requests
  (token bucket shared by the process). On HTTP 429 it stops calling Valio for `Retry-After` seconds (30 when
  the header is missing), answers affected requests with `503 UPSTREAM_RATE_LIMITED` (retryable,
  `next_action: RETRY_SAME_COMMAND`, `retry_after_seconds` = remaining block), counts the event in stats and
  writes one audit entry per block. Cached results keep being served during the block.
- **R-6 Visible state:** `GET /health` shows `upstream {blocked_until, calls_last_minute}`.

## Acceptance criteria
- **AC-1** A box product with `allowedLotSize` 0.01 → 1,000 L in 1 L boxes gives 1,000 units; a quote for
  10.5 units → `422 INVALID_LOT_SIZE`; a KG product with lot 0.01 is quoted in whole kilograms.
- **AC-2** A Terminal line with `availableAmount` 999999 → offer coverage 1.0, `AVAILABILITY_UNCONFIRMED`,
  `availability_known: false`; the same with 666666666 and 9999999999; a policy-clean order confirms without a
  human.
- **AC-3** A product with an empty `scheduleLines` list → `NOT_AVAILABLE`, the order is `REJECTED`, the offer has
  no `rank_label`.
- **AC-4** A `closingTime` before now → `CUTOFF_PASSED` whose text names the delivery date and the cut-off time.
- **AC-5** A mocked 429 without `Retry-After` → the next lookup within 30 s makes no upstream call and answers
  `503 UPSTREAM_RATE_LIMITED` with `retry_after_seconds` ≤ 30; after the block calls resume; one audit entry.
- **AC-6** 20 lookups fired at once never exceed 5 upstream calls in any one-second window (mocked clock).
- **AC-7** The live demo request still returns 464306 at 1,010 € and confirms within the mandate.
