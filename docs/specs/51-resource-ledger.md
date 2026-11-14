# 51 — Resource ledger and reservations

Status: in progress · Decided on 2026-11-14 with Gökalp (capability spec, see spec 47). Builds on specs 43 (tenants),
44 (governance) and 45 (guardrail profiles).

## Purpose
Anything finite that several agents or kitchens compete for — money, storage capacity, returnable crates, a shared
case — is a resource with a ledger and reservations, so contention is decided atomically and explained.

## Requirements
- **R-1 Resources:** `POST/GET /v1/resources` (tenant admin or operator): `{kind: budget|storage|asset|pool, scope,
  period?, capacity, unit}`. Budgets scope by customer and category set; storage by customer, zone (`chilled`,
  `frozen`, `dry`) and delivery date, in declared slots; assets by type (crates, roll cages) with a deposit value.
- **R-2 Demand mapping:** products declare what they consume (`storage_zone`, slots per pack, returnable asset type and
  count); a quote computes its demand on every matching resource.
- **R-3 Reservations:** creating a quote reserves its demand on all matching resources in one `BEGIN IMMEDIATE`
  transaction; ordering consumes; expiry, cancellation, rejection or a changed quote releases. Committed plus reserved
  never exceeds capacity. When something does not fit: `409 RESOURCE_CONTENDED {resource, remaining, competing
  reservations (agent, amount, expiry)}` and options: smaller basket, split delivery dates, defer, or ask a human.
  Managers can release an uncommitted reservation to prioritise another basket.
- **R-4 Split proposals:** for storage contention the gateway proposes a split across delivery dates that fits, with the
  extra delivery cost (policy setting).
- **R-5 Assets:** orders add expected returnable assets to the customer's asset balance; receiving reports (spec 52)
  record returns; the balance and deposit value are visible and an over-limit balance needs approval.
- **R-6 Pools:** a pool resource lets kitchens of one tenant share one SKU: participants commit quantities against a pool
  revision; whole packs, allocation per participant, surplus and an internal handling cost split by allocation are
  computed; any change makes commitments on the old revision stale, and finalising needs all current commitments on the
  current revision (`409 POOL_REVISION_STALE` otherwise), producing one quote and order with allocation lines.
- **R-7 Surfaces:** REST and MCP tools; webhooks `RESOURCE_CONTENDED`, `POOL_REVISION_CHANGED`; audit entries for
  reservations and releases.
- **R-8 External authority:** resources whose balance the supplier owns (kind `credit`) carry `authority: external` and `observed_at`; they are reserved provisionally and re-read at submission; a supplier credit refusal becomes `RESOURCE_CONTENDED {authority: external}` with options.

## Acceptance criteria
- **AC-1** Two concurrent quotes of 600 € and 500 € against an 800 € budget (threads hitting the app): exactly one
  reserves; the other gets `RESOURCE_CONTENDED` naming the competitor. Releasing the first lets the second reserve.
- **AC-2** A frozen pallet that exceeds the remaining freezer slots for the date is contended, and the proposed
  two-date split fits with its extra delivery cost.
- **AC-3** An order with 10 returnable crates raises the asset balance by 10; a receiving report returning 6 lowers it to 4.
- **AC-4** A 6 + 6 pool against a case of 12 yields one pack split 6/6 with handling cost; after one withdraws,
  finalising with old commitments answers `POOL_REVISION_STALE`.
- **AC-5** Reservations expire with their quote and release automatically.
- **AC-6** Supplier credit observed as sufficient at quote but consumed elsewhere before submission yields `RESOURCE_CONTENDED {authority: external}` at submission, with options.
