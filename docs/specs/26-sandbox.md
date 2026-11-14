# 26 — Deterministic developer sandbox

Status: in progress · Decided on 2026-11-14 · Design reviewed with the external architect (round 4).

## Purpose
An outside developer builds against the gateway with reproducible stock and prices, and the team rehearses the
demo without depending on live stock.

## Requirements
- **R-1 `FixtureSupplierAdapter`** (spec 25 interface) serving versioned scenarios from
  `backend/sandbox/scenarios/*.yaml`: `full_stock`, `split_stock`, `missing_evidence`, `shifted_date`,
  `expired_quote`, `approval_time_stock_change`, `supplier_timeout`.
- **R-2 Sandbox workspace:** environment `sandbox` with its own customer(s), agent credentials, reservations,
  budgets, idempotency records, caches, webhooks and audit scope; a fixture clock the scenario controls.
- **R-3 Same rules:** sandbox requests run through the same policy, evidence and approval code as live requests.
- **R-4 Admin endpoints (operator or sandbox-admin credential):** `GET /v1/sandbox/scenarios`,
  `POST /v1/sandbox/reset {scenario}`; a reset invalidates outstanding quotes and pending orders of the workspace
  explicitly (they become `EXPIRED` with reason `sandbox_reset`).
- **R-5 Labels:** every sandbox response, signed artifact and UBL document carries `environment: sandbox`; fixture
  observations are marked as such in provenance.
- **R-6 Credential issuance:** `gen_keys.py --sandbox-agent <id>` issues a sandbox-scoped agent credential.

## Acceptance criteria
- **AC-1** Resetting `split_stock` twice and running the same request gives identical offers, plans and totals.
- **AC-2** `approval_time_stock_change`: the order is pending, the scenario lowers stock, approval answers
  `EVIDENCE_CHANGED` (spec 17).
- **AC-3** `supplier_timeout`: the lookup answers a retryable recovery payload.
- **AC-4** A sandbox credential cannot read or create live objects and vice versa.
- **AC-5** After a reset, an outstanding quote cannot be ordered and names the reset as the reason.
- **AC-6** A sandbox quote's signed payload and UBL export contain `environment: sandbox`.
- **AC-7** Sandbox spending does not count against live daily limits.
