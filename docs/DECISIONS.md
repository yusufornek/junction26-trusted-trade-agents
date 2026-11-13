# Decisions

Agreed decisions, newest first. Format: `YYYY-MM-DD — decision — why`.

- 2026-11-13 — Work continues **only in this repo** (yusufornek/junction26-trusted-trade-agents); the earlier
  Daption-ciray/junction repo is abandoned and its agent docs were moved here — one place for data, docs and code.
- 2026-11-13 — Repo layout: `backend/` (backend + AI owner), `frontend/` (frontend owner),
  `contracts/openapi.yaml` (shared API contract, owned by backend), `docs/` shared — parallel writers never
  touch the same files, and the frontend can mock against the contract before the backend is ready.
- 2026-11-13 — Backend stack: **Python + FastAPI** — OpenAPI comes out of the code (openness criterion),
  pydantic schemas, easy data work, Python MCP SDK available.
- 2026-11-13 — Team split: **Gökalp (+ Claude/Codex) = backend + AI agent**; a frontend teammate = UX + frontend;
  a business teammate = business side (pitch, model, Valio Aimo fit). Backend and frontend share one repo —
  clear ownership keeps parallel work from colliding.
- 2026-11-13 — Challenge: **Valio Aimo — Trusted Trade Agents** (details in `docs/CHALLENGE.md`) — chosen by
  the team; API key and portal password kept in `.env.local`, not in the repo.
- 2026-11-13 — Shared agent memory lives in plain files
  (`AGENTS.md`, `CLAUDE.md`, `docs/`) — fast, zero-risk, readable by both Claude and Codex.
- 2026-11-13 — Gökalp's personal note vault stays outside this repo — keeps the submission repo free of hooks
  and vault folders.
- 2026-11-13 — Working model: Claude = advisor/orchestrator/reviewer, Codex = builder,
  Claude subagents = research/review/tests.
