# 40 — Graduated autonomy: approval first, autonomy earned

Status: in progress · Source: Valio Aimo's answer on the Junction Discord, 2026-11-14 (in `docs/CHALLENGE.md`).

## Purpose
Valio Aimo enforces no approval today; customers control spending through internal instructions or by letting
only a manager order. For agents, Valio expects human approval at first and autonomy later, once reliability is
proven. The gateway makes that path explicit: every agent starts with human approval on every order, builds a
measured track record, and the buyer organisation grants autonomy within limits when the record justifies it.

## Requirements
- **R-1 Autonomy level per agent** (policy mandate and verifiable mandate claim): `approval_required` (every
  order goes to `PENDING_APPROVAL` with rule hit `autonomy_level` — "this agent's orders need sign-off until
  autonomy is granted") or `autonomous_within_limits` (today's behaviour). Default for any agent without an
  explicit level: `approval_required` (`policy.autonomy.default`).
- **R-2 Track record:** `GET /v1/agents/{id}/track-record` (operator, the agent's buyer organisation, and the agent
  itself) — over a window (default 30 days): orders placed, approved unchanged, rejected, expired, evidence
  changes, cancellations, hard-constraint refusals, average approval time, share of orders a human approved
  without changes, and the date of the last rejection.
- **R-3 Readiness signal:** the record carries `autonomy_recommendation {eligible, reasons[]}` from configurable
  thresholds (`policy.autonomy.promotion`: minimum orders approved unchanged, maximum rejection share, days
  since the last rejection or hard-constraint refusal). It is a recommendation; a person decides.
- **R-4 Promotion and demotion:** `POST /v1/agents/{id}/autonomy {level, reason}` by the operator or a
  `buyer_admin` of the agent's organisation; audited as `AUTONOMY_CHANGED` with the track-record snapshot. A
  rejection or hard-constraint refusal while autonomous creates an `autonomy.review_suggested` event (webhook
  when spec 33 is present); demotion back is one call.
- **R-5 Who signs:** approvals for an organisation's orders can be done by its `buyer_approver` (spec 28); the
  order shows whether the buyer side or the Valio operator signed.
- **R-6 Demo defaults:** `backend/policy.yaml` sets `autonomy.default: approval_required`; the demo mandates for
  `hotel-chain-agent` and `web-console` carry `autonomous_within_limits` with the note "autonomy granted after
  the pilot's approved orders (demo setting)", so the demo still shows an autonomous confirmation, and a newly
  registered agent shows the approval-first path.

## Acceptance criteria
- **AC-1** A registered agent without an explicit level → its in-limit order is `PENDING_APPROVAL` with rule hit
  `autonomy_level`.
- **AC-2** The same agent after promotion to `autonomous_within_limits` → the same order confirms; the promotion is
  audited with the reason and the record snapshot.
- **AC-3** The track record counts approved-unchanged, rejected, expired and evidence-changed orders correctly over
  the window.
- **AC-4** With thresholds (5 approved unchanged, ≤ 10 % rejected, 7 days since a rejection) the recommendation
  turns eligible only when all hold, and names the unmet ones otherwise.
- **AC-5** An autonomous agent's order rejected by a human → `autonomy.review_suggested` event; demotion makes its
  next order pending again.
- **AC-6** A verifiable mandate with `autonomy: approval_required` overrides a policy row that says autonomous
  (the stricter level wins).
- **AC-7** A `buyer_admin` of another organisation cannot change this agent's level (`404`).
- **AC-8** The live demo request with `hotel-chain-agent` still confirms at 1,010 € (demo mandate is autonomous).
