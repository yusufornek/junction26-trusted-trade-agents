# 20 — Ranking by the total cost of the packs bought

Status: in progress · Decided on 2026-11-14.

## Purpose
Offer cards are ranked by the price per litre or kilo. What the buyer pays is the price of the whole packs needed
to cover the request, including what is bought on top because packs do not divide evenly. Plans already use that
total; offer cards rank the same way.

## Requirements
- **R-1 Purchase total per offer:** each offer carries `purchase_total` = sales units needed × unit price, and
  `overbuy` {amount, unit} = covered amount − requested amount (zero when exact). Both appear in the offer's
  explanation text.
- **R-2 Price factor:** the price factor of the ranking uses `purchase_total` for offers whose size is known.
  Offers with unknown size keep the per-unit comparison and keep their `SIZE_UNKNOWN` warning.
- **R-3 Factor contributions:** each offer shows `score_breakdown` {price, coverage, reliability, co2, fit} with
  each factor's weighted contribution, so the reason for the order of the cards is visible.
- **R-4 Labels:** `unit_price` keeps its meaning (price per sales unit); the contract describes which figure is
  per unit and which is the total.

## Acceptance criteria
- **AC-1** Request 1,001 L; product A 1 L at 1.00 €/L, product B 5 L at 0.995 €/L (4.975 € per pack) →
  A: 1,001 units, 1,001.00 €, overbuy 0; B: 201 units, 999.98 €, overbuy 4 L; the cards show both totals and the
  price factor uses them.
- **AC-2** A case where the per-litre order and the total order differ (cheaper per litre but large overbuy) →
  the offer with the lower `purchase_total` ranks higher on price.
- **AC-3** `score_breakdown` contributions sum to the offer's score (within rounding).
- **AC-4** The live demo request (1,000 L lactose-free milk) still returns 464306 as best at 1,010 €.
