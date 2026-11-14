# frontend/

Operator console for the Trusted Trade Agents gateway. Vite + React + TypeScript + Tailwind v4.
Talks to the backend only through the surface in `../contracts/openapi.yaml`.

## Run

The gateway must be up first (from `backend/`; restart it after every pull — a running process keeps
old code while `policy.yaml` is re-read from disk):

```sh
.venv/bin/python -m uvicorn app.api:app --port 8000
```

Then, from `frontend/`:

```sh
npm install        # once
npm run dev        # http://localhost:5173 — sign in with OPERATOR_TOKEN from the repo-root .env.local
npm run build      # typecheck + production build
```

The dev server proxies the same paths `deploy/Caddyfile` sends to the gateway (`/v1`, `/health`,
`/.well-known`, `/mcp`, `/a2a`, `/developers`, `/llms.txt`, `/docs`, `/openapi.json`), so the browser
stays on one origin in development and in production. No frontend environment variables.

### Fixture mode (design review)

```sh
npm run dev:mock   # http://localhost:5174 — no gateway, no token
```

Every screen renders from `src/mock.ts` — products, prices and warnings taken from real gateway
responses, with every order status and warning family present. A "MOCK DATA" badge stays on screen.
`VITE_MOCK` is replaced at build time, so production bundles drop the branch and never load the fixtures.
Use it to review layouts in parallel without sharing the in-memory operator token or touching the demo
database.

## Pages

| Route | What it shows |
|---|---|
| `/` | Committed spend, orders awaiting a human, share decided autonomously, calls per lookup, spend curve, approval inbox, agents, rule hits, audit feed |
| `/buyer` | Structured need with hard constraints and soft preferences → lookup counts (matched / screened / priced) and where each constraint came from → **fulfilment plans with a policy preview** (would the agent confirm alone?) → ranked offers with score breakdown → signed quote from a plan or a hand-built split → order |
| `/approvals` | Orders the policy held, with the matched rules and the price-lock countdown; approve / reject as a named human |
| `/orders`, `/orders/:id` | All commitments; detail shows lines with provenance, the hash-chained **timeline**, the alternatives the agent ranked, the policy outcome, **cancel** before the cut-off, and **proof**: price commitment, quote signature digest, signed receipt, UBL 2.1 order document |
| `/products/:id` | Catalog facts the agent judged on: pack and units, claims and labels, allergen statuses and whether a declaration exists |
| `/audit` | Append-only record, expandable to the raw entry payload |
| `/policy` | The guardrails, editable: ceiling, confidence floor, warnings that need a signature or are informational, hard stops, shortlist sizes, ranking weights, **agent mandates**; standing customer constraints shown read-only |
| `/agents` | **Graduated autonomy (spec 40):** the approval-first → track record → autonomy path with the policy's thresholds; per agent its level, 30-day track record and recommendation, and grant / withdraw with an audited reason. Also each mandate's customers, per-order and per-day limits, autonomous spend in the last 24 h, unregistered agents, and revoking a buyer-issued VC-JWT mandate |
| `/trust` | Audit chain head and full verification, public Polygon anchors, the published signing keys, how an outside agent connects (REST, MCP stdio, MCP HTTP) and the open standards `/v1/meta` reports |

## Conventions

- Theme: Valio Aimo blue-and-white (see `docs/DECISIONS.md`). Tokens live in `src/index.css` under
  `@theme`; status colours (green / amber / coral / grey) keep their meaning and never stand in for
  the brand. Component classes (`card`, `chip`, `metric`, `btn-dark` = primary, `btn-ghost`,
  `btn-danger`, `field`) are defined there; Tailwind v4 cannot `@apply` another custom class, so each
  applies real utilities only.
- `src/charts.tsx` holds hand-written SVG charts (sparkline, area chart, ring, bars, progress, avatar).
- `src/api.ts` mirrors the contract by hand and is the only place that calls `fetch`. Non-JSON error
  bodies (a crash, a proxy failure) are reported with their status instead of failing to parse.
- `src/ui.tsx` holds formatting helpers and the shared primitives, including `useAsync` — a plain
  fetch-on-mount hook with explicit `reload()` and optional polling. Background refreshes are quiet and
  pause while the tab is hidden; the backend has no push events.
- Every number the agent acted on is shown with its evidence: unit conversion is spelled out,
  `matched_because` is listed, warnings are chips, provenance names the upstream call and time.
- Allergen codes are named only where the GS1 meaning is certain; other codes are shown bare.

## Not done

Auth beyond the operator token (deliberate for the demo). Spec 40 R-5 (showing whether a buyer approver or
the Valio operator signed) has no contract field yet. The dashboard derives its daily spend series from
orders on the client, because the gateway stores events rather than a time series.
