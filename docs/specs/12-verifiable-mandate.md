# 12 — Verifiable mandate (the buyer grants and revokes authority)

Status: in progress · decided on 2026-11-14 to build it · Builds on [06](06-agent-mandate.md) and
[19](19-authentication.md) · Brief: preferred tech "verifiable credentials / trust layer".

## Purpose
Today the operator on the wholesaler side writes each agent's mandate into the gateway policy. Authority to
spend the hotel chain's money belongs to the hotel chain. With a verifiable mandate the buyer organisation signs
what its agent may do; the agent presents that document; the gateway checks it on every quote and order and
records which mandate was used. The buyer revokes it the same way.

## Requirements
- **R-1 Format:** a W3C Verifiable Credential (data model 2.0) secured as a JWT (VC-JOSE, `typ: vc+jwt`),
  signed EdDSA (Ed25519) by a buyer issuer key. Claims: `iss` (buyer issuer id), `sub` (agent id), `jti`
  (mandate id), `nbf`, `exp`, and `vc.credentialSubject` = `{agent_id, customer_id, max_auto_total_eur,
  max_auto_daily_total_eur, categories?}` with `vc.type = ["VerifiableCredential", "PurchasingMandate"]`.
- **R-2 Trusted issuers:** policy section `customers.<id>.mandate_issuers` lists the buyer's issuer id and public
  JWK(s). Registering an issuer is an operator action (onboarding), audited like any policy change.
- **R-3 Presentation:** REST: header `Agent-Mandate: <JWT>` together with the agent credential of spec 19. MCP
  stdio: `--mandate <file>`; MCP HTTP: the same header. The mandate's `sub` must equal the authenticated agent
  id (holder binding through the authenticated principal).
- **R-4 Verification on every lookup, quote, order and approval:** issuer registered for the mandate's customer,
  signature valid, `nbf ≤ now < exp`, not revoked, `sub` = caller, customer of the request = mandate customer,
  category (when given) of every line allowed. A failed check answers `403 MANDATE_INVALID` with the failed
  check named and is audited.
- **R-5 Effective authority:** a valid mandate grants the agent its customer and limits even without a policy
  row. A policy row for the agent can only tighten it: effective limits are the lower of mandate and policy.
  Without a mandate, the policy row of spec 06 applies as today.
- **R-6 Revocation:** `POST /v1/mandates/revocations` accepts a revocation JWT signed by the same issuer
  (`{iss, jti_revoked[], iat}`); the gateway stores the revoked ids; the next call with that mandate answers
  `403 MANDATE_INVALID (revoked)`. A pending order whose mandate was revoked cannot be approved (as spec 06
  AC-11).
- **R-7 Audit and objects:** every lookup, quote, order and approval records `mandate {id, issuer, limits,
  valid_until}`; orders expose it; `GET /v1/stats` `agents_seen[]` shows the mandate id in use.
- **R-8 Demo issuer:** `backend/scripts/issue_mandate.py` plays the hotel chain's admin: creates the buyer
  issuer key on first use (`backend/var/buyer-keys/`, outside the demo reset's archive step), prints the public
  JWK to register, issues a mandate (`--customer --agent --order-limit --daily-limit --days [--category ...]`)
  to a file, and issues revocations (`--revoke <jti>`). The README shows the three commands end to end.

## Acceptance criteria
- **AC-1** A valid mandate (daily 5,000, order 1,500) for an agent without a policy row: orders within the
  limits confirm; the order shows the mandate id.
- **AC-2** Expired, not-yet-valid, wrongly signed, unknown issuer, or `sub` ≠ caller → `403 MANDATE_INVALID`
  naming the check; audited.
- **AC-3** A mandate for customer "1" used for customer "2" → `403 MANDATE_INVALID (customer)`.
- **AC-4** Policy row with daily 2,000 and mandate daily 5,000 → the 2,000 limit applies; policy row with
  10,000 and mandate 5,000 → 5,000 applies.
- **AC-5** After a valid revocation the next quote with that mandate → `403 MANDATE_INVALID (revoked)`; a
  revocation signed by another key → `422`.
- **AC-6** Category-restricted mandate (dairy only): a line in another category → `403 MANDATE_INVALID
  (category)`.
- **AC-7** `issue_mandate.py` issues a mandate the gateway accepts and a revocation it honours (temp key
  folder).
- **AC-8** MCP stdio with `--mandate` places an order under the mandate's limits.

## For the review
Holder binding is through the authenticated agent credential; key-bound proof of possession (the agent signs
each request with a key named in the mandate) and OAuth-based MCP authorization are the next steps.
