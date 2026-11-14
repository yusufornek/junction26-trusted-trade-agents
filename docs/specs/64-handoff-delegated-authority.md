# 64 — Handoff and delegated authority

Status: in progress · Decided on 2026-11-14 with Gökalp (AI-gateway focus; drafted by Fable from Astra's round 2).
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
