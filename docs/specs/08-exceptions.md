# 08 — Exceptions: partial stock, split orders, substitutes

Status: draft · Judged as "Robustness to exception situations" (15%): partial availability, substitutions and
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

## For the review
Choices made in this draft: max 4 lines per split; substitutes need approval by default; split plans are
proposed only from products that pass the hard filters and have a fit ≥ the policy's confidence floor.
Later steps: recipe-aware substitution, allergen-safe substitution rules per customer type, stock-out after
confirmation (re-plan and notify).
