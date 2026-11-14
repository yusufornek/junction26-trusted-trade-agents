# 50 — Commitment ceiling and tolerance

Status: in progress · Decided on 2026-11-14 with Gökalp (capability spec, see spec 47). Builds on specs 02 (quote and
order), 05 (size reconciliation), 10 (signed quotes) and 40 (graduated autonomy).

## Purpose
What the buyer authorised is a maximum payable amount with tolerances, and it is enforced at every stage where the
amount can change: quote, re-quote, supplier allocation of catch-weight goods, confirmation, delivery and invoice.

## Requirements
- **R-1 Ceiling:** every approved quote or intent records `max_payable` (total incl. charges) and per-line tolerances
  `{quantity_pct, unit_price_pct, weight_range}`; defaults come from the guardrail profile (spec 45).
- **R-2 Catch weight:** products sold by actual weight carry `pricing: per_kg`, `nominal_weight`, `weight_range`; the
  quote shows the minimum, nominal and maximum line totals. The sandbox supplier's allocation step returns the actual
  weight before commitment.
- **R-3 Checkpoints:** one function `check_commitment(stage, authorised, proposed)` runs at re-quote, allocation,
  supplier confirmation (changed price, quantity or substitution), delivery note and invoice. Within tolerance and
  ceiling → proceed and record; beyond → `COMMITMENT_EXCEEDED {stage, authorised, proposed, difference}` with options:
  reduce (smaller allocation or quantity), accept (approval for the new amount), or reject.
- **R-4 Charges:** surcharges, deposits, delivery fees and minimum-order fees are explicit lines with a type; a charge
  absent from the authorised quote is always beyond tolerance.
- **R-5 Price drift:** a price change between quote and order inside tolerance re-signs the quote and proceeds; outside,
  it raises `COMMITMENT_EXCEEDED` at the order step.
- **R-6 Surfaces:** REST and MCP tools to read the ceiling and resolve an exceedance; webhook `COMMITMENT_EXCEEDED`;
  audit entries for every checkpoint result.
- **R-7 Shipment plan:** the supplier confirmation includes the shipment plan (count, dates, charges); a changed plan is a checkpoint and re-runs storage reservations (spec 51).
- **R-8 Ceiling as order terms:** the supplier order states per-line maximum quantity or weight and max payable; an exceedance at delivery or invoice offers `accept_up_to_ceiling`, which creates a spec 52 claim for the excess and marks the liability `disputed`.

## Acceptance criteria
- **AC-1** Fish approved at about 10 kg with a 200 € ceiling: an allocation of 10.6 kg at 20 €/kg (212 €) raises
  `COMMITMENT_EXCEEDED`; choosing a smaller allocation proceeds, and approving 212 € proceeds with a new approval.
- **AC-2** An allocation inside the weight range and ceiling proceeds without asking.
- **AC-3** A supplier confirmation with a 3 % price rise inside a 5 % tolerance proceeds and re-signs; an 8 % rise
  raises the exceedance.
- **AC-4** An unexpected 18 € delivery surcharge at invoice raises `COMMITMENT_EXCEEDED` at stage `invoice`.
- **AC-5** A confirmation that splits the basket into three shipments with two extra delivery charges raises `COMMITMENT_EXCEEDED` at stage `confirmation`.
- **AC-6** A heavier catch-weight delivery beyond the ceiling offers `accept_up_to_ceiling`, which records a claim for the excess.
