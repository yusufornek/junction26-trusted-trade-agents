# 46 — The five brief scenarios, end to end

Status: in progress · Decided on 2026-11-14 with Gökalp: every example scenario in the brief must work end to end.
Builds on specs 30 (exception journey), 31 (functional substitution), 32 (recipe to basket), 35 (recurring orders),
36 (supplier proposals) and 41 (purchase memory).

## Purpose
Each scenario the challenge brief lists runs as a scripted demo over MCP against the gateway, with the gateway doing
the procurement work: what to buy, what can arrive in time, what to substitute and why, and what needs a human.

## Requirements
- **R-1 On-hand stock (need → recipe → order):** a basket manifest ingredient may carry `on_hand {value, unit}`; the
  gateway buys only `max(0, needed − on_hand)` and shows needed, on hand and to-buy per line. An optional
  `focus_ingredient` (the seasonal ingredient the menu is built around) is kept first in coverage and never
  substituted without approval.
- **R-2 Time feasibility (last-minute event):** a basket or lookup with a near `deliver_by` marks each line
  `arrives_in_time: true|false` from `availableDate` and `closingTime`; lines that cannot arrive get
  `cannot_source_in_time` with the earliest possible date and in-time alternatives.
- **R-3 Recipe-logic substitution:** basket lines carry the spec 31 functional need (`function {product_type,
  intended_use, preparation?, acceptable_units[]}`, supplied by the agent from the recipe). When a line is out of
  stock or late, alternatives are chosen within that function and the standing constraints, never by SKU similarity
  alone. Every alternative carries a `trade_off` object: price difference per required quantity, pack and unit
  change, allergen and claim differences, delivery date difference, and a one-line explanation. Substitutes always
  need approval.
- **R-4 Stock-out watch (supplier stock-out):** a scheduler checks the next 7 days of recurring orders and open
  baskets/quotes once per run and on `POST /v1/stockout-watch/run`; when a product will not be available for its
  date it raises `STOCKOUT_PREDICTED` with a function-compatible substitute and its trade-off, as a signed webhook
  event and a buyer inbox entry, before the order date. Supplier calls respect the existing rate limits.
- **R-5 Proactive selling (Valio's own agent):** the supplier agent's campaigns (spec 36) can be generated from the
  catalog: `POST /v1/proposals/suggest` picks products in categories the buyer buys (purchase memory) that the buyer
  has never bought, filtered by the buyer's constraints, and submits them as a campaign within quotas. The buyer's
  agent receives them in the inbox and can accept into a normal lookup → quote → order.
- **R-6 Nutrition rules (allergen-safe substitution under pressure):** standing constraints may include nutrient
  limits per 100 g / 100 ml (`max_salt_g`, `max_sugar_g`, `max_saturated_fat_g`, `min_protein_g`) checked against the
  catalog's nutrition facts. Missing nutrition data is not proof: such products are excluded for that customer and
  listed with the reason. Hospital and school profiles can require them.
- **R-7 Five demo scripts:** `backend/demo/scenario_1_recipe_order.py`, `scenario_2_last_minute_event.py`,
  `scenario_3_stockout.py`, `scenario_4_proactive_selling.py`, `scenario_5_allergen_safe_substitution.py`, each
  driving the gateway over MCP (in-process sandbox data, no network), printing each step and the human decision
  point, and exiting 0. A sandbox scenario per script makes stock and dates deterministic. `docs/SCENARIOS.md`
  explains each run in plain words for the pitch.

## Acceptance criteria
- **AC-1** A manifest with `on_hand` buys only the difference and shows needed, on hand and to-buy.
- **AC-2** A basket for tomorrow marks a line that cannot arrive and offers an in-time alternative.
- **AC-3** An out-of-stock line is replaced by an alternative within its function and constraints, with a complete
  `trade_off`; a SKU-similar product outside the function is not offered.
- **AC-4** The stock-out watch predicts a stock-out for a recurring order three days ahead and delivers a signed
  webhook with a substitute and trade-off.
- **AC-5** Proactive suggestions include only never-bought products in bought categories that pass constraints, and
  accepting one reaches a quote.
- **AC-6** A hospital customer with `max_salt_g` excludes a product above the limit and a product without nutrition
  data, each with its reason.
- **AC-7** Each of the five demo scripts runs green in the test suite.
- **AC-8** The demo path (customer 1, 1000 L lactose-free milk → 464306, 1,010 €, CONFIRMED for hotel-chain-agent)
  is unchanged.
