# 38 — Connect your agent

Status: in progress · Decided on 2026-11-14 · Design reviewed with the external architect (round 4).

## Purpose
Someone outside the backend team goes from an issued sandbox credential to a first simulated order and a
verified receipt by following the published instructions.

## Requirements
- **R-1 `/developers`** (served by the backend, static HTML generated from the repository docs): endpoints and
  transports (REST base URL, MCP stdio command and streamable HTTP URL, A2A URL), authentication, required
  request fields and units, standing constraints, quote expiry, approval lifecycle, recovery contract, rate
  limits, sandbox scenarios, evidence verification.
- **R-2 Context:** `GET /v1/me` and MCP `get_agent_context` return the caller's principal, customers, mandate
  summary, effective limits and remaining budget, environment, rate limits and supported operations.
- **R-3 Reference client:** `clients/python/reference_buyer.py` — MCP, deterministic (no LLM), pinned
  dependencies, `--scenario full_stock | split_stock`, follows `next_action` only; plus `.env.example`.
- **R-4 `llms.txt`** at `/llms.txt`: navigation for LLM agents to the OpenAPI document, tool descriptions, rules
  and the developer page; executable constraints live in tool schemas and policy.
- **R-5 Client configurations:** tested MCP client configuration snippets (Claude Code / Claude Desktop,
  generic `mcp` CLI) and a `curl` walkthrough for REST.
- **R-6 Generated docs:** tool and field tables are generated from the same schemas as the tools
  (`backend/scripts/build_developer_docs.py`); a test fails when docs and schemas diverge.
- **R-7 Failure guidance:** expired credential, wrong endpoint or transport, unsupported protocol version,
  missing scope and reconnect each have a documented symptom and next step.

## Acceptance criteria
- **AC-1** With a sandbox credential the reference client connects, lists tools, completes a `full_stock`
  purchase and verifies the receipt (in-process test).
- **AC-2** `split_stock` run ends in a pending approval, waits, and completes after approval.
- **AC-3** `GET /v1/me` shows scope, limits and environment; an expired credential answers `401` with guidance.
- **AC-4** `/developers` and `/llms.txt` are served and their links resolve.
- **AC-5** Generated tables equal the live tool schemas (divergence test).
- **AC-6** The walkthrough commands in the page are executed by a test against the in-process gateway.
