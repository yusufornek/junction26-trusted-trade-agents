# Decisions

Agreed decisions, newest first. Format: `YYYY-MM-DD — decision — why`.

- 2026-11-14 — **Ordering requires a mandate.** An agent without a mandate can look up offers and get quotes;
  `place_order` answers `403 AGENT_NOT_REGISTERED` and the attempt is audited. Registered agents
  (`hotel-chain-agent`, `web-console`) act for customer "1" with a rolling 24-hour autonomous limit of 5,000 EUR
  — a clear line for the pitch: identity first, then policy (spec 06 R-2).
- 2026-11-14 — **Every substitute outside the buyer's stated preferences goes to a human** (brand, pack size,
  price cap, label) — it matches the brief's example of what needs sign-off; the operator can switch the rule
  in the policy (`OUTSIDE_PREFERENCE` in `warnings_need_approval`) (spec 08).
- 2026-11-14 — **Demo starts from a clean state:** `backend/scripts/reset_demo.py` archives the local database
  and runtime policy under `backend/var/archive/` and rebuilds the catalog — the audit log stays append-only
  and the demo screen shows only the demo's own orders (spec 14).
- 2026-11-13 — **Pitch opening: A.I.mo vs our agent, side by side.** Give the challenge's exact sentence ("1,000 L
  lactose-free milk by Monday …") first to A.I.mo in My Aimo, then to our buyer agent. A.I.mo returns product lists
  (drinking milk mixed with sour/curdled milk, no quantity, price, stock or order); our agent returns 1,000 × 1 L
  Valio Eila for 1,010 €, live price and stock, auto-approved under policy, every step in the audit trail. The
  contrast shows the gap we close in seconds. Record both runs on video as a backup for the live demo.
- 2026-11-13 — **Positioning: an agent-commerce governance layer** — governance for outside agents buying from
  Valio Aimo.
  Pitch line: "Valio Aimo's agent gateway — outside AI agents can buy, but with a mandate, policy, human sign-off
  and a full audit trail." Governance = who (agent identity → mandate), what it may do (policy), when it asks a
  human (rule hits / low confidence), what happened and why (audit), can the data be trusted (provenance → signed
  quotes). Order of work: the flow works first (demo 30% + user need 20%), then governance is made visible
  (guardrails 20% + openness 15% are what set us apart). In the pitch say "aligned with the EU AI Act principles of
  human oversight and record-keeping" — an accurate claim for a procurement agent.
- 2026-11-13 — **UI = a control plane for the humans** who watch, approve and set rules for agents (the agent's
  interface is MCP). The frontend owner designs the screens; the backend provides the data for approvals, order
  history/audit, policy and stats and shares what it observes in `docs/FRONTEND.md`.
- 2026-11-13 — **Frontend stack: Vite + React + TypeScript + Tailwind v4** — no SSR is needed for a local
  demo, Vite gives the fastest dev loop, and Tailwind tokens reproduce the agreed design language cheaply.
  Types are generated from `contracts/openapi.yaml`, so the contract stays the single source of truth.
  UI language is **English** (the jury is international), while the team keeps talking Turkish.
  Scope agreed with the team: dashboard, buyer flow (need → offers → quote → order), approval inbox,
  order list + detail, audit trail, policy view.
- 2026-11-13 — **Design language: light dashboard, dark icon rail.** Warm off-white page, white cards with
  ~20 px radius and hairline borders, near-black pill buttons, green for positive and coral for negative,
  pastel status chips, oversized metric numbers over small grey labels. Every agent decision shown on screen
  carries its evidence (matched_because, warnings, provenance), because that is what the jury scores.
- 2026-11-13 — **MVP first, ship fast.** Build the thinnest working end-to-end flow now; every design decision
  above is revisited one by one *after* the MVP runs — the demo is 30% of the score and time is short.
  Deferred to after the MVP: signed quotes (JWS), mandate as a verifiable credential, UBL/Peppol mapping,
  hash-chained audit log, two approver roles, drift detection, automatic split/substitution orders.
  MVP Core 3: one policy file (YAML) with a few rules — total over a threshold, any fuzzy-decision confidence
  below a threshold, or any warning (partial stock, date shift, cut-off) → `PENDING_APPROVAL`; hard-constraint
  violation → `REJECTED`; else `CONFIRMED`. One "approver" role. Plain append-only audit table.
  Demo buyer agent = any MCP client (Claude Desktop / Claude Code) connected to our MCP server, so no
  buyer-agent code is needed for the MVP.
- 2026-11-13 — **Core 2 Quote → Order** (we build the commitment layer; the mock has no order endpoint):
  `create_quote` re-checks price + availability live, locks a fixed price until min(price validTo, closingTime,
  now + 30 min), supports multiple lines (split orders for partial stock) and is signed (JWS) so an agent can
  verify it. `place_order(quote_id, idempotency_key)` checks expiry, re-checks stock, runs the permission
  rules (Core 3) → `CONFIRMED` | `PENDING_APPROVAL` | `REJECTED`. Fixed prices: order price = quote price;
  expired quote → re-quote with the price diff shown. States: quote OPEN → ACCEPTED | EXPIRED; order
  PENDING_APPROVAL → CONFIRMED | REJECTED | EXPIRED, CONFIRMED → CANCELLED before cut-off. Stock-model
  reservations are tracked locally (mock stock never decreases) — documented assumption. Quote/order fields
  aligned with UBL / Peppol BIS Ordering (a mapping table is enough). A passed `closingTime` is flagged as a
  warning and routed to a human until a mentor explains it. Live API calls are counted per request and
  shown in the audit log ("query only when needed").
- 2026-11-13 — **Core 1 Lookup:** MCP tools `search_products` / `get_offers` (+ same over REST). Input is a
  structured need spec (product intent, hard constraints like lactose-free / excluded allergens, quantity +
  unit, deliver-by date, customer); turning the buyer's sentence into it is the buyer agent's job. Two stages:
  (a) local SQLite catalog search with full-text + hard filters on structured catalog facts (claims, allergens,
  labels, pack volume); (b) live `/api/price` + `/api/availability` only for the top ~10 candidates. Ranking:
  weighted score (price, availability, delivery reliability, CO2; weights from the buyer policy) plus fuzzy
  fit questions via the **OpenAI Decisions API** (`gpt-6-luna`, probability + confidence), presented as
  good/better/best. Each offer card carries pack size, units needed (lot-size rounded), unit / per-litre /
  total price, availability vs date (partial, silent date shift, cut-off), reliability, CO2, labels, why it
  matched (each check + its source) and provenance (source, fetched-at, valid-to) — deterministic gateway,
  fuzzy judgments decomposed into named questions so every decision is auditable.
- 2026-11-13 — Fuzzy decisions go through one provider-agnostic decision interface; OpenAI Decisions API is the
  provider (key verified), Claude/Jev are possible fallbacks — the demo keeps working with whichever provider is
  available.
- 2026-11-13 — The product's agent interface is an **MCP server** (primary door for outside agents), with a
  REST/OpenAPI door over the same core for the frontend — the brief asks how "an outside agent can work with
  our systems on open standards"; MCP is the open agent-tool standard, and any MCP client can plug in for the demo.
- 2026-11-13 — Work continues **only in this repo** (yusufornek/junction26-trusted-trade-agents); the earlier
  Daption-ciray/junction repo is abandoned and its agent docs were moved here — one place for data, docs and code.
- 2026-11-13 — Repo layout: `backend/` (backend + AI owner), `frontend/` (frontend owner),
  `contracts/openapi.yaml` (shared API contract, owned by backend), `docs/` shared — each writer works in its own
  folder, and the frontend can mock against the contract before the backend is ready.
- 2026-11-13 — Backend stack: **Python + FastAPI** — OpenAPI comes out of the code (openness criterion),
  pydantic schemas, easy data work, Python MCP SDK available.
- 2026-11-13 — Team split: **Gökalp (+ Claude/Codex) = backend + AI agent**; a frontend teammate = UX + frontend;
  a business teammate = business side (pitch, model, Valio Aimo fit). Backend and frontend share one repo —
  clear ownership keeps parallel work from colliding.
- 2026-11-13 — Challenge: **Valio Aimo — Trusted Trade Agents** (details in `docs/CHALLENGE.md`) — chosen by
  the team; API key and portal password kept in the gitignored `.env.local`.
- 2026-11-13 — Shared agent memory lives in plain files
  (`AGENTS.md`, `CLAUDE.md`, `docs/`) — fast, zero-risk, readable by both Claude and Codex.
- 2026-11-13 — Gökalp's personal note vault stays outside this repo — keeps the submission repo free of hooks
  and vault folders.
- 2026-11-13 — Working model: Claude = advisor/orchestrator/reviewer, Codex = builder,
  Claude subagents = research/review/tests.
