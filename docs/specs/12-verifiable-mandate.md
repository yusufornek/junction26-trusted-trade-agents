# 12 — Verifiable mandate

Status: draft — for the post-MVP review · Builds on [06 — Agent mandate](06-agent-mandate.md) · Brief: optional
"verifiable-credential or trust-layer approaches".

## Purpose
Today a mandate is a row in the gateway's policy and the agent names itself in a header. In production the buyer
(the hotel chain) should be the one who grants and revokes authority, and the agent should prove it holds that
authority on every call.

## Requirements
- **R-1** A mandate is a signed document issued by the buyer: subject (agent key), customer, limits (per order,
  per day), allowed categories, validity period — as a W3C Verifiable Credential (JWT-VC) or a plain signed JWT
  with the same claims.
- **R-2** The agent presents the mandate when it connects (MCP: at session start; REST: bearer token) and signs
  requests with the key named in it; the gateway verifies issuer, signature, validity and revocation.
- **R-3** The gateway's policy can only tighten what the mandate allows (effective limit = the lower of the two).
- **R-4** The buyer can revoke a mandate; the next call from that agent is treated as unregistered.
- **R-5** The audit trail records the mandate id used for every quote and order.
- **R-6** MCP authorization follows the MCP specification's OAuth 2.1 flow, with the mandate as the grant.

## Acceptance criteria
- **AC-1** A valid mandate gives the limits it states; an expired, revoked or wrongly signed one gives
  `unregistered_agent` behaviour.
- **AC-2** A mandate for customer "1" cannot be used for customer "2".
- **AC-3** Policy limits lower than the mandate win; higher ones do not widen it.

## Open choices for the review
VC data model vs. plain JWT for the hackathon scope; who runs the issuer in the demo (a small "buyer admin" page).
