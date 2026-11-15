# Luotsi — a trusted trade gateway for AI purchasing agents

Junction 2026 entry for the **Valio Aimo — Trusted Trade Agents** challenge. Live at **https://luotsi.online**.

Luotsi is the gateway between external AI purchasing agents and Valio Aimo's systems. An agent from a hotel chain, a hospital kitchen or any assistant (claude.ai, ChatGPT, Claude Desktop, custom agents) connects over open standards, looks up products, prices and availability, commits at a fixed price, and either orders on its own within its authority or sends the order to a human for sign-off. Every step is signed and recorded.

It is an AI gateway, not a chatbot and not a new webshop UI.

## The flow

```
Request  →  Lookup  →  Fixed-price quote  →  Order  →  Agent decides, or a human signs off
```

Example from the brief: *"I need 1,000 litres of lactose-free milk by Monday."*
Luotsi screens the catalog first, prices only a shortlist live (the mock APIs are never queried for all products), ranks plans on price, coverage, delivery reliability, CO₂ and fit, and returns Valio Eila lactose-free 1 L (€1,010, arrives in time) first. Within its limits the agent's order is `CONFIRMED` directly; 3,000 L goes to `PENDING_APPROVAL` because it exceeds the €1,500 per-order limit.

## What it does

**Core**
- **Lookup:** structured search, pack conversion (litres → sales units), deadline and cut-off checks, ranked plans with the reason for each rank.
- **Fixed-price ordering:** signed, time-limited quotes; orders accept the quote unchanged; idempotent retries, stock recheck.
- **Permissions and approval:** per-agent mandates (customers, suppliers, per-order and rolling 24 h limits, autonomy level), buyer-signed W3C Verifiable Credentials, human approval inbox with two-step confirmation, pause / resume, revocation.

**Beyond the core**
- Partial availability and good / better / best plans; substitutes with an explicit trade-off.
- Recipe baskets (need → recipe → order) with on-hand deduction; last-minute events with late-line alternatives.
- Stock-out watch, proactive supplier proposals, purchase memory and recurring orders with drift checks.
- Allergen- and nutrition-safe substitution: hard rules are enforced in code, never left to a model.
- Anomaly checks on quantity, price, order speed and new categories.
- Connection trust: verified clients, owner confirmation, trading contract and a "driving test" for new agents.
- Explainability: per-order timeline, signed receipts, evidence bundles, hash-chained audit log anchored on Polygon PoS.

**Where AI is used:** a fit score ("is this the kind of product the buyer asked for?") and plain-language request interpretation. AI suggests and interprets; deterministic code decides.

## Open standards

| Area | Standard |
|---|---|
| Agent interfaces | MCP (`/mcp`, 100+ tools, resources, prompts, elicitation), A2A 1.0, REST with OpenAPI 3.1 |
| Sign-in for assistants | OAuth 2.1 with PKCE, DCR, client ID metadata documents, resource indicators |
| Trust | Ed25519 / JWS signatures, JWKS, W3C Verifiable Credentials 2.0, hash-chained audit, Polygon anchoring |
| Documents and events | UBL 2.1 orders, Standard Webhooks, RFC 9457 errors with `next_action` |
| Operations | OpenTelemetry, Docker Compose, GitHub Actions deploy |

## Repository

| Folder | Content |
|---|---|
| `backend/` | FastAPI gateway, MCP and A2A servers, policy engine, SQLite store, tests |
| `frontend/` | Operator and buyer console (Vite, React, TypeScript, Tailwind) |
| `contracts/openapi.yaml` | API contract |
| `docs/` | Challenge, decisions, specs (`docs/specs/`), deploy and connection guides |
| `deploy/` | Docker Compose and Caddy configuration |

## Run locally

Backend (from `backend/`; secrets in a repo-root `.env.local`, see `.env.example`):
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m app.catalog build
.venv/bin/python -m uvicorn app.api:app --port 8000      # API docs at /docs
.venv/bin/python -m pytest tests -q
```

Console (from `frontend/`):
```bash
npm install && npm run dev                                # http://localhost:5173
```

## Connect an agent

Add `https://luotsi.online/mcp` as a connector in claude.ai, ChatGPT or Claude Desktop and choose **hotel-chain-agent** on the consent screen. Then open **Connect** in the console and press **"Yes, this is the same agent"** on the new connection so orders within limits can confirm without approval.

All data is synthetic. Orders are simulated against Valio Aimo's mock APIs; no production systems are involved.
