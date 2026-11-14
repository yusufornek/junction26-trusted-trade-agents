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

## README: UBL 2.1 / Peppol BIS Ordering 3 mapping

Both interfaces use the same core: REST **OpenAPI 3.1.0** (the generated contract is
`contracts/openapi.yaml`) and **MCP protocol 2026-07-28** (SDK 2.3.0; negotiated
protocol versions are supported by the SDK). Exports target **OASIS UBL 2.1** Order
and OrderResponse and map the **Peppol BIS Ordering 3** business fields below.
Official XSD validation proves UBL structure; it does not certify Peppol business
rules, routing, party identity or transmission. Placeholder endpoint identifiers
make these demonstration documents unsuitable for delivery over Peppol.

| Gateway field / business field | UBL Order / OrderResponse mapping | Source or missing data |
|---|---|---|
| Standard / process identifiers | UBLVersionID = 2.1; CustomizationID = urn:fdc:peppol.eu:poacc:trns:order:3 or order_response:3; ProfileID = urn:fdc:peppol.eu:poacc:bis:ordering:3 | Peppol BIS Ordering 3 identifiers; no certification claim |
| Order.id / response ID | cbc:ID; response adds -response; OrderReference/ID identifies the order | Gateway-generated identifiers |
| Quote.id | QuotationDocumentReference/ID | Locked quote reference in Order |
| Order.created_at / decided_at | IssueDate, IssueTime | Creation for Order; decision time for OrderResponse |
| Order.currency | DocumentCurrencyCode and currencyID on amounts | Upstream price currency (EUR) |
| Quote.customer_id, Order.customer_id | BuyerCustomerParty/Party/PartyIdentification/ID; PartyName/Name | Internal customer identifier and display name; legal registered buyer name needs data from Valio Aimo |
| Seller identity | SellerSupplierParty/Party/PartyIdentification/ID; PartyName/Name | Explicit gateway-simulated identity/name; seller legal registration identifier and legal name need data from Valio Aimo |
| Buyer and seller electronic address (mandatory Peppol routing data) | BuyerCustomerParty and SellerSupplierParty / Party/EndpointID, schemeID=0088 | Literal placeholder: **needs data from Valio Aimo**; not an invented GLN |
| Party legal registration, postal addresses, country codes, VAT identifiers, contacts | PartyLegalEntity, PostalAddress/Country, PartyTaxScheme, Contact | needs data from Valio Aimo; omit unavailable fields, including conditional Peppol fields |
| Quote.deliver_by | Delivery/RequestedDeliveryPeriod/EndDate | Requested delivery deadline; delivery location/address and recipient identity need data from Valio Aimo |
| Line index | OrderLine/LineItem/ID | Stable 1-based line identifier |
| Line.product_id / GTIN | Item/SellersItemIdentification/ID; StandardItemIdentification/ID schemeID=0160 | GS1 salesUnitGtin; catalog also stores units[].gtin by unitId. Missing GTIN is omitted, seller item ID remains |
| Line.name | Item/Name | synkkaData.names[] (English, then Finnish) |
| Line.units_needed | LineItem/Quantity with unitCode | Sales-unit quantity, decimal string; unit mapping below |
| Line.unit_price | LineItem/Price/PriceAmount and BaseQuantity=1 with unitCode | Locked net price per sales unit; no price negotiation |
| Line.total / Order.total | LineExtensionAmount; AnticipatedMonetaryTotal (Order) or LegalMonetaryTotal (response): LineExtensionAmount, TaxExclusiveAmount | Locked net line and order totals |
| Tax / gross / payable | TaxTotal/TaxAmount, TaxInclusiveAmount, PayableAmount | Derive tax from upstream unit_price_with_tax minus locked net totals, round line gross to cents. Tax category, VAT percentage, exemption reason and seller VAT registration need data from Valio Aimo. If gross is missing, omit tax/gross and explicitly label payable as net in a Note |
| Allowances, charges, rounding, payment means/terms, accounting references, originator references | AllowanceCharge, monetary totals, PaymentMeans, PaymentTerms, AccountingCost, OriginatorDocumentReference | No gateway allowances/charges; other mandatory-if-applicable Peppol data needs data from Valio Aimo; never manufacture values |
| Order.status | OrderResponseCode: CONFIRMED=AP, REJECTED=RE, PENDING_APPROVAL=CA; CANCELLED/EXPIRED=RE | Gateway decision, not a supplier response |
| Order.policy.hits | Note per rule and detail | All policy rule hits; includes approval conditions |
| Order.commitment | Note: gateway-simulated, no supplier order transmitted | Explicit simulated commitment |

UN/ECE Recommendation 20 measures and Recommendation 21 package codes (X prefix):

| Catalog salesUnit | UBL unitCode | Meaning / mapping assumption |
|---|---|---|
| L, LTR | LTR | litre |
| KG | KGM | kilogram |
| ST | H87 | piece |
| KI, KRT | XCT | carton |
| RAS | XPU | tray |
| SK | XSA | sack |
| PAK, DSP | XPK | package; DSP uses generic package pending a precise packaging classification |
| BAG | XBG | bag |
| PRK | XJR | jar |
| BOT | XBO | bottle |
| NIP | XBE | bundle |
| CS | XCS | case |
| PAL | XPX | pallet |
| KAN | XCA | can |
| DZ | DZN | dozen |
| KLE | XBX | box |
| Unknown unit | H87 | fallback with a document Note; precise unit needs data from Valio Aimo |

All 17 sales-unit values in the supplied catalog are covered in `app/ubl.py`.
GS1 source vocabulary is retained: GTIN = `salesUnitGtin` and `units[].gtin`;
allergens = `synkkaData.classifications[name=allergen].values[].id/unit`;
free-from = `classifications[name=nonAllergen]`; claims =
`classifications[name=nutritionalClaim].values[].id`; packaging labels =
`classifications[name=packagingMarkedLabel].values[].id`. Missing facts are not proof.

GET `/v1/orders/{id}/ubl?document=order|response` (default: order) returns XML.
MCP `get_order_ubl(order_id, document)` returns the identical XML. Agents can export
only their own orders; the operator can export all orders. XSD tests use
`xmlschema` with local-only loading of `vendor/ubl-2.1/xsd/maindoc/`.
