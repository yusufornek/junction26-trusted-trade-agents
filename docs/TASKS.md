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
| 12 | Better shortlist: score more catalog candidates with Decisions before live calls (agent searching only "milk" missed cheaper Valio Eila 1 L) | Backend | todo | found in MCP demo |
| 9 | Frontend: approval inbox + order/audit view | Frontend | todo | against `contracts/openapi.yaml` |
| 10 | Business case + pitch deck | Business | todo | |
| 11 | Deploy + demo video | Team | todo | before 15.11 10:00 |
