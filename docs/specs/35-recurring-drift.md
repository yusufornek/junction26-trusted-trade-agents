# 35 — Recurring orders with drift detection

Status: in progress · Decided on 2026-11-14 · Design reviewed with the external architect (round 4).

## Purpose
The brief's scenario: a standing order slowly stops matching what was approved — a smaller pack, a creeping
price, another brand. The gateway notices and asks before buying.

## Requirements
- **R-1 `POST/GET /v1/recurring-orders`:** approved template (need or basket), baseline revision (products,
  pack conversion, unit and basket cost, quantities, claims / allergens, evidence quality), cadence (RRULE subset:
  daily, weekly on weekdays, monthly) with timezone, permitted deviations `{price_pct, quantity_pct,
  brand_change, pack_change}`, and the mandate that authorises recurring execution (`recurring: true` claim or
  policy flag).
- **R-2 Runs:** `GET /v1/recurring-orders/{id}/runs`; each occurrence has a unique occurrence key
  (`<id>:<local date>`), creates a fresh request and quote through the normal core, and stores the comparison.
- **R-3 Drift:** compared per run against the last run and cumulatively against the baseline: product / brand,
  pack conversion, effective unit cost and basket cost, quantity, claims / allergens, delivery date, evidence
  quality. Missing comparison evidence is its own review condition.
- **R-4 Outcome:** within permitted deviations and policy → ordered autonomously; otherwise `PENDING_APPROVAL`
  with rule hit `recurring_drift` and a plain-language diff ("unit cost 1.01 → 1.09 € (+7.9 %, cumulative
  +12.4 % since baseline)").
- **R-5 Baseline update** is an explicit buyer decision (`POST .../baseline` by buyer_admin / buyer_approver).
- **R-6 Scheduler:** an in-process scheduler runs due occurrences; `POST /v1/recurring-orders/{id}/run-now` for
  demos and tests; a missed run executes once when the service returns (policy: `catch_up: once`); daylight
  saving changes keep the local wall-clock time.
- **R-7 Revocation:** a suspended agent or revoked mandate stops the next run with an event.

## Acceptance criteria
- **AC-1** Two identical runs → both confirmed, no drift.
- **AC-2** Price up 3 % per run with a 5 % per-run tolerance and 8 % cumulative tolerance → third run goes to
  approval naming the cumulative drift.
- **AC-3** The supplier switches to a smaller pack → `recurring_drift` (pack) with the conversion shown.
- **AC-4** The same occurrence triggered twice (scheduler retry) creates one order.
- **AC-5** Comparison evidence missing → approval with reason "comparison evidence missing".
- **AC-6** Baseline update by a buyer approver resets cumulative drift; an agent cannot update the baseline.
- **AC-7** Mandate revoked → the next run is skipped with a `recurring.stopped` event.
- **AC-8** A run scheduled across a daylight-saving change fires at the same local time.
