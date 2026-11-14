# 22 — Shared foundation: one procurement state machine

Status: in progress · Decided on 2026-11-14 · Design reviewed with the external architect (round 4).

## Purpose
REST, MCP, A2A, schedules and supplier proposals all run the same authenticated commands and produce the same
orders, approvals, events and evidence. This spec fixes the contracts every later feature builds on.

## Requirements
- **R-1 Principal:** every request resolves to `{kind (agent | operator | buyer_user | supplier_agent), id,
  organisation_id, customer_ids, role, credential_ref}`. Roles: `agent`, `valio_operator`, `buyer_admin`,
  `buyer_approver`, `buyer_viewer`, `supplier_agent`. Policy section `organisations` maps an organisation to its
  customers; `gen_keys.py` issues credentials for buyer users and supplier agents the same way as for agents.
- **R-2 Procurement context:** every lookup, quote, order and task carries `{customer_id, supplier_id,
  request_ref, effective_constraints, mandate_ref, policy_version, environment (live | sandbox)}`; the policy
  version is a hash of the policy document in force.
- **R-3 Command idempotency:** every mutating command (quote, order, cancel, approve, reject, subscription and
  proposal writes) accepts an idempotency key scoped to the principal and bound to a digest of the payload, shared
  across transports. Same key and same payload returns the first result; same key and a different payload answers
  `409 IDEMPOTENCY_CONFLICT`.
- **R-4 Domain events:** table `events` `{id, schema_version, type, customer_id, environment, aggregate_type,
  aggregate_id, aggregate_revision, actor, ts, correlation_id, causation_id, payload}`. A state change, its audit
  entry and its event commit in one SQLite transaction. Aggregate revisions increase by one per change.
- **R-5 Durable outbox:** table `outbox` holds one row per (event, consumer) with status, attempts and next
  attempt time; a dispatcher delivers after commit. Consumers register by name (webhooks, A2A push, activity).
- **R-6 Correlation:** `X-Correlation-Id` is accepted or generated, returned on every response, and stored on
  audit entries, events and traces; MCP and A2A calls carry it in their metadata.
- **R-7 Compatibility manifest:** `GET /v1/meta` (public) lists the specification versions the gateway implements
  (OpenAPI, MCP protocol and server-card draft commit, A2A, UBL, VC data model and securing profile, RFC 9457,
  Standard Webhooks) separately from the package versions in use.
- **R-8 Linked identifiers:** request, quote, order, task, event, receipt, audit checkpoint and trace carry each
  other's ids so one id leads to all the others (`GET /v1/orders/{id}` exposes `links`).

## Acceptance criteria
- **AC-1** The same order placed over REST and over MCP by the same agent produces the same order fields, one
  audit entry and one `order.*` event each, with equal policy outcome.
- **AC-2** Same idempotency key + same payload over REST then MCP → one order; same key + changed payload →
  `409 IDEMPOTENCY_CONFLICT`; another principal may use the same key string for its own command.
- **AC-3** A failing audit write rolls back the state change and the event (nothing half-written).
- **AC-4** An event has a monotonically increasing `aggregate_revision`; the outbox holds a pending row per
  registered consumer after commit.
- **AC-5** A request with `X-Correlation-Id` returns it, and the audit entry, event and order carry it.
- **AC-6** `GET /v1/meta` lists every specification with a pinned version or commit.
- **AC-7** A buyer_viewer credential resolves to its organisation and customers; a supplier_agent credential
  resolves to kind `supplier_agent`.
