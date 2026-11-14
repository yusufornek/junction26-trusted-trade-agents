# 17 — Approval applies to the evidence the human saw

Status: in progress · Decided on 2026-11-14 after the external architecture review.

## Purpose
Approval re-checks stock and policy. When the facts changed since the order was placed (stock dropped, a new
warning appeared), today the order is confirmed and the change is only written to the response and the audit
log. The approver decides on what the inbox showed; a changed picture comes back to the approver first.

## Requirements
- **R-1 Evidence revision:** every order carries `evidence_revision` (integer, 1 at placement). The decision
  evidence of a revision is: per line the quantity, covered amount, confirmed delivery date and warning codes;
  the order-level warning codes; the policy rule hits (rule names).
- **R-2 Compare at approval:** approve refreshes stock and policy as today, then compares the refreshed decision
  evidence with the stored one. Fetch timestamps and texts are outside the comparison.
- **R-3 Changed evidence returns to the human:** when it differs, the order keeps `PENDING_APPROVAL`, stores
  the refreshed lines and policy, increments `evidence_revision`, writes an `EVIDENCE_CHANGED` audit entry
  with the list of changes, and answers `409 EVIDENCE_CHANGED` with `data {evidence_revision, changes[]}` —
  each change in plain words (e.g. "464306: covered 400 → 300 of 400", "new warning PARTIAL_STOCK").
- **R-4 Stated revision:** the approve body accepts an optional `evidence_revision`. A value different from the
  order's current revision answers `409 EVIDENCE_CHANGED` with the current revision and makes no upstream call.
- **R-5 Unchanged evidence confirms:** an approve whose refreshed evidence equals the stored revision confirms
  the order as today.
- **R-6 Hard failures stay hard:** a refreshed evaluation that is `REJECTED`, or unverifiable availability,
  answers as today (no confirmation, order stays pending or is refused per the existing rules).
- **R-7 Timeline:** the order timeline shows `EVIDENCE_CHANGED` steps.

## Acceptance criteria
- **AC-1** Pending order, stock unchanged → approve confirms; `evidence_revision` stays 1.
- **AC-2** Pending order fully covered at placement; stock then drops below the ordered amount → approve answers
  `409 EVIDENCE_CHANGED`, the order is still pending with the new `PARTIAL_STOCK` warning stored,
  `evidence_revision` is 2, the audit has an `EVIDENCE_CHANGED` entry listing the change.
- **AC-3** After AC-2, a second approve with stock unchanged confirms the order.
- **AC-4** Approve with `evidence_revision: 1` after the order moved to revision 2 → `409 EVIDENCE_CHANGED`,
  no upstream call.
- **AC-5** Only timestamps differ between placement and approval → approve confirms.
- **AC-6** The timeline of the order from AC-2/AC-3 shows placement, evidence change, approval in order.

## For the review
The console can send the revision it displayed (R-4); without it the backend comparison (R-2, R-3) still
protects the approver.
