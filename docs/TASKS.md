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
| 13 | Order cancel endpoint over REST (core has `Orders.cancel`, contract does not) | Backend | todo | frontend has no screen for it until the endpoint exists |
| 10 | Business case + pitch deck | Business | todo | |
| 11 | Deploy + demo video | Team | todo | before 15.11 10:00 |
