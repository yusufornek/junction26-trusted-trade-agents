# 25 — Supplier adapters and a second supplier

Status: in progress · Decided on 2026-11-14 · Design reviewed with the external architect (round 4).

## Purpose
The gateway's rules are supplier-independent. A supplier sits behind an adapter that turns its API into
normalised facts; a second, differently shaped supplier proves the boundary.

## Requirements
- **R-1 `SupplierAdapter` interface:** `capabilities()`, `ingest_catalog()`, `price(customer, date, skus)`,
  `availability(customer, date, skus)`; **`OrderExecution` interface:** `submit(order)`, `status(ref)`,
  `cancel(ref)` with `mode: simulated | supplier_backed` and declared atomicity / partial-acceptance behaviour.
- **R-2 Normalised facts:** supplier SKU, GTIN, sale unit, conversion evidence, currency and tax basis, price
  validity, stock semantics (finite | unlimited | unknown), requested and returned dates, source evidence and
  observation time. Products are keyed by `(supplier_id, supplier_sku)`.
- **R-3 Valio adapter:** the existing client and catalog build move behind the interface unchanged in behaviour;
  order execution is the existing simulated implementation.
- **R-4 Second supplier "Nordfood" (synthetic, labelled as such everywhere):** a small HTTP service in
  `backend/suppliers/nordfood/` with different field names, case-based pricing (price per case, minimum order
  quantity), finite stock, another currency option (SEK alongside EUR) and explicit order acknowledgements
  (`POST /orders` → ack with status). Its own adapter; its catalog is a fixture of ~60 products with GTINs.
- **R-5 Supplier in the request:** `NeedSpec.supplier_id` (default `valio`) binds a procurement request to one
  supplier; lookups, quotes and orders carry it; policy and mandates may restrict suppliers per agent.
- **R-6 Order execution result on the order:** `execution {mode, supplier_ref, acknowledged_at, status}`;
  `commitment` is `gateway-simulated` or `supplier-acknowledged` accordingly.
- **R-7 Registry:** `GET /v1/suppliers` lists suppliers with capabilities and whether they are synthetic.

## Acceptance criteria
- **AC-1** The existing Valio demo flow gives the same results through the adapter (same best offer, total).
- **AC-2** The same reference need against `nordfood` returns ranked offers with case pricing converted to the
  requested unit and minimum quantities respected.
- **AC-3** A Nordfood order is submitted to its service and comes back acknowledged; the order shows
  `commitment: supplier-acknowledged` and the supplier reference.
- **AC-4** Nordfood timeout during submit → the order stays in a recoverable state and a retry with the same
  idempotency key does not create a second supplier order.
- **AC-5** SEK prices are refused for the EUR-only policy with a clear code, or converted only when the policy
  names a rate source (default: refused).
- **AC-6** GTIN shared by two packaging levels maps to the right sale unit per supplier.
- **AC-7** A mandate restricted to supplier `valio` is refused for `nordfood` (`403`).
- **AC-8** `GET /v1/suppliers` marks Nordfood as synthetic.
