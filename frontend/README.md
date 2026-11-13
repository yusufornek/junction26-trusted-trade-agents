# frontend/

Operator console for the Trusted Trade Agents gateway. Vite + React + TypeScript + Tailwind v4.
Talks to the backend only through the REST surface in `../contracts/openapi.yaml`.

## Run

The gateway must be up first (from `backend/`):

```sh
.venv/bin/python -m uvicorn app.api:app --port 8000
```

Then, from `frontend/`:

```sh
npm install        # once
npm run dev        # http://localhost:5173
npm run build      # typecheck + production build
```

Vite proxies `/v1` and `/health` to `127.0.0.1:8000`, so the browser stays on one origin and the
gateway can remain loopback-only. No frontend environment variables.

## Pages

| Route | What it shows |
|---|---|
| `/` | Committed spend, orders awaiting a human, share decided autonomously, upstream call count, approval preview, audit feed |
| `/buyer` | Structured need → offers (good/better/best) → locked quote → order |
| `/approvals` | Orders the policy held, with the matched rules, approve / reject as a named human |
| `/orders`, `/orders/:id` | All commitments; detail shows lines, warnings, provenance, policy outcome and that order's audit trail |
| `/audit` | Append-only record, expandable to the raw entry payload |
| `/policy` | The guardrails, rendered from `backend/policy.yaml` itself |

## Conventions

- Design tokens live in `src/index.css` under `@theme` — colours, radii and the `Inter` stack.
  Component classes (`card`, `chip`, `metric`, `btn-dark`, `field`) are defined there too, so pages
  stay free of long utility strings. Tailwind v4 cannot `@apply` another custom class; each one
  applies real utilities only.
- `src/api.ts` mirrors the contract by hand and is the only place that calls `fetch`.
- `src/ui.tsx` holds formatting helpers and the shared primitives, including `useAsync` — a
  deliberately plain fetch-on-mount hook with explicit `reload()`, no cache layer.
- Every number the agent acted on is shown with its evidence: unit conversion is spelled out,
  `matched_because` is listed, warnings are chips, and provenance names the upstream call and time.
- `/policy` imports `../../backend/policy.yaml?raw`, so the page can never drift from the rules the
  gateway loads. This needs `server.fs.allow: ['..']` in `vite.config.ts`.

## Not done

Live-updating views (everything refetches on mount or after an action), auth, multi-line quotes from
the UI (the buyer flow locks one offer at a time), and the order-cancel path the core supports.
