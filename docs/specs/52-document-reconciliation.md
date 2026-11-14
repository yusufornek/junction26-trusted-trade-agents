# 52 — Document reconciliation

Status: in progress · Decided on 2026-11-14 with Gökalp (capability spec, see spec 47). Builds on specs 10 (signed
quotes), 13 (UBL mapping) and 50 (commitment ceiling).

## Purpose
After the order, every document that describes the deal — confirmation, delivery note, receiving report, invoice — is
matched line by line against what was authorised, and every difference becomes an explicit, signed decision.

## Requirements
- **R-1 Documents:** an order collects documents of types `confirmation`, `delivery_note`, `receiving_report`,
  `invoice`, `credit_note` (`POST /v1/orders/{id}/documents`; UBL 2.1 invoice/credit note or the gateway's JSON form).
  The sandbox supplier produces delivery notes and invoices, including fixtures with discrepancies.
- **R-2 Receiving:** a receiving report records per line `received`, `accepted`, `held` and reason codes
  (`DAMAGED_PACKAGING`, `MISSING`, `TEMPERATURE`, `WRONG_ITEM`, `SHORT_SHELF_LIFE`) and returned assets (spec 51).
- **R-3 Reconciliation:** one deterministic matcher compares, per line, ordered vs confirmed vs delivered vs received vs
  accepted vs invoiced (quantity, unit price, charges, tax rate) with explicit rounding tolerance, and calls the
  commitment check (spec 50) for invoice amounts. Result per order: `MATCHED`, `ACCEPTED_PARTIAL` or `DISPUTED`, with
  each discrepancy typed.
- **R-4 Claims and disputes:** discrepancies produce a signed claim or invoice dispute draft (replacement, credit or
  corrected invoice proposal; no payment), sent to the sandbox supplier, which returns a signed acknowledgement; a
  corrected document re-runs the matcher. Open obligations stay visible until resolved.
- **R-5 Surfaces:** REST and MCP tools (`submit_receiving_report`, `submit_invoice`, `get_reconciliation`,
  `create_claim`); webhooks `CLAIM_ACKNOWLEDGED`, `INVOICE_DISPUTED`; documents and decisions in the evidence bundle
  (spec 29).
- **R-6 Receipt is a buyer act:** a carrier "delivered" sets `DELIVERED_UNCONFIRMED`; only a receiving report from the order's delivery point closes the order and increases on-hand stock; a different delivery point is discrepancy `WRONG_DELIVERY_POINT`; unconfirmed chilled or frozen goods past a window raise `RECEIPT_OVERDUE`.
- **R-7 Lots at receiving:** receiving lines record lot and date; the matcher re-runs the spec 49 shelf-life constraint per lot; failing lots are held as `SHORT_SHELF_LIFE` and claimed.

## Acceptance criteria
- **AC-1** 10 ordered, 8 received, 6 accepted yields `ACCEPTED_PARTIAL`, a claim for 2 missing and 2 damaged, and a
  signed acknowledgement that verifies against the JWKS.
- **AC-2** An invoice with an 18 € surcharge absent from the quote yields `DISPUTED` with the charge as discrepancy; a
  corrected invoice reconciles to `MATCHED`.
- **AC-3** An invoice for 10 when 8 were accepted is disputed on quantity.
- **AC-4** Rounding differences within tolerance do not raise discrepancies.
- **AC-5** The evidence bundle of a reconciled order contains its documents and decisions and still verifies.
- **AC-6** A carrier "delivered" at the wrong loading bay leaves the order `DELIVERED_UNCONFIRMED`; frozen goods unconfirmed past the window raise `RECEIPT_OVERDUE`.
- **AC-7** A pallet with mixed lots holds and claims only the lot that fails the shelf-life constraint.
