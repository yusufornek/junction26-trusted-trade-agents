# 53 — Neutral ranking, private RFQ and supply watch

Status: in progress · Decided on 2026-11-14 with Gökalp (capability spec, see spec 47). Builds on specs 09
(price-complete shortlist), 25 (supplier adapters), 29 (evidence bundle) and 46 R-4 (stock-out watch).

## Purpose
The marketplace can show that its ranking serves the buyer, let a buyer ask several suppliers privately, and warn every
affected order when something upstream goes wrong.

## Requirements
- **R-1 Explainable ranking:** each ranked offer returns a score breakdown (every factor, weight and value) and the hash
  of the buyer's scoring policy; `POST /v1/rankings/{lookup_id}/replay` recomputes it from the stored inputs.
- **R-2 Affiliation-blind:** offers carry `commercial_affiliation` (marketplace owner, partner, independent) visible to
  the buyer and never used as a ranking input; replay with the affiliation changed returns the identical order.
- **R-3 Buyer scoring policy:** the buyer (tenant) can set weights for price, delivery reliability, CO2, stock coverage
  and fit within published bounds; the policy version is bound into quotes.
- **R-4 Private RFQ:** `POST /v1/rfqs {lines, deliver_by, invited_suppliers, closes_at}` sends a request to selected
  supplier adapters; bids are sealed until close (stored with their digest at submission), then ranked with the same
  scoring; one bid becomes a normal quote. Suppliers never see each other's bids.
- **R-5 Supply watch:** supply events (`SUPPLIER_DISRUPTION`, `PRODUCT_DISCONTINUED`, `RECALL_NOTICE`, `PRICE_SHOCK`)
  can be recorded by the operator or a supplier adapter, with affected suppliers, products or categories. The watch
  finds affected open intents, quotes, recurring orders and pools across tenants, raises one `SUPPLY_RISK` per affected
  item with a function-compatible alternative and trade-off (spec 46), and generalises the spec 46 stock-out watch.
- **R-6 Evidence:** ranking breakdown, policy hash and RFQ bid digests are part of the evidence bundle.
- **R-7 Identity events:** the supply watch also consumes spec 54 `PRODUCT_IDENTITY_CHANGED` and `SUCCESSOR_LINKED` events and raises `SUPPLY_RISK` on affected recurring orders and open quotes.

## Acceptance criteria
- **AC-1** An Aimo-affiliated offer that loses on the buyer's policy stays behind the independent offer; replay with
  affiliation toggled returns the identical order and scores.
- **AC-2** Changing the buyer's weights changes the order reproducibly, and quotes carry the policy version.
- **AC-3** An RFQ to two sandbox suppliers keeps bids sealed before close, ranks them after, and converts the winner
  into a quote.
- **AC-4** A `SUPPLIER_DISRUPTION` on one supplier raises `SUPPLY_RISK` on a recurring order and an open quote of two
  tenants, each with an alternative and trade-off.
- **AC-5** The evidence bundle of an order from an RFQ includes the bid digests and still verifies.
- **AC-6** A `SUCCESSOR_LINKED` event raises `SUPPLY_RISK` on a recurring order of the old product.
