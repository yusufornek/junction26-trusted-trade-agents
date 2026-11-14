# 33 — Signed webhooks and approver notifications

Status: in progress · Decided on 2026-11-14 · Design reviewed with the external architect (round 4).

## Purpose
Agents and approvers learn about decisions when they happen instead of polling.

## Requirements
- **R-1 Subscriptions:** `POST/GET /v1/webhook-subscriptions`, `PATCH`, `DELETE` (disable), `GET
  /v1/webhook-subscriptions/{id}/deliveries`, `POST .../deliveries/{id}/redeliver`; scoped to the principal's
  customers; event types `order.approval_required`, `order.evidence_changed`, `order.confirmed`,
  `order.rejected`, `order.cancelled`, `order.expired`, `mandate.revoked`, `proposal.received`.
- **R-2 Delivery:** from the durable outbox (spec 22), at least once, stable event ids, aggregate revisions,
  exponential backoff with jitter, bounded attempts, then `failed` with manual redelivery.
- **R-3 Signature:** Standard Webhooks (pinned revision) with its Ed25519 form: headers `webhook-id`,
  `webhook-timestamp`, `webhook-signature`; the public key is in the JWKS; a receiver verification snippet is in
  the README.
- **R-4 Destination safety:** HTTPS destinations only (plus `http://localhost` in sandbox), ownership verified
  by a challenge at subscription time, public-address egress rule re-checked at each delivery including after
  DNS changes; redirects are not followed.
- **R-5 Approver notifications:** an `order.approval_required` event to an approver subscription carries a link
  to the authenticated review page (`/approvals/{order_id}?revision=N`); approving remains a revision-bound
  authenticated POST.
- **R-6 Separate status:** notification delivery status is shown next to, and independent from, the purchasing
  decision.

## Acceptance criteria
- **AC-1** A pending order produces one `order.approval_required` delivery with a valid signature.
- **AC-2** Receiver down → retries with growing intervals; after recovery the event arrives; a receiver that
  deduplicates on `webhook-id` processes it once.
- **AC-3** Tampered body fails verification with the published key.
- **AC-4** A subscription to a private address or plain HTTP (outside sandbox localhost) is refused; a
  destination whose DNS later resolves to a private address is not called.
- **AC-5** Exhausted attempts → `failed`; manual redelivery succeeds.
- **AC-6** A subscriber receives only events of its own customers.
- **AC-7** The notification link opens the review for the stated revision; a stale revision answers
  `EVIDENCE_CHANGED`.
