# 06 — Agent mandate (Core 3: what each agent may decide alone)

Status: built (verified live 2026-11-14) · defaults await the team review · Found in the core audit on 2026-11-14.

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
- **R-2 Unregistered agents can look and quote, a human decides their orders:** an order from an agent without
  a mandate always goes to `PENDING_APPROVAL` with rule hit `unregistered_agent`.
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
- **AC-2** Unregistered agent, same order → `PENDING_APPROVAL` with `unregistered_agent`.
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
- **AC-9** Over MCP the identity from `--agent-id` is the one checked (registered vs. not).
- **AC-10** Console flow: an order placed from the buyer page is recorded as `agent:web-console` and confirms within its mandate.

## For the review
Defaults chosen tonight and easy to change in `policy.yaml`: unregistered agents may quote but never
auto-confirm; daily window = rolling 24 h; daily limit 5,000 EUR. A signed mandate (verifiable credential)
remains a later step.
