# 63 — Connection contract and driving test

Status: in progress · Decided on 2026-11-14 with Gökalp (AI-gateway focus; drafted by Fable from Astra's round 2).
Builds on specs 23, 26, 37, 38, 40 and 60.

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
