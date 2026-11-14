# 63 — Connection contract and driving test

Status: implemented · Decided on 2026-11-14 with Gökalp (AI-gateway focus; drafted by Fable from Astra's round 2).
Builds on specs 23, 26, 37, 38, 40, 54 (typed quantities) and 60.

## Purpose
Agent and gateway mean the same thing by every call. The agent states which contract it speaks, the gateway refuses what
it cannot interpret exactly, and a new agent proves its behaviour in the sandbox before it may commit. Covers stale
schemas, protocol fallbacks, invented arguments, unit misunderstandings and capabilities that are declared but do not
work.

## Requirements
- **R-1 Trading profile:** a versioned machine-readable profile at `GET /v1/trading-profile` and
  `luotsi://trading-profile`: sale unit versus base unit and case contents, price basis (per sale unit, VAT treatment),
  currency, time zone, delivery date versus order cut-off, rounding. The contract digest covers the profile, tool
  schemas and recovery vocabulary; it is published in the cards, `/v1/meta` and the MCP initialize result.
- **R-2 Strict arguments:** REST, MCP and A2A adapters validate against the published schema and pass arguments
  unchanged. An unknown field answers `422 UNKNOWN_ARGUMENT` with the field and the nearest known field. Commercial
  fields (quantity with unit, customer, delivery date, delivery point) have no defaults and answer
  `422 ARGUMENT_REQUIRED`.
- **R-3 Contract echo:** a connection acknowledges a contract version at connect; state-changing calls may carry it
  (`Luotsi-Contract` header or `_meta`). When the acknowledged version differs from the current one by a change marked
  material (unit, default, meaning of a field), the call answers `409 CONTRACT_STALE` with the change list and
  `REFRESH_CONTRACT`. Non-material changes pass. Changes send `notifications/tools/list_changed`.
- **R-4 Contract pinning:** an intent records the contract version it started under. A material change mid-flight sets
  `REEVALUATION_REQUIRED`: the buyer re-evaluates under the new contract or completes on the signed quote until its
  expiry.
- **R-5 Capability record:** per connection, the negotiated protocol revision and client capabilities, each `declared`
  until first successful use, then `proven`. Human-question steps choose their path from proven capabilities. An
  elicitation unanswered by its deadline falls back to `input_required` plus an approval-link notification (spec 61).
  Connection health shows the matrix and what the fallback removed.
- **R-6 Driving test:** `POST /v1/connections/{id}/driving-test` (MCP `start_driving_test`, A2A skill) opens a sandbox
  challenge set that the agent runs through its own connection: unit worked example ("12 milk": cartons versus cases),
  expired quote → `REFRESH_QUOTE`, dropped response → retry with the same intent id, unknown argument, approval required
  → wait, elicitation probe. Each check ends `passed`, `failed` or `not_attempted`.
- **R-7 Graduated enablement:** the result is signed (spec 10) and bound to connection, client and contract version.
  Order scope turns on only with the unit, retry and approval checks passed. Failed checks keep that operation in sandbox
  or `approval_required`; the owner can override through a change request (44). A material contract change expires the
  result. Seeded demo agents carry a passing result.
- **R-8 Visibility:** result and enabled operations appear in connection health, `get_agent_context` and the agent
  register (44).

## Acceptance criteria
- **AC-1** The trading profile digest is identical in the server card, the A2A card and `/v1/meta`.
- **AC-2** A quote call with an invented `delivery_date` (the schema says `deliver_by`) answers `UNKNOWN_ARGUMENT`
  naming `deliver_by`; a quantity without a unit answers `ARGUMENT_REQUIRED`; the same happens over MCP and A2A.
- **AC-3** After a material change to a case size, a connection on the old contract gets `CONTRACT_STALE` with the
  change on its order call; after refresh the call succeeds; an open basket shows `REEVALUATION_REQUIRED`.
- **AC-4** A client that declares elicitation but never answers: the tier question falls back to `input_required` and an
  approval link, and the capability stays `declared`.
- **AC-5** An agent that orders 12 cases when 12 cartons were meant fails the unit check; its connection keeps lookup and
  quote, and order answers `403 DRIVING_TEST_REQUIRED` naming the check; after a passing run the signed result verifies
  against the JWKS and order is enabled.
- **AC-6** The demo path (464306, 1,010 €, CONFIRMED) is unchanged; the seeded demo agents carry a passing result.

## Decisions taken
- Capability code is in `backend/app/connection_contract*.py`; existing shared modules contain hooks only.
- A contract version is a SHA-256 digest of the profile, all audience tool input/output schemas, and recovery map.
  Revision history is persistent. Validation constraints/defaults and commercial facts are material; editorial
  titles, examples and documentation are not. Field descriptions are conservatively material because they can change
  meaning. Returning to an earlier digest creates a fresh revision.
- Connect requests can acknowledge `contract_version`. SDK initialize negotiates protocol through the installed SDK
  and records capabilities. REST clients explicitly acknowledge through `POST /v1/connections/{id}/contract`.
  Calls echo `_meta["luotsi/contract"]` or `Luotsi-Contract`. An unacknowledged external connection cannot mutate trade.
- Historical internal/domain callers without an issued connection retain the previous authority checks. Every issued
  external connection is graduated. The two seeded demo identities carry signed initial results; those results are
  not silently renewed on a material change.
- Contract refresh and driving graduation are independent: after a material change the client refreshes and repeats
  the required checks. An explicit `Luotsi-Contract-Pin: signed-quote` (MCP `luotsi/contractPin`) selects an unexpired
  verified quote under its original pin; it does not bypass current authority, approval, or graduation checks.
  A passing result for that same old connection/client/contract can complete its pinned promise until quote expiry;
  it cannot authorize a fresh purchase under the new contract.
- Intent pins are separate persistent records. Response links expose their current reevaluation status; mutable
  navigation metadata is excluded from quote integrity just like the existing forward links. Reevaluation performs
  a new ordinary lookup; recipe baskets must repeat their manifest lookup under the refreshed contract.
- Challenges use the normal SDK tools and order engine in isolated, fixture-backed databases beneath the system
  temporary directory. The caller cannot supply pass flags. Unit evaluation compares actual quote quantities with
  the declared conversion; retry compares the actual replayed order; approval requires observing WAIT_FOR_APPROVAL.
  Optional checks remain not_attempted when the run is finished early. Required checks alone control order enablement.
  An explicitly failed quote-recovery check keeps new quotation in the sandbox until retested or independently
  overridden; not_attempted optional checks do not block quotation.
- SDK elicitation has a bounded deadline and proves capability only on a valid response. A timed-out capability
  remains declared and is removed from the usable path until renegotiated; fallback carries a scoped approval link
  and a recorded notification for the spec-61 delivery seam.
  The SDK emits an approval-link notification on the agent-context resource. Legacy MCP revisions cannot carry an
  InputRequiredResult on the wire, so the same SDK request/state is preserved in a structured tool result with
  status input_required; modern clients receive the native result. Contract changes use native legacy notifications
  and native modern subscription events.
- Overrides are spec-44 change requests bound to connection and current contract. An independent tenant administrator
  decides through the existing confirmed governance decision flow. An override never fabricates a passing test result.
- Delivery-point fields are required where the published schema includes them; this capability does not invent a
  delivery-point argument for earlier schemas that identify delivery through the customer and date.

## Acceptance mapping
Exactly six focused tests in `backend/tests/test_spec63_connection_contract_driving_test.py`:

| AC | Test |
|---|---|
| AC-1 | `test_AC_1_identical_digest_in_cards_meta_resource_and_sdk_initialize` |
| AC-2 | `test_AC_2_strict_unknown_and_required_units_rest_mcp_a2a` |
| AC-3 | `test_AC_3_material_change_stale_refresh_pinned_basket_and_signed_quote` |
| AC-4 | `test_AC_4_unanswered_declared_elicitation_falls_back_with_approval_link` |
| AC-5 | `test_AC_5_measured_failed_and_passing_runs_signatures_and_order_gate` |
| AC-6 | `test_AC_6_demo_464306_1010_confirmed_and_seeded_passing_results` |

Run from `backend/` with the supplied Python environment and
`-m pytest tests/test_spec63_connection_contract_driving_test.py -q`.

Generated discovery/developer/OpenAPI artifacts are left for merge regeneration as required.

## Validation
- Final completion run: **8 passed in 50.99 s** — all six acceptance tests, the existing provenance-tampering
  refusal, and spec 60's existing connection-health/demo check. This includes legacy and modern SDK contract-change
  notifications, explicit null forwarding, the governed override, open recipe-basket reevaluation, and the signed-old-
  promise path. The demo remains product 464306, EUR 1,010, CONFIRMED.
- Focused acceptance/idempotency/expiry run: **10 passed in 48.37 s**, including the existing A2A message-id
  durability/conflict check and all three existing earliest-quote-expiry parameter cases.
- Completed core compatibility selection (`test_signed_quotes.py`, `test_recovery_contract.py`,
  `test_orders_policy.py`, plus this acceptance file): **76 passed, 3 failed in 214.19 s**. Two assertions must be
  reconciled at merge: generated OpenAPI equality (regeneration is explicitly deferred), and the old recovery-action
  allowlist excluding the required REFRESH_CONTRACT action. The third failure counted an extra background stock-watch
  supplier read during a tampering test; that exact test passed in the final completion run. It remains a timing
  interaction with the existing scheduler, not evidence of supplier contact by the rejected order command.
- Selected existing spec 60 / A2A / connection checks: **17 passed, 3 failed, 40 deselected in 81.76 s**.
  The A2A idempotency-priority failure was fixed and verified by the focused run above. Two older spec-42 examples
  mutate through newly issued credentials without acknowledging any contract; they now receive CONTRACT_STALE as
  required by R-3. Their examples need the contract acknowledgement step. Bound legacy MCP discovery keeps its
  existing compatibility subset, while gateway cards advertise the expanded surface; three earlier spec-60 inventory
  assertions assuming those lists are identical also need merge reconciliation. Existing test files were not edited.
- No full-suite run, live network calls, live state changes, frontend changes, generated-artifact edits, or commits.
  New signing keys and challenge databases are test/sandbox state under temporary directories. No new environment
  variables or dependencies are required.
