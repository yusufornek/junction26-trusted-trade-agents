# 10 — Signed quotes (trust protocol)

Status: in progress · decided on 2026-11-14 to build it · Brief: "the minimum data/verification standard for an external agent to
trust Valio Aimo's claims on stock, price and origin without a manual check."

## Purpose
A quote is the gateway's promise: these products, these fixed prices, valid until this time. An outside agent (and
the buyer's own systems) should be able to verify that the promise really came from the gateway and was not
altered on the way, without calling back.

## Requirements
- **R-1** Every quote carries `signature`: a detached JWS (RFC 7515, EdDSA / Ed25519) over the canonical JSON
  (RFC 8785) of `{id, customer_id, deliver_by, lines[product_id, units, unit_price, currency], total, expires_at}`.
- **R-2** The public key is published as a JWK Set at `GET /.well-known/jwks.json` with a key id; quotes name the
  `kid`. Keys are generated on first start and stored in `backend/var/`.
- **R-3** `place_order` verifies the stored quote against its signature before committing; a mismatch is
  refused (`QUOTE_SIGNATURE_INVALID`) and audited.
- **R-4** The MCP tool description and README explain how an agent verifies a quote offline.
- **R-5** Provenance of the underlying claims (price / availability source, fetched-at) stays inside the signed
  payload's lines, so the signature also covers where each number came from.

## Acceptance criteria
- **AC-1** A quote verifies with the published JWK; changing any signed field breaks verification.
- **AC-2** Tampering with a stored quote makes `place_order` answer `QUOTE_SIGNATURE_INVALID`.
- **AC-3** After a key rotation, old quotes verify with the old `kid` while it is still published.
- **AC-4** Contract and README document the signature and the JWKS endpoint.

- **AC-5** A confirmed order carries a signed receipt that verifies with the published JWK; a changed status or
  total breaks it.
- **AC-6** `scripts/verify_quote.py` verifies a saved quote JSON against a saved JWKS file with no gateway
  running, and reports which check failed (signature, expiry, customer).
- **AC-7** Quotes and orders show `price_commitment` / `commitment` with the values of R-8.

## Decisions (2026-11-14)
- **R-6 Receipts:** every order decision (CONFIRMED, REJECTED, CANCELLED) carries `receipt`: a detached JWS over
  the canonical JSON of `{order_id, quote_id, quote_signature_digest, customer_id, agent_id, status, total,
  currency, decided_at, decided_by_kind (agent | operator | system)}`.
- **R-7 Offline verifier:** `backend/scripts/verify_quote.py <quote.json> --jwks <file-or-url>
  [--customer ID] [--at ISO-time]` uses only the standard library and `cryptography`; exit code 0 when valid.
- **R-8 What is promised:** quotes carry `price_commitment: "gateway-locked"` (the gateway holds the price until
  `expires_at`); orders carry `commitment: "gateway-simulated"` (recorded in this gateway, no supplier order
  transmitted). Both values are inside the signed payloads and documented in the contract.
- Algorithm Ed25519 (EdDSA) via `cryptography`; canonical JSON per RFC 8785 with monetary amounts and
  quantities as decimal strings. Keys live in `backend/var/keys/` (outside the demo reset's archive step);
  `backend/scripts/rotate_key.py` adds a new active key and keeps the previous public key published for 7 days.
- The signature proves origin and integrity of the gateway's promise. The truth of stock, origin and product
  claims rests on the upstream sources named in each line's provenance.
