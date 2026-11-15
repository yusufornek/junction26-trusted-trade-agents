# 64 — Handoff and delegated authority

Status: built · Decided on 2026-11-14 with Gökalp (AI-gateway focus; drafted by Fable from Astra's round 2).
Builds on specs 06, 12, 34, 44, 47 (intents, resume) and 62 R-4.

## Purpose
Work can move between agents and be shared with helper agents without authority moving with it. Every actor uses its
own identity, the effective authority is the intersection along the chain, and work in flight always has exactly one
holder that a successor can find. Covers agent switches mid-order, client restarts, agents calling agents, and borrowed
specialists.

## Requirements
- **R-1 Checkpoint:** `GET /v1/intents/{id}/checkpoint` returns a transport-independent state: intent version and state,
  pending actions, artifact references (offer refs, quote, order, pending approvals, open questions). No credentials.
- **R-2 Handoff grant:** the holder or the owner creates `POST /v1/intents/{id}/handoff {to_agent_id, mode:
  prepare_only | may_commit, expires_at}` and receives a single-use code. The recipient authenticates with its own
  credential and accepts at `POST /v1/handoffs/{code}/accept`. Effective authority is recipient mandate ∩ intent
  envelope ∩ mode. `may_commit` is chosen by the owner (human question, spec 60 R-5).
- **R-3 Exclusive holder:** an intent has one holder; accept is a compare-and-set. The previous holder's state-changing
  calls answer `409 INTENT_HANDED_OVER` with the new holder; reads stay.
- **R-4 Open work discovery:** intents and A2A tasks belong to tenant + agent, never to a connection or session.
  `GET /v1/intents?state=open`, `luotsi://intents/open` and A2A task listing let any connection of that agent find and
  reattach to its work. `POSSIBLE_DUPLICATE_DEMAND` (47 R-10) names the open intent to resume.
- **R-5 Delegation grant:** `POST /v1/delegations {to_agent_id, intent_id, operations, expires_at, max_depth}` issues an
  attenuated credential with its own identity. Scope is delegator's effective policy ∩ grant ∩ delegate's mandate.
  Default operations are read-only research (lookup, product detail, RFQ read); order needs explicit owner approval.
  `max_depth` defaults to 1; a further hop is a child grant with intersected scope, allowed only inside the depth.
- **R-6 Actor chain:** every call records `actor_chain` from original requester to immediate caller, taken from the
  delegation credential or a declared `on_behalf_of` (REST header, MCP `_meta`, A2A metadata) naming a principal of the
  same tenant. Authority is the intersection across the chain. An unknown principal answers
  `403 UNKNOWN_PRINCIPAL_IN_CHAIN`. Undeclared intermediaries stay bounded by the caller's own mandate and limits
  and the demand fingerprint (47).
- **R-7 Revocation cascade:** pausing or revoking a parent, or expiry of a grant, invalidates all children at their next
  call (62 R-4).
- **R-8 Surfaces and audit:** MCP tools `handoff_intent`, `accept_handoff`, `delegate_task`; A2A keeps the context id
  across the handoff; webhook `INTENT_HANDOVER`. The timeline and evidence bundle show the chain and each grant.

## Acceptance criteria
- **AC-1** A basket prepared over MCP by agent A is handed to agent B, which accepts over A2A and completes the order;
  A's later order attempt answers `INTENT_HANDED_OVER`; exactly one order exists.
- **AC-2** With `prepare_only`, B can quote but its order answers `403 HANDOFF_MODE`; a recipient whose mandate excludes
  the category is refused at accept.
- **AC-3** A handoff code used twice, or after expiry, is refused.
- **AC-4** A client restarted with a fresh session lists its open intent and continues it; starting the same demand anew
  answers `POSSIBLE_DUPLICATE_DEMAND` pointing at the open intent.
- **AC-5** A specialist agent with a research delegation reads pack options for the intent; its order attempt answers
  `403 OUTSIDE_DELEGATION`; its attempt to delegate one hop further is refused at `max_depth` 1; the timeline shows the
  chain.
- **AC-6** A restricted agent calling through a privileged agent with `on_behalf_of` gets the restricted agent's limits;
  pausing the delegator stops the delegate's next call.
- **AC-7** The demo path (464306, 1,010 €, CONFIRMED) is unchanged.


## Implementation

The capability lives in `backend/app/handoff_delegated_authority.py` (durable grants, checkpoints and credentials),
`handoff_delegated_hooks.py` (policy meets, holder guards and live authority), and `handoff_delegated_surfaces.py`
(REST, native MCP SDK tools/resources and A2A SDK task adapters). Shared files contain registration or narrow hooks.
No new dependency or environment variable is needed.

Shared integration hooks: `backend/app/service.py`, `auth.py`, `api.py`, `mcp_server.py`, `audit.py`,
`evidence.py`, `orders.py`, `foundation.py`, `agent_surface.py`, `a2a.py`, and `visibility.py`.
The only other changed files are the new acceptance test, this spec, and task 64 in `docs/TASKS.md`.

REST additionally exposes `/v1/handoffs/{id}/approve` for a pending commit grant and
`/v1/delegations/{id}/revoke` for cascading withdrawal. The existing human confirmation flow is used for
owner-issued commit handoffs, commit-grant approval and order delegation. The `/v1/tenant/intents/{id}/handoff`,
`/v1/tenant/handoffs/{id}/approve`, and `/v1/tenant/delegations` aliases expose these owner operations
under tenant credential scopes. Human MCP adapters retain native SDK
elicitation and request-state binding. Agent assertions cannot stand in for an authenticated human owner.

## Decisions taken

- A holder can prepare a `may_commit` handoff, but acceptance waits for its human owner (or existing platform
  administrator authority) to confirm the exact grant. `prepare_only` needs no additional owner decision.
- Handoffs bind the current holder and intent version. Any intervening buying-state revision invalidates an
  unaccepted grant. Codes are random, stored only as digests, recipient-bound, expiring and single-use.
- The intent envelope preserves the granting policy and signed mandate limits. Live recipient/delegate policy
  can only narrow that envelope. Declared ancestors and helper purchases share their ancestors’ spend; rolling
  autonomous budgets retain their separate 24-hour semantics, alongside calendar governance budgets.
- `max_depth` counts remaining credential hops including the new recipient. A default grant consumes its sole
  hop; an explicit larger depth permits children only inside the remaining scope, depth and expiry.
  Order authority needs a confirmed owner grant for the named helper; helper claims cannot approve it.
- `on_behalf_of` names tenant-registered agent principals, in original-requester-to-caller order; REST accepts
  `On-Behalf-Of`, MCP uses `_meta`, and A2A uses metadata. Credential ancestry cannot be removed by a declaration.
  Chains are bound into lookup/quote context so dropping metadata at placement cannot restore authority.
- Spec 47’s existing same-tenant, mandate-rechecked `resume` remains compatible before an explicit handoff.
  After an explicit handoff it is holder-only and cannot bypass the single-use grant or handoff mode.
  Internal supplier recovery rechecks the current holder without creating another commercial order.
- A2A task ownership moves with the intent, keeping its existing task/context ID. Connection and session IDs
  never own the work. Codes and helper credentials are delivered once and excluded from persisted task history,
  checkpoints and audit payloads.
- Existing procurement audit events keep their counts and meanings. Every authenticated surface call records
  its chain in a separate tenant-bound call ledger; the signed evidence manifest includes related call records.
  Grant events and their chains appear in both the purchase timeline and the original tamper-evident audit chain.
  Call records use a separate SQLite file beside the application database so task reads cannot wait behind
  a purchasing transaction that is blocked on supplier I/O.

## Acceptance mapping

Exactly seven tests live in `backend/tests/test_spec64_handoff_delegated_authority.py`:

| AC | Test |
|---|---|
| AC-1 | `test_AC_1_mcp_to_a2a_exclusive_handoff_one_order` |
| AC-2 | `test_AC_2_prepare_only_and_recipient_category_intersection` |
| AC-3 | `test_AC_3_handoff_single_use_expiry_and_stale_cas` |
| AC-4 | `test_AC_4_fresh_session_open_work_and_duplicate_resume` |
| AC-5 | `test_AC_5_research_credential_scope_depth_and_chain` |
| AC-6 | `test_AC_6_declared_chain_intersects_limits_and_parent_pause_cascades` |
| AC-7 | `test_AC_7_demo_464306_1010_confirmed_unchanged` |

Generated MCP/REST artifacts are intentionally left to merge regeneration. Verification details follow below.

## Verification (offline, 2026-11-15)

- Final capability + prior intent acceptance: **16 passed** (7 spec-64 tests, 9 spec-47 tests).
  AC-1 includes a real existing A2A task/context, signed mocked webhook delivery and evidence export.
  AC-5 also proves an explicitly owner-approved helper can order. AC-6 covers metadata removal between
  quote and order, cumulative ancestor spending, a child grant, pause/resume, revocation and expiry.
- Focused shared-module run: **257 passed, 4 failed** across the two acceptance files and
  `test_authentication.py`, `test_api_mcp.py`, `test_audit_chain.py`, `test_orders_policy.py`,
  `test_order_timeline.py`, `test_evidence_bundle.py`, `test_a2a_service.py`,
  `test_shared_foundation.py`, and `test_spec60_agent_surface.py`. This was not the full suite.
- The A2A streaming failure exposed call-ledger writes waiting behind an in-flight purchasing writer.
  The separate call ledger fixes it; the final dedicated SDK streaming/outbox/replay/push/cancel recheck
  is **1 passed, 31 deselected**. The final 16 acceptance tests were rerun after that fix and pass.
- Python AST parsing passed for all 15 changed/new Python files. No linter/build configuration exists
  under `backend/`; no dependencies were installed.

Remaining compatibility checks (not reported as passing):

1. `test_authentication.py::test_ac_1_credentials_and_startup`: the existing spec-63 strict request
   boundary returns `422 ARGUMENT_REQUIRED` for an unauthenticated empty `/v1/sandbox/reset` body, while
   the historical matrix expects authentication to precede body validation (`401`). This boundary is
   outside the handoff modules and was left unchanged.
2. `test_api_mcp.py::test_contract_all_routes_have_response_schema`: its old route-prefix allowlist
   does not admit the new `/v1/handoffs` and `/v1/delegations` namespaces.
3. `test_order_timeline.py::test_timeline_ac5_unknown_order_and_generated_contract`: generated OpenAPI
   differs from the expanded runtime schema. `contracts/openapi.yaml`, `backend/static/tools.json` and
   `backend/static/developers.html` are intentionally not edited and need merge regeneration.

Run the focused acceptance checks from `backend/` with the supplied Python environment:

```sh
/Users/daption-ciray/Desktop/junction/.claude/worktrees/selam-junction-challenge-44cf24/backend/.venv/bin/python -m pytest tests/test_spec64_handoff_delegated_authority.py tests/test_spec47_purchase_intent_integrity.py -q
```

Final check after scope enforcement and tenant MCP owner aliases: **17 passed, 31 deselected**
(spec 64 + spec 47 + the dedicated SDK streaming regression), in 32.99 s.

```sh
/Users/daption-ciray/Desktop/junction/.claude/worktrees/selam-junction-challenge-44cf24/backend/.venv/bin/python -m pytest tests/test_spec64_handoff_delegated_authority.py tests/test_spec47_purchase_intent_integrity.py tests/test_a2a_service.py -k "spec64 or spec47 or test_ac_6_sdk_streaming_outbox_replay_push_and_cancel" -q --assert=plain --tb=short
```
