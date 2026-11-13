# AGENTS.md — Junction 2026: Valio Aimo Trusted Trade Agents

Shared instructions for every coding agent working in this repo (Codex, Claude and subagents).
Read this file, `docs/CHALLENGE.md`, `docs/DECISIONS.md` and `docs/TASKS.md` before starting work.
`PROJECT.md` (Turkish) is the team's verified API reference; `VALIO-AIMO-CHALLENGE-TR.md` is the brief in Turkish.

## Context
- Junction 2026 entry for the **Valio Aimo — Trusted Trade Agents** challenge (see `docs/CHALLENGE.md`).
- Final submission deadline: **Sun 15.11 10:00 (+02:00)**. Time is the scarcest resource: a **working,
  demoable end-to-end flow** (request → lookup → order → approval) first, polish later.
- The team communicates in Turkish; code, comments, commit messages and new docs are in English.

## Team and ownership
| Area | Owner | Folder |
|---|---|---|
| Backend + AI agent | Gökalp (with Claude + Codex) | `backend/` |
| API contract | backend owns, frontend reviews | `contracts/openapi.yaml` |
| UX + frontend | frontend teammate | `frontend/` |
| Business case, pitch | business teammate | `docs/` (pitch material) |
| Challenge docs, data | shared | `docs/`, `data/`, `PROJECT.md` |

Only write inside your own area. Contract changes go through `contracts/openapi.yaml` and are announced
to the other side.

## Agent roles
- **Claude (main session)** — advisor and orchestrator: discusses decisions with the user, splits work,
  writes task briefs, reviews and verifies results, fixes mistakes.
- **Codex** — primary builder: writes code, UI, visuals and assets from Claude's briefs.
- **Claude subagents** — research (APIs, datasets, docs), independent review, tests and verification.

## Rules for agents
1. Stay inside the scope of your brief and your area's folder. Don't refactor or add files outside it.
2. Before finishing: run the build / tests / lint that exist and report the results honestly.
3. End with a short summary: files changed, how to run it, anything left undone or uncertain.
4. Never commit new secrets. Use `.env.local` (gitignored) and document new variables in `.env.example`.
5. Don't install heavy dependencies without a reason; prefer what the stack already has.
6. If a decision is needed that isn't in `docs/DECISIONS.md`, stop and ask instead of guessing.
7. Don't edit `docs/DECISIONS.md` — Claude records decisions there after agreeing them with the user.
   You may update your own task's line in `docs/TASKS.md`.

## Stack
- **Backend:** Python + FastAPI (OpenAPI generated from pydantic models), SQLite for orders/approvals/audit
  log, AI agent via Claude tool use, optional MCP server over the same operations.
- **Frontend:** chosen by the frontend owner; talks to the backend only through `contracts/openapi.yaml`.

## Commands
Backend (from `backend/`, venv at `backend/.venv`, deps in `requirements.txt`; secrets in repo-root `.env.local`):
```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # once
.venv/bin/python -m app.catalog build                  # data/ → var/app.db (~2 s)
.venv/bin/python -m uvicorn app.api:app --port 8000    # REST API, docs at /docs
.venv/bin/python -m app.mcp_server                     # MCP over stdio (--http --port 8001 for HTTP)
.venv/bin/python -m pytest tests -q                    # tests, no network
.venv/bin/python scripts/export_openapi.py             # refresh contracts/openapi.yaml
```
Frontend (from `frontend/`, Vite + React + TS + Tailwind v4; needs the gateway on port 8000):
```
npm install                                            # once
npm run dev                                            # http://localhost:5173
npm run build                                          # typecheck + production build
```
