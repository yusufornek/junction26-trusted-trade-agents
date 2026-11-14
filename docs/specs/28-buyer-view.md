# 28 — Buyer-side view

Status: in progress · Decided on 2026-11-14 · Design reviewed with the external architect (round 4).

## Purpose
The buying organisation sees and controls what its agents do: agents, mandates, spending against limits, orders,
approvals and activity — scoped to its own customers.

## Requirements
- **R-1 Endpoints under `/v1/buyer`** (buyer_user credentials; operator may read): `GET /agents`, `GET /mandates`,
  `GET /spend`, `GET /orders`, `GET /approvals`, `GET /activity`; `POST /agents/{id}/suspend`,
  `POST /agents/{id}/resume`, `POST /mandates/{jti}/revoke`.
- **R-2 Roles:** `buyer_viewer` reads; `buyer_approver` also approves and rejects pending orders of its
  customers (same evidence-revision rule as the operator); `buyer_admin` also suspends agents and revokes
  mandates. Every response states the customer scope it covers.
- **R-3 Spend:** per agent `{window_start, window_end, currency, limit, limit_source (mandate | policy),
  autonomous_spend, remaining, pending_commitments, human_approved_spend}` from the same ledger the policy uses.
- **R-4 Suspension:** a local emergency suspension blocks the agent's next command (`403 AGENT_SUSPENDED`,
  CONTACT_ADMIN) and is shown with its provenance next to VC revocations; history stays visible.
- **R-5 Activity:** the organisation's events (spec 22) newest first, with plain-language summaries.
- **R-6 Approver identity:** a buyer approval is audited as `human:buyer_approver:<credential ref>` with the
  organisation; the order shows who (which side) signed off.

## Acceptance criteria
- **AC-1** A buyer_viewer of organisation H sees H's agents and orders and none of another organisation's.
- **AC-2** `/spend` equals the policy's own computation after two autonomous and one human-approved order.
- **AC-3** buyer_admin suspends an agent → its next lookup answers `403 AGENT_SUSPENDED`; resume restores it.
- **AC-4** buyer_admin revokes a mandate → the next command with it fails (spec 12) and a pending order of that
  agent cannot be approved; confirmed orders stay visible.
- **AC-5** buyer_approver approves a pending order of its customer; a buyer_viewer attempt → `403`.
- **AC-6** buyer_approver of H cannot approve an order of another organisation (`404`).
- **AC-7** `/activity` lists the suspension, the revocation and the approval with actors.
