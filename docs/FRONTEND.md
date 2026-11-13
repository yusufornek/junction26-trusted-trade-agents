# Backend notes for the frontend

Written from the backend side for the frontend/UX owner: what the API offers and what we noticed while testing
it end to end. Screens, flow and look are the frontend owner's call — use whatever here is useful.

Contract: `contracts/openapi.yaml` (live docs at `http://localhost:8000/docs` when the backend runs).
Background: `docs/CHALLENGE.md`, `docs/DECISIONS.md`.

## What the backend exposes for people (the agent itself uses MCP)
| Need | Endpoint |
|---|---|
| Orders waiting for a human | `GET /v1/approvals` → `{order, quote}` with `order.status = PENDING_APPROVAL` |
| Approve / reject | `POST /v1/approvals/{order_id}/approve` or `/reject` with `{approver, comment}` |
| The agent's search behind a quote, incl. alternatives | `GET /v1/lookups/{quote.need_ref}` → `need`, `offers[]` |
| Orders | `GET /v1/orders`, `GET /v1/orders/{id}` |
| Step-by-step history | `GET /v1/audit?order_id=…` / `?quote_id=…` → `{ts, actor, action, summary, data}` |
| Rules the agent runs under | `GET /v1/policy`, `PUT /v1/policy` `{policy, updated_by, comment}`, `POST /v1/policy/reset` |
| Overview numbers | `GET /v1/stats` (optional `?since=`) |

## Observed while testing the backend
- **Why an order needs a human** is in `order.policy.hits[]` as `{rule, detail}`, e.g. `max_auto_total_eur` →
  "Net total 1708.00 exceeds 1500.00 EUR", `CUTOFF_PASSED` → order cut-off already passed. This was the most
  useful information when judging an order by hand.
- **Pending orders expire**: `order.expires_at` is at most 30 minutes after the quote (fixed-price lock). In our
  runs the remaining time mattered for whoever approves.
- **Every offer explains itself**: `matched_because[]` (e.g. "FREE_FROM_LACTOSE from catalog nutritionalClaim"),
  `fit` (0–1 from OpenAI Decisions, the question in `fit_question`), `warnings[]` with `code` + plain `text`,
  and `provenance` (which Valio endpoint, fetched at, valid until).
- **Alternatives exist for every order**: the lookup behind a quote keeps up to 10 ranked offers with
  `rank_label` best / better / good — handy when someone wants to see what else the agent considered.
- **Actors in the audit log** are `agent:<id>` (e.g. `agent:hotel-chain-agent`), `human:<name>` and `system`;
  `data.live_calls` shows how few Valio calls a request needed (typically 1 price + 1 availability).
- **Policy changes take effect immediately** and are audited as `POLICY_UPDATED` with a before/after diff.
  Lowering `max_auto_total_eur` from 1,500 to 1,000 turned the same 1,010 € order from CONFIRMED into
  PENDING_APPROVAL — a clear way to show that people set the agent's limits.
- **Common warnings in real data**: `CUTOFF_PASSED` appears on most Terminal products (meaning still to be
  confirmed with a mentor), `PARTIAL_STOCK` on some Stock products, `SIZE_UNKNOWN` on items without litre data.
- **Status values**: `CONFIRMED`, `PENDING_APPROVAL`, `REJECTED`, `EXPIRED`, `CANCELLED`.
- **Timing**: a lookup with live calls + Decisions takes a few seconds; approvals and audit reads are instant.
  There are no push events yet — polling every few seconds is what we used while testing.

## Demo context (from `docs/DECISIONS.md`)
The pitch opens by giving the challenge sentence ("1,000 L lactose-free milk by Monday …") to A.I.mo in My Aimo
and then to our agent; what our agent did is then shown through the UI.

## Running the backend locally
```
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # once
.venv/bin/python -m app.catalog build                                 # once
.venv/bin/python -m uvicorn app.api:app --port 8000
```
Needs the repo-root `.env.local` (VALIO_API_KEY, OPENAI_API_KEY — ask Gökalp). CORS allows `http://localhost:*`.
Sample data: `backend/demo/run_buyer_agent.sh` (Claude as buyer agent) or quotes/orders via `/docs`.
Need another field or endpoint? Ask the backend side — it is quick to add.
