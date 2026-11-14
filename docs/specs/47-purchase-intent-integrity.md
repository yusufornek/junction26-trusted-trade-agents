# 47 — Purchase intent integrity

Status: in progress · Decided on 2026-11-14 with Gökalp: specs from here on describe general capabilities that cover a
class of situations, built from the scenario reviews with Astra (gpt-6-astra). Builds on specs 02 (quote and order,
idempotency keys), 23 (recovery contract) and 30 (exception journey).

## Purpose
One buying decision stays one commercial commitment, whatever happens to the agents and connections around it: lost
responses, retries over another protocol, a second agent taking over, a cancellation racing a confirmation, or a basket
that can only be partly fulfilled.

## Requirements
- **R-1 Purchase intent:** `POST /v1/intents {client_intent_id, customer_id, lines[], deliver_by, constraints}` creates a
  tenant-scoped intent with a canonical payload digest and a version. Quotes and orders reference `intent_id` and
  `intent_version`. REST, MCP and A2A share the same intent store.
- **R-2 Idempotent across protocols:** the same `client_intent_id` with the same digest returns the existing intent and
  its current result (quote, order, status) from any protocol. The same id with a different digest answers
  `409 INTENT_AMENDMENT_REQUIRED` with the difference; `POST /v1/intents/{id}/amendments` creates a new version, and
  amendments after an order exists follow the order change rules and need approval.
- **R-3 State machine:** intent states `DRAFT → QUOTED → PENDING_APPROVAL → ORDERED → CONFIRMED | PARTIALLY_CONFIRMED →
  CLOSED`, plus `CANCELLED` and `EXPIRED`. Every transition is a compare-and-set on the version inside `BEGIN IMMEDIATE`;
  a losing writer gets `409 INTENT_VERSION_CONFLICT` with the winning state. A cancellation and a confirmation racing on
  one order end in exactly one final state, and the loser receives a recovery action (spec 23).
- **R-4 Handover:** `POST /v1/intents/{id}/resume` lets another agent of the same tenant continue the intent under its
  own mandate; the gateway re-checks that mandate against the intent (limits, categories), records the handover in the
  audit log and returns the structured next actions. Pending approvals stay valid; nothing is duplicated.
- **R-5 Partial commitment:** an intent declares `fulfilment_policy`: `all_or_nothing`, `allow_partial` with a minimum
  share, or per-line `essential: true`. When the supplier can confirm only part, the gateway applies the policy:
  commits what the policy allows, holds the rest as an open remainder with its own next action, or asks the human
  when essential lines are missing.
- **R-6 Fault injection (sandbox):** a sandbox switch drops the response after the order is written, delays
  confirmations, or confirms only some lines, so these paths are demonstrable.
- **R-7 Surfaces:** REST, MCP tools (`create_intent`, `get_intent`, `amend_intent`, `resume_intent`) and A2A task
  mapping; webhooks `INTENT_AMENDED`, `INTENT_HANDOVER`, `INTENT_PARTIALLY_CONFIRMED`.
- **R-8 Unknown outcome:** a supplier submission carries a gateway-generated stable reference; a lost response sets `SUBMISSION_UNKNOWN`; "not found" inside the adapter's declared consistency window stays unknown; the gateway re-queries and re-submits only with the same reference, then asks a human.
- **R-9 Inbound supplier events:** deduplicated by supplier event id, applied in supplier sequence through the R-3 state machine; a cancel is `CANCEL_REQUESTED` until acknowledged; an acceptance after a cancel request opens `CANCEL_DISPUTED`, shown to the kitchen as "delivery may still arrive".
- **R-10 Demand fingerprint:** each intent line is fingerprinted (customer, delivery point, consumption window, product or functional need); overlap with open intents or orders of any agent of the tenant answers `POSSIBLE_DUPLICATE_DEMAND` with the overlaps and requires `supersedes` or `additional` (the latter with approval).

## Acceptance criteria
- **AC-1** An order whose response is dropped, retried once over REST and once over MCP with the same
  `client_intent_id`, yields one order; both retries return it.
- **AC-2** The same id with a changed quantity answers `INTENT_AMENDMENT_REQUIRED` with the difference.
- **AC-3** A cancel and a confirm fired concurrently on one order end in exactly one final state; the loser gets
  `INTENT_VERSION_CONFLICT` with a recovery action.
- **AC-4** A second agent resumes an intent with a pending approval; the approval completes it without a duplicate order;
  a second agent whose mandate excludes the category is refused.
- **AC-5** `allow_partial` with a 70 % minimum commits a 4-of-5-line confirmation and leaves the fifth as an open
  remainder; `all_or_nothing` with the same supplier answer commits nothing and asks the human.
- **AC-6** The demo path (464306, 1,010 €, CONFIRMED) is unchanged.
- **AC-7** A supplier accepts an order but the acknowledgement is lost; "not found" inside the consistency window keeps it `SUBMISSION_UNKNOWN`, and the re-submission reuses the same reference (one supplier order).
- **AC-8** A supplier cancel acknowledgement arriving before its acceptance event, and a duplicate event, leave the order in one terminal state.
- **AC-9** A retry with a new `client_intent_id` for the same demand, and a second agent of the tenant buying the same demand, both answer `POSSIBLE_DUPLICATE_DEMAND`.
