# Frontend brief — human control plane

Owner: frontend teammate. Folder: `frontend/` (stack is your choice). Contract: `contracts/openapi.yaml`
(live docs at `http://localhost:8000/docs` when the backend runs). Background: `docs/CHALLENGE.md`,
`docs/DECISIONS.md` (see "Positioning" and "UI = a control plane").

## What we are building
Outside AI buyer agents (e.g. a hotel chain's agent) buy from Valio Aimo through our MCP gateway. The agent has no
screen. Our UI is for the **humans** around it: they see what agents did and why, approve what the policy sends to
them, and set the rules. The message the UI carries: *agents can buy on their own, inside rules that people set,
and every step is explained and recorded.*

## Screens (priority order)

### 1. Approval inbox + decision card — must have
- **Inbox:** `GET /v1/approvals` → list of `{order, quote}` with `order.status = PENDING_APPROVAL`. Show agent
  (`order.agent_id`), product, quantity, total, time left (`order.expires_at`), number of rule hits. Poll every
  2–3 s so a new order appears live during the demo.
- **Decision card** (one order):
  - The request: `GET /v1/lookups/{quote.need_ref}` → `need.intent`, quantity, `deliver_by`.
  - The agent's choice: `order.lines[]` — name, `units_needed` × `sales_unit`, `unit_price`, `price_per_l_or_kg`,
    `total`, availability (`available_amount`, `available_date`, `closing_time`), `delivery_reliability`,
    `co2_kg_per_kg`, `labels`.
  - **Why it needs you:** `order.policy.hits[]` — each `{rule, detail}` as one plain-language line
    (e.g. `max_auto_total_eur` → "Above the 1,500 € auto-approval limit").
  - Why it matched: `matched_because[]`, `fit` (show as % with the question in a tooltip: `fit_question`).
  - Alternatives: the other `offers[]` of the lookup with `rank_label` best / better / good, price, warnings.
  - Data source: `provenance.price` / `provenance.availability` (source, fetched at, valid until).
  - Actions: **Approve** / **Reject** with approver name + comment →
    `POST /v1/approvals/{order_id}/approve|reject` `{approver, comment}`. Alternatives are shown as context in the
    MVP; picking one comes after the MVP.

### 2. Order timeline (audit trail) — must have
- `GET /v1/orders` (list) and `GET /v1/orders/{id}`.
- `GET /v1/audit?order_id=…` and `GET /v1/audit?quote_id=…` → events `{ts, actor, action, summary, data}`.
  Render as a vertical timeline: request/lookup → quote (price locked until …) → order → policy check → human
  decision. Actor badge: `agent:…`, `human:…`, `system`. Show `data.live_calls` ("2 Valio API calls") and
  Decisions calls — they prove the agent queries live data only when needed.
- Status badges: CONFIRMED (green), PENDING_APPROVAL (amber), REJECTED (red), EXPIRED / CANCELLED (grey).

### 3. Policy page — the strongest demo moment
- `GET /v1/policy` → thresholds, warning codes that need approval or reject, ranking weights, `known_warning_codes`.
- Edit form → `PUT /v1/policy` `{policy, updated_by, comment}`; `POST /v1/policy/reset` `{updated_by}`.
- Show "last changed by … at …" and link the `POLICY_UPDATED` audit events.
- Demo: lower `max_auto_total_eur` from 1,500 to 1,000 → the agent re-runs the same request → the order now lands
  in the inbox instead of being confirmed automatically.

### 4. Operator overview — when 1–3 are done
- `GET /v1/stats` → a few charts/tiles: auto-confirmed vs sent to a human vs rejected, confirmed spend, average
  Valio calls per lookup, top warnings and rule hits, agents seen.

## Look and feel
- Calm, trustworthy B2B tool: dense but readable tables, clear status badges, generous whitespace on the card.
- Colours: navy + orange accent (as on the challenge flyer). Our own project name, presented as "a prototype for
  Valio Aimo".
- Desktop first (demo on a laptop/projector); readable at 1280 px.

## Demo flow (≈3 min) the UI must support
1. Terminal/Claude on the left: the buyer agent receives "1,000 L lactose-free milk by Monday".
2. Our UI on the right: the order appears in the inbox (or as CONFIRMED in the order list), open the card, approve.
3. Timeline shows every step with actors and reasons.
4. Policy page: lower the limit, re-run, outcome changes.
5. An exception: partial stock or a passed order cut-off, shown as a warning with the agent's alternatives.

## Running the backend locally
```
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # once
.venv/bin/python -m app.catalog build                                 # once
.venv/bin/python -m uvicorn app.api:app --port 8000
```
Needs the repo-root `.env.local` (VALIO_API_KEY, OPENAI_API_KEY — ask Gökalp). CORS allows `http://localhost:*`.
Sample data: run `backend/demo/run_buyer_agent.sh` (Claude as buyer agent) or create quotes/orders via `/docs`.
