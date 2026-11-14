# Decisions

Agreed decisions, newest first. Format: `YYYY-MM-DD — decision — why`.

- 2026-11-14 — **The product is Luotsi; the console takes its colours and marks from the Luotsi brand kit**
  (`Luotsi-7a-Kit`, the user's request): teal `#005671` for actions, charts, focus and the rail, slate `#2C6A81` as
  the secondary colour, the galley-and-star mark in the rail, phone bar and favicon, the wordmark on the sign-in card
  and the dashboard's summary card, and the kit's share image for link previews. This replaces the Valio-blue console
  theme below; Valio Aimo stays the supplier the agents buy from, and the forest-floor status colours are unchanged.
  Rail text moved from white/60 to white/75, which keeps 5.4:1 on teal (white/60 drops to 4.0:1).
- 2026-11-14 — **The console has a Connect page (`/connect`)**: pick the tool the agent works in (Claude Desktop,
  Claude Code, Cursor, VS Code, ChatGPT, another MCP client, REST), create a per-agent key shown once, copy that
  tool's own setup, and see the agent's first call arrive — the user's request, modelled on Higgsfield's connect
  flow. Keys come from the gateway (`/v1/connections`, contract in `docs/CONNECT-API.md`, backend by Gökalp); new
  agents start approval-first (spec 40). Until the endpoints exist the page says so and shows `<YOUR_AGENT_KEY>`.
- 2026-11-14 — **Console typeface is Schibsted Grotesk; status colours come from the Finnish forest floor**: spruce
  `#2e8b62` confirmed, cloudberry `#c97716` pending, lingonberry `#c73b45` rejected, birch `#7d8a97` cancelled or
  expired — the user found the system font and stock status colours generic. Schibsted Grotesk has tabular figures
  for prices; text on its soft background stays above 4.5:1 and fills above 3:1. The rail widens with page names on
  hover and the page moves over and narrows instead of being covered (the user's request); every route is measured
  with the rail open and closed from 768 to 1440 px.
- 2026-11-14 — **The gateway remembers purchase rhythms and suggests replenishment** (business teammate's
  proposal + Gökalp): per-customer purchase memory, detected cadence and quantity, suggestions the buyer's agent can
  prepare as a lookup or turn into a recurring order over MCP; suggestions never order by themselves (spec 41).
- 2026-11-14 — **Console theme is Valio Aimo blue-and-white**, replacing the dark rail and near-black buttons —
  the product is presented to Valio Aimo, whose identity is blue and white. Values come from valioaimo.fi's own
  CSS: navy `#002f6d` (rail), Valio blue `#003388` (primary actions, charts, focus), Aimo brand `#005670`,
  Aimo light `#d1dde3`, page `#f2f7f8`. Status colours keep their meaning and are never replaced by the brand:
  green confirmed/covered, amber pending, coral rejected, grey cancelled/expired. Tokens live in
  `frontend/src/index.css` only.
- 2026-11-14 — **Frontend has a fixture mode for design review** (`npm run dev:mock`, port 5174): the whole
  console renders from `frontend/src/mock.ts` without a gateway or operator token, with a visible "MOCK DATA"
  badge. The flag is compiled out of production builds — reviewers (human or agent) can inspect every screen
  in parallel without sharing the in-memory operator token or mutating the demo database.
- 2026-11-14 — **Follow the organiser's mock-API answers** (spec 39): whole sales units everywhere; availability
  ≥ 999999 = unknown but orderable (informational `AVAILABILITY_UNCONFIRMED`); empty availability = `NOT_AVAILABLE`
  (reject); `closingTime` = order cut-off for its delivery date; Valio calls paced at ≤ 5/s and a 429 pauses
  upstream calls for its block time while cached answers keep flowing.
- 2026-11-14 — **The gateway is built out as a product in this submission** (specs 22–38): one procurement state
  machine behind REST, MCP and A2A (shared principals, command idempotency, domain events, durable outbox);
  RFC 9457 recovery contract; per-agent rate limits; supplier adapters with a second (synthetic) supplier;
  deterministic sandbox; OpenTelemetry; buyer-side view; downloadable transaction evidence; the 400 L exception
  journey; hospital allergen-safe substitution; recipe baskets; signed webhooks; a real A2A 1.0 service;
  recurring orders with drift detection; proactive supplier proposals; MCP server card; a connect-your-agent kit.
  Pitch sentence: "Valio Aimo's agent-commerce gateway lets external buyer agents discover offers and place
  orders under buyer-defined authority, with human approval for exceptions and verifiable transaction records."
- 2026-11-14 — **The buyer grants its agent's authority as a signed W3C Verifiable Credential** (JWT, Ed25519);
  the gateway verifies it on every step, the operator's policy can only tighten it, and the buyer revokes it with a
  signed revocation (spec 12). **The audit chain's head hash is anchored on Polygon mainnet** in a plain
  transaction (no smart contract), so anyone can check the log against a public record; Gökalp funds and runs the
  anchoring wallet (spec 21).
- 2026-11-14 — **The trust protocol ships in this submission:** Ed25519-signed quotes and order receipts with a
  public key set and an offline verifier (spec 10), a hash-chained audit log with a signed head a buyer can keep
  as a checkpoint (spec 11), and a UBL 2.1 Order / OrderResponse export validated against the official OASIS
  schemas (spec 13). Orders state `commitment: gateway-simulated` so the promise is explicit. **Offer cards rank
  by the total cost of the packs bought**, overbuy shown, with each ranking factor's contribution (spec 20).
- 2026-11-14 — **Missing data never counts as proof.** An allergen exclusion passes a product only when the
  catalog has an allergen declaration for it that does not list the allergen; an explicit free-from
  declaration is shown as such. Products without allergen data and "may contain" products are excluded under
  that exclusion. Unknown codes (allergens, claims, labels, units) are refused with the valid values. The same
  rule applies to every other field the gateway relies on (spec 15).
- 2026-11-14 — **Credentials establish the principal.** Each agent gets a secret key, the console operator a
  token; approvals, policy changes and global views need the operator token, and every object belongs to the
  agent that created it. The `X-Agent-Id` header has no authority (spec 19).
- 2026-11-14 — **The buyer organisation's constraints bind every agent:** per-customer standing constraints
  are unioned with each request, and every quote is bound to a stored request (spec 16). **Approval applies to
  the evidence the approver saw** — changed evidence returns to the approver (spec 17). **An intent-only lookup
  broadens deterministically** and reports how many products it screened (spec 18).
- 2026-11-14 — **Delivery reliability and CO2 stay in the ranking** — both are organiser-provided per-product
  data and the brief rewards them; each offer shows the factors' contributions, and missing values get a
  neutral treatment. **The pitch keeps a 15-second recording** of A.I.mo answering the challenge sentence,
  framed as discovery versus delegated purchase.
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

## 2026-11-14 — Accounts, tenants and audiences (spec 43, builds on spec 42)
- Decision: the gateway is multi-tenant. Buyer businesses sign in with email + password (scrypt, session cookie +
  CSRF) and connect their own agents over MCP, REST, A2A, webhooks or a tool-calling pack. Remote MCP and A2A use
  OAuth 2.1 per the MCP authorization specification (2026-07-28, vendored), alongside API keys shown once.
  Each tenant's approvers approve that tenant's orders; Valio staff work as `platform_admin` across tenants.
- Why: the challenge is about outside agents buying on behalf of real businesses; a connection must be bound to
  exactly one account, and a buyer business owns its approvals. Gökalp chose OAuth + API keys, all connection
  types, tenant approvers and email + password.

## 2026-11-14 — Agent connections first (spec 42), accounts next (spec 43)
- Decision: spec 42 implements the console's Connect contract (`docs/CONNECT-API.md`): operator-issued agent keys
  (create / list / revoke) and MCP served inside the gateway at `/mcp`. It is built right away in a parallel lane and
  pushed to main because the deployed Connect page waits for it. The multi-tenant accounts work becomes spec 43 and
  extends the same `Connection` shape. Demo tenants: a hotel chain (the challenge's own example) and a hospital
  kitchen (allergen-safe substitution, spec 31).
- Why: Yusuf wrote and deployed the frontend against this contract; Gökalp chose to build it immediately.

## 2026-11-14 — Agent governance inside a buyer business (spec 44)
- Decision: every agent has an owner (new `member` role runs agents). Policy has three layers — platform (Valio),
  tenant guardrails (tenant admin), agent policy (owner) — and the most restrictive value wins, shown with its
  source. Tightening applies at once; loosening becomes a change request a tenant admin approves (never the
  requester). Policies are versioned, simulated against recent history, and every order records the version it
  ran under. Owners and admins can pause agents; alerts and a signed governance report close the loop.
- Why: Gökalp asked for a small AI governance layer: business owners see which person runs which agent and what it
  does, people manage their own agents' policies, admins control them.
