# 19 — Authentication and ownership

Status: in progress · Decided on 2026-11-14 after the external architecture review.

## Purpose
Mandates, approvals and the audit trail describe who did what. Today the caller states its own identity in a
header and any caller can approve orders or edit the policy. Credentials establish the principal; every rule
then applies to that principal.

## Requirements
- **R-1 Credentials from the environment:** `AGENT_KEYS` (`agent-id:secret` pairs, comma separated) and
  `OPERATOR_TOKEN`, read from the process environment / repo-root `.env.local`. The server keeps SHA-256
  digests in memory and compares in constant time. `backend/scripts/gen_keys.py --write` appends missing
  entries for the agents in the policy and the operator to `.env.local` without printing the values;
  `.env.example` documents the names.
- **R-2 Principals:** `Authorization: Bearer <secret>` resolves to `agent:<id>` or to the operator. The
  `X-Agent-Id` header has no effect on authority. The operator may use the buyer endpoints; its buyer actions
  run as the console's agent identity (`OPERATOR_AGENT_ID`, default `web-console`) under that agent's mandate.
- **R-3 Every business endpoint needs a credential:** a missing or unknown credential answers
  `401 UNAUTHENTICATED`. `GET /health`, `/docs` and `/openapi.json` stay open.
- **R-4 Agent scope:** an agent uses lookups, quotes and orders for customers in its mandate
  (`403 CUSTOMER_NOT_IN_MANDATE`, checked before any upstream call); an authenticated agent without a mandate
  answers `403 AGENT_NOT_REGISTERED` on lookup, quote and order. Lookups, quotes and orders record their
  agent; an agent reads, quotes from, orders from, cancels and follows the timeline of its own objects, and
  gets `404` for another agent's.
- **R-5 Operator scope:** approve, reject, cancel any order, `PUT /v1/policy`, policy reset, and the global
  views (approvals inbox, order list, audit log, stats, policy) need the operator credential; an agent
  credential answers `403 OPERATOR_REQUIRED`. Agents get their own orders from `GET /v1/orders`.
- **R-6 Honest actors:** operator actions are audited as `human:operator`; the typed name stays on the order
  (`approver`, `cancelled_by`, `updated_by`) and in the audit data as `stated_name`.
- **R-7 MCP:** over stdio the identity is the operator-provisioned `--agent-id` of the launched server. Over
  HTTP every request carries the launched agent's Bearer secret.
- **R-8 Secrets stay out of data:** credentials and digests appear in no response, policy document, audit
  entry or log line.
- **R-9 Startup:** without `OPERATOR_TOKEN` the API refuses to start and names `gen_keys.py`.
- **R-10 CORS** allows the `Authorization` header for the console origins.

## Acceptance criteria
- **AC-1** No credential on `POST /v1/offers`, `/v1/quotes`, `/v1/orders`, `GET /v1/orders`, `/v1/audit`,
  `/v1/policy` → `401 UNAUTHENTICATED`; `/health` → 200.
- **AC-2** Agent A's secret with header `X-Agent-Id: B` → the order is recorded and budgeted as A.
- **AC-3** Agent credential on approve, reject, `PUT /v1/policy`, policy reset, audit, stats →
  `403 OPERATOR_REQUIRED`.
- **AC-4** Agent A cannot read, order from, cancel or follow agent B's quote / order / lookup (`404`); the
  operator can read all.
- **AC-5** Agent with mandate for customer "1" looks up for customer "2" → `403 CUSTOMER_NOT_IN_MANDATE`, zero
  upstream calls, attempt audited.
- **AC-6** Authenticated agent without a mandate → `403 AGENT_NOT_REGISTERED` on lookup, quote and order.
- **AC-7** Operator approves → audit actor `human:operator`, `stated_name` kept; order `approver` shows the
  typed name.
- **AC-8** Operator credential on the buyer endpoints → actions recorded as `agent:web-console` under its
  mandate.
- **AC-9** No secret or digest in `GET /v1/policy`, the audit log or the OpenAPI examples.
- **AC-10** `gen_keys.py --write` on a temp env file adds the missing names once; a second run changes nothing.
- **AC-11** MCP over stdio keeps working with `--agent-id`; MCP over HTTP without the Bearer secret is refused.

## For the review
Prototype credential provisioning: shared secrets issued by the operator. Next steps on the path to
production: OAuth-based authorization for remote MCP, named operator accounts, buyer-issued signed mandates
(spec 12).
