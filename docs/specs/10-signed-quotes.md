# 10 — Signed quotes (trust protocol)

Status: draft — for the post-MVP review · Brief: "the minimum data/verification standard for an external agent to
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

## Open choices for the review
Ed25519 vs. ES256; sign orders and approvals too (receipts); how long retired keys stay published.
