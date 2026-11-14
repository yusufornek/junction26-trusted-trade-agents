# 29 — Downloadable transaction evidence

Status: in progress · Decided on 2026-11-14 · Design reviewed with the external architect (round 4).

## Purpose
A buyer takes away one portable record of a purchase that another party can check without trusting the gateway's
user interface.

## Requirements
- **R-1 `GET /v1/orders/{id}/evidence`** returns a ZIP; `?format=json` returns the manifest alone; MCP tool
  `get_order_evidence` returns the manifest with artifact links.
- **R-2 Contents:** `manifest.json` (schema version, order / customer / environment binding, file list with
  SHA-256), signed quote, signed receipt, the order's audit entries with their chain hashes and the hash-only
  links needed to connect them to the signed checkpoint (entries of other customers appear as hashes only),
  signed checkpoint, UBL Order and OrderResponse, anchor metadata (chain id, transaction, checkpoint digest,
  confirmation status) when present, the JWKS used, and `VERIFY.md` with the commands.
- **R-3 Verifier:** `backend/scripts/verify_evidence.py <bundle.zip> --trusted-jwks <file-or-url>
  [--rpc-url URL] [--xsd-dir DIR]` reports each check separately: `signature_valid`, `trusted_issuer_match`
  (keys come from the configured trusted source, the bundled JWKS is only compared), `audit_consistent`,
  `anchor_status` (matched | pending | not_checked), `ubl_schema_valid`, `supplier_acceptance`
  (`simulated` or `acknowledged`). It states which checks ran offline and which used the network. Exit code 0
  only when every executed check passes.
- **R-4 Historical keys:** retired verification keys stay available so old bundles verify.
- **R-5 Access:** the order's agent, its buyer organisation and the operator.

## Acceptance criteria
- **AC-1** A bundle for a confirmed order verifies: every check reported, `supplier_acceptance: simulated`.
- **AC-2** Changing a quantity in the quote file → `signature_valid: false` naming the artifact; exit code ≠ 0.
- **AC-3** Changing an audit entry → `audit_consistent: false` naming the entry.
- **AC-4** A bundle verified against an untrusted JWKS → `trusted_issuer_match: false` even though the bundled
  key verifies the signature.
- **AC-5** Without `--rpc-url` the anchor check reports `not_checked`; with a mocked RPC and a matching
  transaction it reports `matched`.
- **AC-6** The bundle contains no data of other customers (only hashes).
- **AC-7** Another agent requesting the bundle gets `404`.
