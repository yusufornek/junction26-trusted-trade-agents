# 11 — Tamper-evident audit trail

Status: draft — for the post-MVP review · Brief: "a usable, human-readable log of an agent's purchasing decisions,
for both buyer and Valio Aimo."

## Purpose
The audit table is append-only inside the database. A reader outside the gateway (the buyer, an auditor) should
be able to check that no entry was removed or changed after the fact.

## Requirements
- **R-1** Each audit entry stores `prev_hash` and `hash = SHA-256(prev_hash ‖ canonical JSON of the entry)`,
  forming one chain per gateway.
- **R-2** `GET /v1/audit/verify` recomputes the chain and returns `{valid, entries, head_hash, first_break?}`.
- **R-3** `GET /v1/audit` returns `hash` on every entry; the order timeline shows the hash of each step.
- **R-4** The head hash can be exported (`GET /v1/audit/head`) so the buyer can keep its own checkpoint; a later
  verify can be anchored to a checkpoint the buyer holds.
- **R-5** Existing rows are chained once in a migration; the chain start is recorded as an audit event.

## Acceptance criteria
- **AC-1** A fresh chain verifies; editing or deleting a row directly in SQLite makes verify report the first
  broken entry.
- **AC-2** Appending entries from REST and MCP processes at the same time keeps one valid chain.
- **AC-3** A checkpoint taken earlier still matches after more entries are appended.

## Open choices for the review
One chain per gateway vs. per customer (so a buyer can be given only its own entries); signing the head hash with
the quote key (spec 10).
