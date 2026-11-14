# 36 — Proactive supplier proposals

Status: in progress · Decided on 2026-11-14 · Design reviewed with the external architect (round 4).

## Purpose
The brief's "proactive Valio Aimo sales agent": the supplier side proposes relevant offers to buyer agents, with
the buyer's consent, and every purchase still runs under the buyer's authority.

## Requirements
- **R-1 Buyer consent:** `GET/PUT /v1/buyer/proposal-preferences` (buyer_admin): opt-in categories and
  interests, channels (inbox, webhook), cadence limit per week, quiet periods, muted campaigns.
- **R-2 Proposals:** a `supplier_agent` principal submits `POST /v1/proposals {campaign_id, products[],
  rationale, price evidence or quote_ref, valid_until, target {categories | customer_ids}}`.
- **R-3 Gateway filtering:** per targeted customer the gateway keeps only products that pass the customer's
  standing constraints and evidence rules, checks consent, cadence, quiet periods and duplicates of an equivalent
  campaign, and records why a proposal was not delivered.
- **R-4 Delivery:** to the buyer's proposal inbox `GET /v1/proposals/inbox` (agent and buyer users of that
  customer), MCP tool `list_proposals`, and `proposal.received` webhooks.
- **R-5 Actions:** `POST /v1/proposals/{id}/accept | decline | mute`. Accept starts a normal lookup → quote →
  order under the buyer agent's mandate with price and stock revalidated; declining and muting are recorded.
- **R-6 Information boundary:** the supplier agent sees delivery counts and accept / decline totals per
  campaign; buyer budgets, limits and preferences stay in buyer views. Supplier-written text is content only;
  permissions come from policy and mandates.
- **R-7 Quotas:** per supplier and per customer proposal quotas in the policy.
- **R-8 Demo:** `backend/demo/run_sales_agent.py` proposes a lactose-free campaign to the hotel chain.

## Acceptance criteria
- **AC-1** An opted-in customer receives a relevant proposal in its inbox; a customer without opt-in receives
  none, and the reason is recorded.
- **AC-2** A proposal product violating the customer's standing allergen exclusion is removed before delivery.
- **AC-3** Accept → order under the buyer agent's limits: above the limit it goes to approval like any order.
- **AC-4** Accept after `valid_until` or after the price changed → refused or re-quoted with the new price shown.
- **AC-5** The cadence limit holds: the third proposal in a week with limit 2 is held back.
- **AC-6** Mute stops further proposals of that campaign; equivalent duplicate campaigns are merged.
- **AC-7** The supplier agent cannot read buyer spend, limits or preferences (`403`).
- **AC-8** A buyer agent credential cannot submit proposals.
