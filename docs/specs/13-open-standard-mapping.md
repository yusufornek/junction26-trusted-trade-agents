# 13 — Open-standard mapping (path to production)

Status: in progress · decided on 2026-11-14 to build it · Judged as "Openness and credible path toward production" (15%).

## Purpose
Show that what the gateway produces maps onto standards European B2B trade already uses, so the prototype is a
step towards production and not a private format.

## Requirements
- **R-1** A mapping table (in this spec and in `backend/README.md`) from our `Quote` and `Order` fields to
  Peppol BIS Ordering 3 / UBL 2.1 `Order` and `OrderResponse` (ids, parties, delivery date, lines, item
  identification by GTIN, quantities with unit codes, prices, totals, tax).
- **R-2** `GET /v1/orders/{id}/ubl` returns the order as a UBL 2.1 Order XML document built from that mapping
  (GTINs from the catalog's `salesUnitGtin`, UN/ECE unit codes for L, KG and pieces).
- **R-3** Product facts use GS1 vocabulary where the catalog already carries it (GTIN, allergen codes, claims),
  and the README names the source field for each.
- **R-4** The MCP server and the OpenAPI contract are listed as the two open interfaces, with the version of
  each specification used.

## Acceptance criteria
- **AC-1** The UBL document for a confirmed order validates against the UBL 2.1 Order schema.
- **AC-2** Every mandatory Peppol BIS Ordering field is either mapped or listed as "needs data from Valio Aimo"
  (e.g. buyer and seller endpoint ids).
- **AC-3** Line GTINs, quantities and prices in the UBL document equal those of the order.

- **AC-4** A `REJECTED` or `CONFIRMED` order also exports a UBL 2.1 `OrderResponse` that validates against the
  schema, with the response code matching the decision.
- **AC-5** MCP tool `get_order_ubl` returns the same document for the agent's own order.

## Decisions (2026-11-14)
- Build the export. Schemas: the official OASIS UBL 2.1 XSDs vendored in `backend/vendor/ubl-2.1/`; tests
  validate with `xmlschema`.
- **R-5 GTIN in the catalog:** the catalog build stores `salesUnitGtin` (and each unit's `gtin`) per product;
  `OfferCard`, quote and order lines expose `gtin`.
- **R-6 OrderResponse:** `GET /v1/orders/{id}/ubl?document=order|response` (default `order`); the response
  document carries the decision (Peppol response codes: AP accepted, RE rejected, CA conditionally accepted for
  pending) and the rule hits as notes.
- **R-7 Unit codes:** UN/ECE Recommendation 20 for measures (LTR, KGM, H87) and Recommendation 21 package codes
  (X-prefixed, e.g. XCT for cartons) for sales units; a mapping table in code covers every sales unit seen in
  the catalog, unknown units fall back to H87 with a note in the mapping table.
- **R-8 Missing party data:** buyer and seller endpoint ids are not in the mock; the export uses
  `schemeID="0088"`-style placeholders marked `needs data from Valio Aimo` in the mapping table, never invented
  real identifiers.
- Access follows spec 19 (agent: own orders; operator: all).
