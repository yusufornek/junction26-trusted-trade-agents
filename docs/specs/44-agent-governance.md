# 44 — Agent governance inside a buyer business

Status: in progress · Decided on 2026-11-14 with Gökalp. Builds on specs 40 (graduated autonomy), 42 (agent
connections) and 43 (accounts, tenants, audiences).

## Purpose
A buyer business knows, at any moment, which of its people runs which agent, what each agent did, and under which
rules. People shape the rules of their own agents; admins set the boundaries, review loosening changes and can stop
any agent. Every rule an agent obeys traces back to a person and a version.

## Roles (extends spec 43)
- `member` (new): a person who runs agents. Creates agents and connections that it owns, edits its own agents'
  policies inside the tenant guardrails, pauses its own agents, sees their activity.
- `tenant_admin`: everything a member can do for every agent of the tenant, plus tenant guardrails, review of policy
  change requests, ownership transfer, tenant-wide stop.
- `approver` and `viewer` keep their spec 43 meaning; an approver can also be a member.

## Requirements
- **R-1 Ownership:** every agent has exactly one `owner_user_id` in its tenant. Agents created through spec 42
  connections by an operator, and the seeded agents, are owned by the tenant's first `tenant_admin`. A member sees and
  manages only the agents it owns; `tenant_admin` sees all of the tenant's agents.
- **R-2 Three policy layers:** platform policy (Valio, `platform_admin`) ⊇ tenant guardrails (`tenant_admin`) ⊇ agent
  policy (owner). Agent policy fields: allowed customers, allowed operations (lookup, quote, order), per-order
  ceiling, daily and monthly budget, allowed and blocked product categories, required claims and excluded allergens,
  substitutes allowed or not, ordering hours and delivery-day window, autonomy level (spec 40), and approval routing
  (which approvers, optional amount threshold above which a `tenant_admin` must approve).
- **R-3 Effective policy:** the gateway enforces the most restrictive value across the three layers.
  `GET /v1/tenant/agents/{id}/policy/effective` returns every field with its value and its source layer, version and
  author ("daily budget 800 € — tenant guardrail v4, set by <user>").
- **R-4 Change control:** an owner's change that only tightens the agent policy applies immediately. A change that
  loosens it (a higher limit, a new customer, category or operation, fewer exclusions, a higher autonomy level) and
  stays inside the guardrails becomes a policy change request for a `tenant_admin`; the requester cannot approve its
  own request. A value beyond the tenant guardrails answers `422 OUTSIDE_GUARDRAIL` with the limit and its source.
  Guardrail changes by a `tenant_admin` that loosen beyond the platform policy answer the same.
- **R-5 Versions:** every agent policy and guardrail change creates an immutable version (author, time, reason,
  diff). List versions, read a version, and roll back to an earlier version (a rollback that loosens follows R-4).
  Lookups, quotes and orders record the effective policy version they ran under.
- **R-6 Simulation:** `POST /v1/tenant/agents/{id}/policy/simulate` applies a proposed policy to the agent's last
  30 days of quotes and orders and returns how many would have passed, needed approval or been refused, with
  examples. The change request shows this result to the reviewer.
- **R-7 Agent register:** `GET /v1/tenant/agents` lists for each agent: owner, status (active, paused, disabled),
  connections and clients, autonomy level, effective budget use this day and month, orders, approvals asked and
  granted, refusals by code, last activity. Filterable by owner and status.
- **R-8 Activity:** `GET /v1/tenant/agents/{id}/activity` returns the agent's audit entries (lookups, quotes,
  orders, refusals, policy changes, pauses) newest first, with the policy version for each.
- **R-9 Stop:** the owner pauses or resumes its own agent; a `tenant_admin` pauses or resumes any agent of the
  tenant and can pause every agent of the tenant at once; a `platform_admin` can pause a tenant. A paused agent's
  calls answer `423 AGENT_PAUSED` with who paused it and when; pending approvals stay; open quotes cannot be
  ordered.
- **R-10 People leaving:** disabling or removing a user pauses the agents it owns and lists them for the admin to
  transfer. `POST /v1/tenant/agents/{id}/transfer` moves ownership to another member and keeps history.
- **R-11 Alerts:** the gateway raises tenant alerts for budget use at 80 % and 100 %, a burst of refusals (5 within
  10 minutes), activity outside the agent's ordering hours, a new client or address on a connection, and pending
  change requests. `GET /v1/tenant/alerts` lists them; the tenant webhook (spec 43) delivers them.
- **R-12 Governance report:** `GET /v1/tenant/governance/report?from&to` returns per agent: owner, policy versions in
  force, orders and spend, approvals with approver, refusals, pauses and changes — as JSON and as a signed export
  (spec 10 signing).
- **R-13 Every surface:** the register, effective policy, change requests, pause and alerts are available to the
  tenant audience on REST, MCP (`/mcp/tenant`), A2A and webhooks (spec 43 R-17); decisions follow spec 43 R-18.
- **R-14 Audit:** policy changes, change-request decisions, rollbacks, pauses, resumes and transfers are audited with
  actor, tenant, agent and version.

## Acceptance criteria
- **AC-1** A member lists only its own agents; a `tenant_admin` lists all of the tenant's agents with owners.
- **AC-2** Effective policy takes the lowest daily budget across platform, tenant and agent and names its source
  layer, version and author.
- **AC-3** An owner lowers its agent's per-order ceiling → applies immediately, new version recorded.
- **AC-4** An owner raises the ceiling inside the guardrail → change request; the owner cannot approve it; a
  `tenant_admin` approves; the new version applies to the next quote.
- **AC-5** A value above the tenant guardrail → `422 OUTSIDE_GUARDRAIL` naming the limit and its source.
- **AC-6** Rollback to an earlier, looser version creates a change request; to a tighter version applies at once.
- **AC-7** Orders and quotes record the effective policy version; the activity feed shows it.
- **AC-8** Simulation of a lower budget over seeded history reports orders that would have needed approval.
- **AC-9** Paused agent → `423 AGENT_PAUSED` on lookup, quote and order over REST and MCP; resume restores access;
  tenant-wide pause stops every agent of that tenant and no agent of another tenant.
- **AC-10** Disabling a user pauses its agents; transfer moves ownership and keeps the history.
- **AC-11** Budget at 80 % raises an alert and delivers a signed tenant webhook event.
- **AC-12** Governance report lists orders with approver and the policy versions in force, and its signature
  verifies against the JWKS.
- **AC-13** A member of tenant A cannot read, change or pause tenant B's agents (`404`).
- **AC-14** The demo path (customer 1, 1000 L lactose-free milk → 464306, 1,010 €, CONFIRMED for
  `hotel-chain-agent`) is unchanged.
