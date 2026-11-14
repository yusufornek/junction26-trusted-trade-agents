# 23 — Machine-readable recovery contract

Status: in progress · Decided on 2026-11-14 · Design reviewed with the external architect (round 4).

## Purpose
An agent decides what to do after a refusal from data, not from prose: retry, repair, wait for a human, or stop.

## Requirements
- **R-1 Format:** REST errors are RFC 9457 `application/problem+json` (`type`, `title`, `status`, `detail`,
  `instance`) extended with `code`, `retryable`, `next_action`, `retry_after_seconds`, `resource_ref`,
  `correlation_id`, `violations[]` (field, problem, valid values) and the existing `data`. `code` and `message`
  stay available for existing clients.
- **R-2 Action vocabulary (versioned, `recovery_version: 1`):** `RETRY_SAME_COMMAND`, `REFRESH_QUOTE`,
  `LOOKUP_REQUIRED`, `WAIT_FOR_APPROVAL`, `REVIEW_UPDATED_EVIDENCE`, `FIX_REQUEST`, `CONTACT_ADMIN`, `STOP`.
- **R-3 Mapping:** one table in code maps every domain error code to `{retryable, next_action}`:
  NEED_REF_REQUIRED → LOOKUP_REQUIRED; EXPIRED_QUOTE → REFRESH_QUOTE; EVIDENCE_CHANGED → REVIEW_UPDATED_EVIDENCE;
  upstream 502/503/timeouts → RETRY_SAME_COMMAND (retryable, with interval); IDEMPOTENCY_CONFLICT → STOP;
  UNKNOWN_CODE / validation → FIX_REQUEST; CUSTOMER_NOT_IN_MANDATE, AGENT_NOT_REGISTERED, MANDATE_INVALID →
  CONTACT_ADMIN; RATE_LIMITED → RETRY_SAME_COMMAND with interval; hard rejections → STOP. A test fails when a
  DomainError code has no mapping.
- **R-4 Workflow states carry guidance too:** an order in `PENDING_APPROVAL` returns `next_action:
  WAIT_FOR_APPROVAL` and `poll_after_seconds`; a confirmed order returns `next_action: null`.
- **R-5 MCP:** domain failures are tool results with `isError: true` and the same structured payload; protocol
  errors stay for malformed protocol use. The A2A mapping is documented in spec 34.
- **R-6 Safe references:** `resource_ref` appears only for resources the caller may read; authentication failures
  give minimal public detail. Guidance describes authorised transitions and grants nothing.

## Acceptance criteria
- **AC-1** Every DomainError code used in the code base has a mapping (test enumerates them).
- **AC-2** Expired quote over REST → `application/problem+json` with `next_action: REFRESH_QUOTE`,
  `retryable: false`; the same failure over MCP → `isError` tool result with the same fields.
- **AC-3** Upstream timeout → `retryable: true`, `RETRY_SAME_COMMAND`, `retry_after_seconds` set.
- **AC-4** Pending order status → `WAIT_FOR_APPROVAL` + `poll_after_seconds`; after approval → no action.
- **AC-5** Unknown allergen code → `FIX_REQUEST` with `violations[0].valid_values`.
- **AC-6** 401 carries no `resource_ref`; a 404 for another agent's order carries none either.
- **AC-7** The reference client (spec 38) recovers from an expired quote, a throttle and a lost placement
  response by following `next_action` only.
