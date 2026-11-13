# 13 — Open-standard mapping (path to production)

Status: draft — for the post-MVP review · Judged as "Openness and credible path toward production" (15%).

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

## Open choices for the review
Whether to build the XML export for the demo or keep the mapping table only; OrderResponse for approvals.
