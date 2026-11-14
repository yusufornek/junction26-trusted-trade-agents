# 47 — Purchase intent integrity

Status: built · Decided on 2026-11-14 with Gökalp: specs from here on describe general capabilities that cover a
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

## Implementation and decisions taken

- Intent versions are compare-and-set revisions, including state changes and handovers. Signed quotes and orders
  retain the revision at which their commercial evidence was issued. Canonical digests ignore object-key and line
  ordering, but retain quantities, constraints and fulfilment policy.
- `fulfilment_policy` is an object with `mode` and `minimum_share` (0–1). Coverage is the mean of each line's fulfilled
  quantity fraction, so unlike units are not added together. Every essential line must be complete. Held outcomes
  require an explicit human partial-confirmation review; open remainders have their own lookup actions.
- Supplier sequence numbers start at one for each stable gateway submission reference. Events are buffered across
  sequence gaps; duplicate event ids cannot apply twice. A superseded order's events cannot affect its replacement.
- Unknown supplier submissions reconcile first, wait through the adapter's declared consistency window and permit
  at most one same-reference re-submission. Unresolved outcomes then require a human. Missing consistency-window
  metadata fails closed to human reconciliation. Restart recovery uses only the intent dispatcher for intent orders.
- `supersedes` cancels uncommitted intents; an existing commercial order must first have its cancellation acknowledged.
  Declared `additional` demand requires approval, including when introduced by an amendment. Pending amendments protect
  both the proposed demand and the still-existing order from duplicate purchases.
- Post-order amendments use approved cancel-and-replace with a fresh, matching quote and normal cutoff/mandate checks.
  Human REST confirmation binds the existing order evidence, proposed intent and replacement quote. Supplier-backed
  replacements wait for cancellation acknowledgement before another order can be created.
- Sandbox faults use `PUT /v1/intents/{id}/sandbox-faults` with `drop_order_response`,
  `confirmation_delay_seconds` and/or `confirmed_lines`. No new environment variables are needed.
- REST intent routes, the four named MCP tools and version-1 A2A intent commands use the same persistent store.
  Generated tool manifests, developer HTML and OpenAPI artifacts are left for merge-time regeneration.

## Acceptance tests

Exactly nine tests live in `backend/tests/test_spec47_purchase_intent_integrity.py`:

| AC | Test |
|---|---|
| AC-1 | `test_AC_1_dropped_response_replays_rest_mcp_one_order` |
| AC-2 | `test_AC_2_changed_payload_requires_explicit_amendment` |
| AC-3 | `test_AC_3_concurrent_cancel_confirm_has_one_winner` |
| AC-4 | `test_AC_4_handover_preserves_pending_approval_and_rechecks_category` |
| AC-5 | `test_AC_5_partial_policy_commits_only_allowed_lines` |
| AC-6 | `test_AC_6_existing_demo_464306_1010_confirmed_unchanged` |
| AC-7 | `test_AC_7_unknown_outcome_keeps_stable_reference_through_window` |
| AC-8 | `test_AC_8_out_of_order_and_duplicate_supplier_events_are_terminal` |
| AC-9 | `test_AC_9_new_id_and_second_agent_detect_same_demand` |

## Verification (2026-11-14)

- Final spec-only run: **9 passed** (4.47 s). Spec plus existing order-policy tests: **33 passed** (10.51 s).
- Relevant regression run (14 files, not the full suite): **307 passed, 8 failed**. The failures are two generated
  OpenAPI snapshot comparisons, three exact MCP-tool inventories, one REST-path allowlist, one exact A2A-skill
  inventory and one exact supplier-capability dictionary. They omit the new required intent interfaces and adapter
  consistency-window metadata. Generated files and existing test expectations were intentionally left unchanged.
- All tests use temporary state and mocks/in-process apps. No git commands, commits or writes to `backend/var/`.
