# 41 — Purchase memory and replenishment suggestions

Status: in progress · Source: the business teammate's proposal (`business_oneriler.txt` on branch `business`) and
Gökalp's idea of a gateway memory that turns repeated behaviour into workflows, 2026-11-14.

## Purpose
A buyer orders the same things on a rhythm. The gateway remembers each customer's purchases, recognises the
rhythm, and tells the buyer's agent ahead of time: "you order about 1,000 L of lactose-free milk every month,
the last order was on 1 November, the next one is due around 1 December — shall I prepare it?" The agent can turn
the pattern into a standing workflow. Every purchase still runs through the buyer's mandate, policy and approval.

## Requirements
- **R-1 Purchase memory per customer:** built from the gateway's own confirmed orders (and, in sandbox, from a
  seeded history file). Per customer and product: order dates, quantities in the request unit, total paid,
  agent. Memory is scoped to the customer; other customers, agents of other organisations and supplier agents
  never see it.
- **R-2 Pattern detection:** a product with at least 3 confirmed orders forms a pattern: median interval in days,
  interval spread, typical quantity (median) and its spread, last order date, predicted next need date
  (last + median interval), and a confidence from the number of orders and the spread (high / medium / low,
  with the reason).
- **R-3 Suggestions:** `GET /v1/suggestions` (agent: its customers; buyer users: their organisation; operator:
  all) and MCP tool `get_replenishment_suggestions` return patterns whose predicted need date falls within a
  horizon (default 14 days): the evidence (dates and quantities), the suggested quantity, the predicted date,
  the confidence and a plain-language sentence.
- **R-4 Acting on a suggestion:** `POST /v1/suggestions/{id}/prepare` (MCP `prepare_suggestion`) runs a normal
  lookup for the suggested need under the caller's mandate and returns the lookup with plans — it never quotes or
  orders by itself. `POST /v1/suggestions/{id}/make-recurring` (MCP `create_recurring_from_suggestion`) creates a
  recurring order (spec 35) with the pattern's cadence and quantity as its baseline; the buyer's recurring-order
  rules and drift detection apply. `POST /v1/suggestions/{id}/dismiss` and `/snooze {days}` are remembered.
- **R-5 Workflows over MCP:** MCP tools `list_recurring_orders`, `create_recurring_order`,
  `run_recurring_order_now` and `pause_recurring_order` expose spec 35 to agents under their mandate (a mandate
  without the recurring permission is refused).
- **R-6 Buyer control:** `PUT /v1/buyer/memory-preferences` (buyer_admin): memory on / off per customer,
  excluded products, suggestion horizon; switching memory off hides suggestions and stops pattern updates.
  `DELETE`-style forgetting is a recorded request that hides the customer's memory from suggestions.
- **R-7 Audit and events:** suggestion shown, prepared, made recurring, dismissed and snoozed are audited and
  emitted as events (`suggestion.*`) for webhooks (spec 33).
- **R-8 Demo:** sandbox scenario `monthly_milk` seeds six monthly orders of ~1,000 L lactose-free milk for the
  hotel chain on the 1st; `backend/demo/run_replenishment.py` shows the suggestion, prepares it, and turns it
  into a recurring order.

## Acceptance criteria
- **AC-1** Six monthly orders on the 1st of ~1,000 L → one pattern: median interval ~30 days, typical quantity
  1,000 L, predicted next date the 1st of the next month, confidence high.
- **AC-2** Two orders only → no pattern; irregular intervals (spread above the threshold) → confidence low with
  the reason.
- **AC-3** The suggestion appears for the hotel chain's agent within the horizon and for no agent of another
  organisation.
- **AC-4** `prepare` returns a lookup with plans and creates no quote or order; quoting and ordering afterwards
  follow the normal mandate and approval rules.
- **AC-5** `make-recurring` creates a recurring order whose baseline equals the pattern; the next run behaves as
  spec 35 (autonomous within tolerance, approval on drift).
- **AC-6** Memory switched off → no suggestions; dismissed / snoozed suggestions stay hidden for their period.
- **AC-7** MCP `create_recurring_order` by an agent whose mandate lacks the recurring permission → refused with
  the recovery payload.
- **AC-8** Every suggestion action is in the audit log and emits its event.
