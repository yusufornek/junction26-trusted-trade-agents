# 11 — Tamper-evident audit trail

Status: in progress · decided on 2026-11-14 to build it · Brief: "a usable, human-readable log of an agent's purchasing decisions,
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

- **AC-4** `GET /v1/audit/head` returns a head whose JWS verifies with the published JWK (spec 10).
- **AC-5** `GET /v1/audit/verify?checkpoint_entries=N&checkpoint_hash=H` reports `checkpoint_match: false` when
  an entry at or before N was changed, even if the chain was recomputed afterwards.

## Decisions (2026-11-14)
- One chain per gateway. Entries are appended inside the database write transaction, so the chain order equals
  the commit order across REST and MCP processes.
- **R-6 Signed head:** `GET /v1/audit/head` → `{entries, head_hash, at, jws}`, the JWS signed with the active
  quote key (spec 10). A buyer stores it as an independent checkpoint.
- **R-7 Checkpoint verify:** `GET /v1/audit/verify` accepts an optional checkpoint `{entries, head_hash}` and
  reports `checkpoint_match`.
- **R-8 Access:** `head` and `verify` are readable by any authenticated principal (they carry hashes and counts
  only); the entries themselves follow spec 19.
- Canonical JSON of an entry: RFC 8785 over `{seq, ts, actor, action, summary, order_id, quote_id, data}`.
- Honest limit: a chain kept in the same database proves integrity only against checkpoints held outside it;
  the signed head is that checkpoint.
