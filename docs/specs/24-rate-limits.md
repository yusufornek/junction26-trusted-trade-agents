# 24 — Per-agent rate limits

Status: in progress · Decided on 2026-11-14 · Design reviewed with the external architect (round 4).

## Purpose
One buyer agent cannot exhaust the price and stock APIs or starve other agents.

## Requirements
- **R-1 Token buckets per authenticated principal**, shared across REST, MCP and A2A, with separate costs:
  lookup (expensive), mutation (quote, order, cancel), status read (cheap). Limits in policy section
  `rate_limits {lookup, mutation, read: {capacity, refill_per_minute}}`, overridable per agent.
- **R-2 Before any external call:** the limit is checked before Decisions and supplier calls.
- **R-3 Answer:** `429` + `Retry-After` + the recovery payload (`RATE_LIMITED`, `RETRY_SAME_COMMAND`,
  `retry_after_seconds`); MCP and A2A return the same structured payload.
- **R-4 Replays stay cheap:** an exact idempotent replay returns the stored result at read cost.
- **R-5 Global upstream guard:** a global concurrency limit and bounded queue for supplier calls; overflow answers
  `503 UPSTREAM_BUSY` (retryable).
- **R-6 Unknown credentials** are limited per source address before authentication work.
- **R-7 Visibility:** `GET /v1/me` shows the caller's limits and remaining tokens; throttles are audited
  (`RATE_LIMITED`) and counted in stats. Limiter scope (in-process, single instance) is stated in `/v1/meta`.

## Acceptance criteria
- **AC-1** An agent exceeding the lookup bucket gets `429` with `Retry-After`; zero upstream calls for the
  refused request.
- **AC-2** While agent A is throttled on lookups, agent A can still read order status and agent B is unaffected.
- **AC-3** Buckets are shared: lookups over REST and MCP by the same agent draw from one bucket.
- **AC-4** An idempotent replay of an order placement succeeds while the mutation bucket is empty.
- **AC-5** After the refill interval the same request succeeds.
- **AC-6** Policy change of a limit takes effect on the next request and is audited.
- **AC-7** Global upstream concurrency above the bound → `503 UPSTREAM_BUSY`, retryable.
