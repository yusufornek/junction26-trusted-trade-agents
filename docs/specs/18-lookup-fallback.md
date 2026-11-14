# 18 — Lookup fallback and screening disclosure

Status: in progress · Decided on 2026-11-14 after the external architecture review.

## Purpose
An agent that sends only a free-text intent ("1000 litres of lactose-free milk for a hotel") gets no result,
because every word must match a product. And when more products match than the pool screens, nothing says so.
The lookup recovers from a too-narrow query in a deterministic way and reports how much it looked at.

## Requirements
- **R-1 Retrieval modes:** with `search_terms` the lookup searches those terms (all must match), mode `terms`.
  Without them it searches the intent words (all must match), mode `intent`.
- **R-2 Deterministic broadening:** when mode `intent` matches nothing, the lookup keeps the meaningful words
  of the intent (lower-cased; numbers, units of measure and a short English + Finnish stop-word list removed;
  hyphenated words also tried in parts) and searches for products matching any of them, best match first (most
  words matched, then FTS rank). Mode `intent_broadened`. Hard constraints apply unchanged; the fit screening
  receives the original intent.
- **R-3 Retrieval report:** `LookupResult.retrieval {mode, terms_used[]}` and, in mode `intent_broadened`, a
  lookup-level warning `RETRIEVAL_BROADENED` naming the words used.
- **R-4 Counts:** `LookupResult.matched_count` = products that matched the search and passed the hard
  constraints; `screened_count` = products given to the fit screening; `priced_count` as today. When
  `matched_count > screened_count` the lookup carries warning `POOL_TRUNCATED` ("40 of 1,455 matching products
  screened; add search terms to narrow").
- **R-5 Honest empty result:** when nothing matches after broadening, `no_match_reason` names the words tried
  and suggests `search_terms`.
- **R-6 Audit:** the `LOOKUP` audit entry records `retrieval` and the three counts.

## Acceptance criteria
- **AC-1** Intent "1000 litres of lactose-free milk for a hotel", no `search_terms`, claim FREE_FROM_LACTOSE →
  offers are returned, `retrieval.mode = intent_broadened`, `terms_used` contains "milk", warning
  `RETRIEVAL_BROADENED` present.
- **AC-2** `search_terms: ["milk"]` → `retrieval.mode = terms`, no broadening warning (unchanged behaviour).
- **AC-3** An intent whose words all match products → mode `intent`, no warning.
- **AC-4** More matching products than `shortlist.catalog_pool` → `matched_count` > `screened_count` and warning
  `POOL_TRUNCATED` with both numbers.
- **AC-5** An intent with no meaningful word in the catalog → no offers, `no_match_reason` lists the words tried.
- **AC-6** In mode `intent_broadened` a product violating a hard constraint is still excluded.

## For the review
Deterministic only: the buyer's agent stays responsible for turning prose into `search_terms`; the fallback
keeps a sloppy request from ending in an empty answer.
