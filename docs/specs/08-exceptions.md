# 08 — Exceptions: partial stock, split orders, substitutes

Status: built (verified live 2026-11-14: split plan 33 cases + 4 cartons for 400 L, quote from `plan_ref`, order outcome equal to the preview) · R-10–R-13 added after the independent review, in progress · Judged as "Robustness to exception situations" (15%): partial availability, substitutions and
delivery constraints beyond the happy path. Brief: "how an agent handles partial availability and substitution
ranking (good-better-best) within Valio Aimo's own assortment."

## Purpose
Today an offer that cannot cover the request carries a warning and the agent has to work out the rest. The
gateway should answer the exception itself: propose complete ways to fulfil the need, say what each one costs
and whether it would need a human, and mark substitutes that leave the buyer's stated preferences.

## Requirements
- **R-1 Fulfilment plans:** every lookup result carries `plans[]` next to `offers[]`. A plan is a set of lines
  `{product_id, units, covers}` that together cover the requested quantity by the deadline, with `total`,
  `coverage`, `warnings[]` and a `kind`:
  - `single` — one product covers everything (the best fully-covering offers, up to 3);
  - `split` — several products combined because no acceptable single product covers it, or because a split is
    cheaper: fill from the best-ranked offers with on-time stock until the quantity is covered (max 4 lines);
  - `partial` — the best the assortment can do by the deadline when full coverage is impossible, with the
    uncovered quantity stated (`uncovered {value, unit}`).
- **R-2 Policy preview:** each plan carries `policy_preview {outcome, hits[]}` — the result the current policy
  would give if this plan were ordered now (same evaluation as `place_order`, no side effects). The agent knows
  before committing whether it acts within its authority or a human will be asked, and why.
- **R-3 Preferences:** `NeedSpec.preferences` (all optional): `brands[]`, `max_price_per_unit` (per L / KG),
  `pack_size {min, max}` (L / KG per sales unit), `labels[]` (e.g. organic). Hard constraints stay in
  `required_claims` / `exclude_allergens`.
- **R-4 Substitutes are marked:** an offer that meets the hard constraints but leaves a stated preference is a
  substitute: warning `OUTSIDE_PREFERENCE` listing each difference ("brand Arla, preferred Valio"; "10 L box,
  preferred ≤ 1 L"). `OUTSIDE_PREFERENCE` is a known warning code and is in the default
  `warnings_need_approval` — matching the brief's "substitutions outside stated preferences" need sign-off.
- **R-5 Ranking:** offers inside the preferences rank above substitutes; within each group the existing score
  decides. Labels best / better / good go to the top three overall.
- **R-6 One-call ordering:** `create_quote` accepts `plan_ref {lookup_id, plan_index}` as an alternative to
  explicit lines; the quote is built from the plan's lines and re-checked live as usual.
- **R-7 Nothing fits:** when the hard filters leave no product, the result says so with
  `no_match_reason` (which constraint removed how many candidates) instead of an empty list without explanation.
- **R-8 Live calls stay bounded:** plans are computed from the offers already fetched; no extra Valio calls.
- **R-9 MCP + contract:** tool descriptions explain plans, previews and substitutes; contract regenerated.

- **R-10 Bounded work:** building plans takes bounded time whatever the quantity, pack size or stock limits:
  the search for the cheapest split stops after a fixed node budget (default 20,000) and keeps the best plan
  found so far; the result is always a valid plan. Plans are computed outside the database write transaction,
  so a lookup never makes quotes, orders or approvals wait.
- **R-11 Preferences first:** split and partial plans are first built from offers inside the buyer's
  preferences. A cheaper split that includes a substitute is added as a further plan (it carries
  `OUTSIDE_PREFERENCE` and previews accordingly), so the agent always sees the plan that stays within its
  authority when one exists.
- **R-12 Preview knows the customer scope:** when the calling agent has a mandate and the need's customer is
  outside it, every plan previews `REJECTED` with rule hit `CUSTOMER_NOT_IN_MANDATE` — the same refusal the
  quote would give.
- **R-13 Weight lots:** for KG-sold products plan units follow `allowed_lot_size` (e.g. 10.5 kg), so a single
  plan's units and total equal that product's offer.

## Acceptance criteria
- **AC-1** One product covers 1,000 L → a `single` plan for it with `policy_preview.outcome = CONFIRMED` when
  within limits.
- **AC-2** Best product has 380 L in stock, second has 700 L → a `split` plan with two lines covering 1,000 L;
  totals and coverage are the sums; each line's units respect whole sales units.
- **AC-3** All on-time stock together covers 600 of 1,000 L → a `partial` plan with `uncovered = 400 L` and
  warning `PARTIAL_STOCK`; preview is `PENDING_APPROVAL`.
- **AC-4** A plan whose total exceeds the auto limit previews `PENDING_APPROVAL` with `max_auto_total_eur`;
  ordering it gives the same outcome and hits.
- **AC-5** `preferences.brands = ["Valio"]`: an Arla offer carries `OUTSIDE_PREFERENCE` naming the brand and
  ranks below every Valio offer; ordering it needs approval.
- **AC-6** `preferences.pack_size.max = 1`: a 10 L box is a substitute with the pack-size difference in the text.
- **AC-7** `create_quote` with `plan_ref` produces the plan's lines; an out-of-range `plan_index` → 422.
- **AC-8** Hard filters remove everything → empty `offers`, empty `plans`, `no_match_reason` set.
- **AC-9** Plans add no Valio calls (`live_calls` unchanged: 1 price + 1 availability).
- **AC-10** Live check: 1,000 L of a product family with limited Stock items yields a sensible split or partial
  plan with previews.

- **AC-11** 10,000 KG, ten KG-sold products each with 3,000 kg on-time stock at 3.00–3.09 EUR/kg → plans return
  in under 2 seconds with a valid 4-line split covering 10,000 kg (3 × 3,000 + 1,000 from the cheapest four).
- **AC-12** 1,000 L, a 0.02 L portion at 0.1234 EUR and a 1 L pack at 6.17 EUR, both unlimited → plans return in
  under 2 seconds.
- **AC-13** `preferences.brands = ["Valio"]`, Valio A 1 L at 1.20 with 380 in stock, Valio B 1 L at 1.25 with 700,
  Arla C 1 L at 1.22 unlimited, need 1,000 L → a split A 380 + B 620 is offered and previews `CONFIRMED`; the
  cheaper split with C is offered as a further plan carrying `OUTSIDE_PREFERENCE`.
- **AC-14** Registered agent with `customer_ids ["1"]` looks up for customer "2" → every plan previews
  `REJECTED` with `CUSTOMER_NOT_IN_MANDATE`.
- **AC-15** KG-sold product with lot size 0.5 and a 10.25 kg need → the single plan has 10.5 units and the same
  total as the offer.
- **AC-16** While plans are being built, another connection can open a write transaction (no write lock held).

## For the review
Choices made in this draft: max 4 lines per split; substitutes need approval by default; split plans are
proposed only from products that pass the hard filters and have a fit ≥ the policy's confidence floor.
Later steps: recipe-aware substitution, allergen-safe substitution rules per customer type, stock-out after
confirmation (re-plan and notify).
