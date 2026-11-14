# 06 — Agent mandate (Core 3: what each agent may decide alone)

Status: built (verified live 2026-11-14) · R-2 revised and verified live 2026-11-14 (`external`: quote 200, order 403, attempt audited; `web-console`: CONFIRMED) · Found in the core audit on 2026-11-14.

## Purpose
Today one policy applies to every caller and the spend limit is per order. Two gaps follow: an unknown agent has
the same authority as a known one, and any agent can stay under the limit by splitting one large order into
several small ones. A mandate says, per agent, on whose behalf it buys and how much it may commit on its own.

## Requirements
- **R-1 Mandates in the policy:** new policy section `agents`: a map `agent_id → mandate` with
  `customer_ids` (list of customers it may act for), `max_auto_total_eur` (optional, per order; the effective
  limit is the lower of this and `approval.max_auto_total_eur`), `max_auto_daily_total_eur` (cumulative, see R-3)
  and `note` (free text, who granted it). Defaults in `backend/policy.yaml` register `hotel-chain-agent` and
  `web-console` (the operator console's own buyer flow) for customer `"1"` with a daily limit of 5,000 EUR.
- **R-2 Ordering requires a mandate** (team decision 2026-11-14; tightened by spec 19: an authenticated agent
  without a mandate answers `403 AGENT_NOT_REGISTERED` on lookup, quote and order, a missing credential answers
  `401`). Before spec 19 an agent without a mandate could look up offers and get quotes. `place_order` from such an agent answers `403 AGENT_NOT_REGISTERED`, creates no order and no
  reservation, and is audited as a failed attempt under `agent:<id>`. Policy previews (`plans[].policy_preview`)
  for such an agent show outcome `REJECTED` with rule hit `unregistered_agent`, so the agent learns it before
  quoting. An idempotent retry of an order that already exists returns that order. A pending order whose agent
  lost its mandate after placement answers `403 AGENT_NOT_REGISTERED` (with the rule hits) on approve; a human
  can reject it.
- **R-3 Cumulative limit:** when the sum of this agent's orders that were confirmed without a human in the last
  24 hours (cancelled ones excluded) plus the new order's total exceeds `max_auto_daily_total_eur`, the order
  goes to `PENDING_APPROVAL` with rule hit `max_auto_daily_total_eur` and the numbers in `detail`.
- **R-4 Customer scope:** a registered agent that quotes or orders for a customer outside `customer_ids` gets
  `403 CUSTOMER_NOT_IN_MANDATE` (audited as a failed attempt).
- **R-5 Per-agent order limit:** rule hit `max_auto_total_eur` reports the effective limit and whether it comes
  from the mandate or the global policy.
- **R-6 Governed like the rest of the policy:** `GET /v1/policy` returns `agents`; `PUT /v1/policy` accepts it
  (validated: non-empty ids, limits ≥ 0, at least one customer) and audits changes in the `POLICY_UPDATED` diff.
  A `PUT` body without `agents` keeps the current mandates, so existing clients keep working.
- **R-7 Identity:** REST `X-Agent-Id` (default `external`, which has no mandate); MCP identity from `--agent-id`.
  `POST /v1/offers` also accepts `X-Agent-Id` so lookups are attributed to the agent in the audit trail.
- **R-8 Stats:** `agents_seen` marks each agent as registered or not.
- **R-9 Console:** the operator console already sends `X-Agent-Id: web-console` for its buyer flow; that id is
  registered in the default mandates, so console orders are attributed and governed like any other agent.

## Acceptance criteria
- **AC-1** Registered agent, order within all limits → `CONFIRMED`, no hits (unchanged behaviour).
- **AC-2** Unregistered agent: lookup and quote succeed, the plan preview shows `REJECTED` with
  `unregistered_agent`; `place_order` → `403 AGENT_NOT_REGISTERED`, no order row, no reservation, the failed
  attempt is in the audit log under `agent:<id>`.
- **AC-3** Registered agent with daily limit 2,500: first order 1,400 → `CONFIRMED`; second order 1,400 →
  `PENDING_APPROVAL` with `max_auto_daily_total_eur` ("1,400 + 1,400 exceeds 2,500").
- **AC-4** After the first order is cancelled, the same second order → `CONFIRMED`.
- **AC-5** Orders approved by a human do not count towards the autonomous daily total.
- **AC-6** Mandate with `max_auto_total_eur: 500` and global 1,500: an 800 EUR order → `PENDING_APPROVAL`, the
  hit names the mandate limit.
- **AC-7** Registered agent quoting for customer "2" with `customer_ids: ["1"]` → `403 CUSTOMER_NOT_IN_MANDATE`,
  audited.
- **AC-8** `PUT /v1/policy` without `agents` keeps mandates; with a changed mandate the audit diff shows the
  field; invalid mandates → `422`.
- **AC-9** Over MCP the identity from `--agent-id` is the one checked: a registered identity confirms, an
  unregistered one gets `AGENT_NOT_REGISTERED` from `place_order`.
- **AC-10** Console flow: an order placed from the buyer page is recorded as `agent:web-console` and confirms within its mandate.

- **AC-11** A `PENDING_APPROVAL` order whose agent's mandate was removed afterwards: approve →
  `403 AGENT_NOT_REGISTERED` carrying the `unregistered_agent` hit, the order stays pending; reject works.

## For the review
Defaults easy to change in `policy.yaml`: daily window = rolling 24 h; daily limit 5,000 EUR. Decided on
2026-11-14: ordering requires a mandate (R-2). A signed mandate (verifiable credential) remains a later step
(spec 12).
