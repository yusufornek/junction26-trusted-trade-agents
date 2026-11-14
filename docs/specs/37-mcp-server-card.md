# 37 — MCP server card and discovery

Status: in progress · Decided on 2026-11-14 · Design reviewed with the external architect (round 4).

## Purpose
A client that has only the gateway's address finds out how to connect.

## Requirements
- **R-1 Server card:** follow the MCP server-card extension at a pinned draft commit (recorded in `/v1/meta` and
  in the card). With `/mcp` as the transport endpoint, serve `/mcp/server-card` with media type
  `application/mcp-server-card+json`.
- **R-2 Catalog:** `/.well-known/ai-catalog.json` points to the server card, the A2A agent card, the OpenAPI
  document, `/llms.txt` and the JWKS.
- **R-3 Contents:** identity, version, transports and endpoints from the pinned schema; gateway-specific metadata
  (JWKS URL, policy documentation URL, supported environments) in a clearly named extension block. Tools are
  discovered through MCP's runtime listing.
- **R-4 HTTP behaviour:** ETag and `Cache-Control`, conditional requests, CORS for public metadata.
- **R-5 Consistency:** a startup check and a test compare the card with the running server (name, version,
  transports); customer-specific authority stays behind authentication (`GET /v1/me`, `get_agent_context`).
- **R-6 Labelled as draft:** the card states the extension's draft status and commit.

## Acceptance criteria
- **AC-1** A discovery client follows catalog → server card → MCP endpoint and lists tools; server name and
  version match the card.
- **AC-2** The card validates against the pinned schema (vendored).
- **AC-3** `If-None-Match` with the current ETag → `304`.
- **AC-4** The catalog links resolve (server card, agent card, OpenAPI, llms.txt, JWKS).
- **AC-5** No customer-specific data in public metadata.
