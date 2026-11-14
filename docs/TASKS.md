# Tasks

MVP first (see `DECISIONS.md`). Design decisions are revisited after the MVP runs end to end.

| # | Task | Owner | Status | Notes |
|---|------|-------|--------|-------|
| 1 | Challenge docs, agent docs, repo layout | Gökalp + Claude | done | |
| 2 | Preliminary submission: draft on Junction platform with this challenge picked (by 13.11 23:59) | Team | done | |
| 3 | Core 1–3 design (MVP cut) | Gökalp + Claude | done | `DECISIONS.md` |
| 4 | Backend scaffold (FastAPI), catalog → SQLite, Valio client, export `contracts/openapi.yaml` | Backend (Claude → Codex) | done | contract first: unblocks frontend |
| 5 | Lookup: search + live offers + Decisions fit score | Backend (Claude → Codex) | done | |
| 6 | Quote → order, policy YAML, approval endpoints, audit log | Backend (Claude → Codex) | done | |
| 7 | MCP server over the same core (search, offers, quote, order, status) | Backend (Claude → Codex) | done | |
| 8 | End-to-end demo: Claude as MCP client runs the 1,000 L milk request | Gökalp + Claude | done | `backend/demo/run_buyer_agent.sh`; verified live 13.11 |
| 12 | Better shortlist: score more catalog candidates with Decisions before live calls (agent searching only "milk" missed cheaper Valio Eila 1 L) | Backend | done | demo now picks 1,010 € auto-confirmed option instead of 1,708 € |
| 9 | Frontend: dashboard, buyer flow, approval inbox, orders, audit, policy | Frontend | done | Vite + React + TS + Tailwind; stats/policy/lookups wired, 5 s polling, multi-line quotes, editable policy; verified live 13.11; backend notes: `docs/FRONTEND.md` |
| 33 | Frontend: every contract endpoint on screen — plans + policy preview, preferences, timeline, cancel, UBL, proof, agents & mandates, trust & connect, product facts | Frontend | done | 14.11; fixture mode `npm run dev:mock` for design review |
| 34 | Frontend: Valio blue-and-white theme + pixel pass on cards, spacing and charts | Frontend (Claude + 3 subagents) | done | palette from valioaimo.fi; every route measured at 390/1024/1280/1440 with no page overflow or cut labels |
| 35 | Live server + deploy on push | Frontend + Claude | done | https://trustedtrade.vps.webdock.cloud — see `docs/DEPLOY.md` "Live demo server" |
| 36 | `OPENAI_API_KEY` on the live server (without it every order is held with NO_FIT_SCORE) | Gökalp | todo | add to `~/app/deploy/.env`, then `docker compose … up -d` |
| 37 | Frontend for graduated autonomy (spec 40): path, track record, recommendation, grant / withdraw | Frontend | done | `/agents`, autonomy chips on `/policy`; R-5 signer display waits for a contract field |
| 38 | Frontend: Connect page (`/connect`), rail with hover labels, phone menu, Schibsted Grotesk, forest status palette | Frontend | done | 14.11; degrades to `<YOUR_AGENT_KEY>` until #39 lands |
| 39 | Backend: `/v1/connections` (issue / list / revoke agent keys) and MCP served at `/mcp` in the gateway process | Gökalp | done | spec 42; contract in `docs/CONNECT-API.md`; AC-1–11 in `backend/tests/test_spec42_agent_connections.py` |
| 13 | Order cancel over REST + MCP (`POST /v1/orders/{id}/cancel`, tool `cancel_order`) | Backend | done | spec `docs/specs/04-order-cancel.md`, [issue #1](https://github.com/yusufornek/junction26-trusted-trade-agents/issues/1); verified live 13.11 |
| 14 | Spec-driven core: audit + specs 01–03 as built (`docs/specs/`) | Backend | done | reviewed one by one after the MVP |
| 15 | Size reconciliation — exact pack counts | Backend | done | spec `docs/specs/05-size-reconciliation.md` |
| 16 | Agent mandate — per-agent authority, cumulative limit | Backend | done | spec `docs/specs/06-agent-mandate.md` |
| 17 | Order timeline endpoint (`GET /v1/orders/{id}/timeline`) | Backend | done | spec `docs/specs/07-order-timeline.md` |
| 18 | Price-complete shortlist — stable best offer | Backend | done | spec `docs/specs/09-price-complete-shortlist.md` |
| 19 | Exceptions — fulfilment plans, policy preview, substitutes | Backend | done | spec `docs/specs/08-exceptions.md`; verified live 14.11 |
| 20 | Ordering requires a mandate (`403 AGENT_NOT_REGISTERED` for unregistered agents) | Backend (Claude → Codex) | done | spec `docs/specs/06-agent-mandate.md` R-2; verified live 14.11 |
| 21 | Demo reset script (`backend/scripts/reset_demo.py`) | Backend (Claude → Codex) | done | spec `docs/specs/14-demo-reset.md`; verified live 14.11 |
| 22 | Evidence completeness — missing data never counts as proof | Backend (Claude → Codex) | done | spec `docs/specs/15-evidence-completeness.md` |
| 23 | Standing constraints + bound quotes | Backend (Claude → Codex) | done | spec `docs/specs/16-standing-constraints.md` |
| 24 | Approval evidence revision | Backend (Claude → Codex) | done | spec `docs/specs/17-approval-evidence.md` |
| 25 | Lookup fallback + screening counts | Backend (Claude → Codex) | done | spec `docs/specs/18-lookup-fallback.md` |
| 26 | Authentication + ownership (+ console patch for review) | Backend (Claude → Codex) | done | spec 19 implemented; 11 authentication AC tests and frontend build pass; 16 legacy test failures conflict with the requested unchanged assertions, awaiting clarification |
| 27 | Ask the mentor about `closingTime`, Terminal stock 999999, shifted dates | Gökalp | todo | text ready in the morning notes |
| 28 | Signed quotes + receipts, audit chain, UBL export | Backend (Claude → Codex) | done | specs 10, 11, 13 |
| 29 | Total-cost ranking with factor breakdown | Backend (Claude → Codex) | done | spec `docs/specs/20-total-cost-ranking.md` |
| 30 | Verifiable mandate (buyer-issued W3C VC) | Backend (Claude → Codex) | done | spec `docs/specs/12-verifiable-mandate.md` |
| 31 | Audit anchoring on Polygon | Backend (Claude → Codex) | done | spec `docs/specs/21-audit-anchoring.md`; mainnet wallet funded by Gökalp |
| 32 | Gateway product build-out: specs 22–38 | Backend (Claude → Codex) | in progress | sequential build chain; live verification after each group |
| 10 | Business case + pitch deck | Business | todo | |
| 11 | Deploy + demo video | Team | todo | before 15.11 10:00 |
| 40 | Accounts, tenants, team management, OAuth, three audiences (spec 43) | Gökalp + Codex | building |
| 41 | Frontend: sign-in, team management (roles), connect tabs on buyer and Valio side, OAuth consent — data contract in FRONTEND.md "Accounts and connections (spec 43)" | frontend teammate | todo |
| 42 | Agent governance: ownership, policy layers, change requests, versions, simulation, register, pause, alerts, report (spec 44) | Gökalp + Codex | queued |
| 43 | Frontend: agent register, effective-policy view with sources, policy editor + change-request review, simulation result, pause buttons, alerts, governance report — data contract in FRONTEND.md "Agent governance (spec 44)" | frontend teammate | todo |
