# 09 — Price-complete shortlist (Core 1 quality)

Status: draft → in progress · Found in a live check on 2026-11-14.

## Purpose
The lookup screens a catalog pool with Decisions and sends only the ten highest fit scores to the live price
API. Fit scores of equally suitable products differ by noise (0.95 vs 0.96), so which ten get priced changes
from call to call: in one run the cheapest suitable milk (1,010 €) was priced and chosen, in the next it was
left out and the "best" offer cost 1,100 €. The buyer asked for the best option "based on price and
availability", so every product that is suitable enough must be priced before ranking.

## Requirements
- **R-1 Fit is a gate, price decides:** after the Decisions screen, the live shortlist is every pool candidate
  whose fit is at or above `approval.min_fit_probability` (the policy's confidence floor), highest fit first,
  capped at `shortlist.live_max` (new policy field, default 25, allowed 10–40).
- **R-2 Fill-up:** when fewer than `max_candidates` pass the floor, the shortlist is filled with the next best
  fits up to `max_candidates`, so the agent still sees the closest alternatives (they carry their low fit).
- **R-3 Still one call each:** all shortlisted products go to Valio in the same single `/api/price` and single
  `/api/availability` request. Live calls per lookup stay 1 + 1.
- **R-4 Returned offers:** all shortlisted products are ranked by the existing weighted score; the response
  returns the top `max_candidates` (default 10). `LookupResult` gains `priced_count` (how many were priced live)
  and `screened_count` (pool size scored by Decisions).
- **R-5 No Decisions:** when fit scores are unavailable, the shortlist is the first `max_candidates` of the pool
  in catalog order (unchanged behaviour, `NO_FIT_SCORE`).
- **R-6 Deterministic outcome:** with identical upstream data, two lookups whose fit scores differ only above
  the floor return the same best offer.
- **R-7 Policy + contract:** `shortlist.live_max` is readable and editable through `/v1/policy` with validation;
  a `PUT` without it keeps the current value; contract regenerated; README updated.

## Acceptance criteria
- **AC-1** Pool of 15 products all with fit ≥ floor, `max_candidates` 10 → 15 products priced in one price call
  and one availability call; 10 offers returned; `priced_count` 15, `screened_count` 15.
- **AC-2** The cheapest suitable product has the lowest fit among those above the floor → it is priced and
  ranked best.
- **AC-3** Two runs with fit scores shuffled among products above the floor → same best offer, same top three.
- **AC-4** Only 3 of 15 pass the floor → shortlist is those 3 plus the next 7 by fit; the 7 keep their low fit
  (and need approval if ordered, as today).
- **AC-5** `live_max` 12 with 15 passing → exactly 12 priced, highest fits first.
- **AC-6** Decisions unavailable → first `max_candidates` in catalog order, `NO_FIT_SCORE` warnings.
- **AC-7** `GET /v1/policy` shows `shortlist.live_max`; `PUT` validates 10–40; `PUT` without it keeps the value.
- **AC-8** Live check: repeated 1,000 L milk lookups return the same best offer every time (the 1,010 €
  product), with 1 + 1 Valio calls.
