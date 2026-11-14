# Connector test results

Live checks of agent clients connecting to the Luotsi gateway at `https://luotsi.online/mcp`.
Each run gets a row in **Summary**, its issues go to **Open findings** (reuse an ID when a later run hits
the same issue), and its details get a section below, newest last.
Never paste agent keys here: record the agent ID and customer scope only.

## Summary

| # | Date | Connector / client | Transport | Auth | Result |
|---|---|---|---|---|---|
| 1 | 2026-11-14 | Raw JSON-RPC from Claude Code (baseline, no MCP client) | Streamable HTTP, direct | Agent key | ✅ Pass; findings F-1 to F-4 |
| 2 | 2026-11-14 | Claude Desktop, `claude_desktop_config.json` + `mcp-remote` 0.16.6 (macOS) | stdio → Streamable HTTP | Agent key in `env` | ✅ Pass; connect page config works unchanged |

## Open findings

| ID | Severity | Finding | Suggested fix | Runs |
|---|---|---|---|---|
| F-1 | Medium | `find_offers` ranks offers whose order cut-off has passed (`CUTOFF_PASSED`) above deliverable ones, and those offers still report `available_date` = the requested date and `enough: true`. An agent that picks the cheapest entry from `offers` (not `plans`) can choose an offer that cannot arrive in time. | Drop such offers or rank them last; report the next deliverable date and `enough: false`. | 1 |
| F-2 | Low | `enough: true` is returned while `availability_known: false`. It means "Valio accepts any quantity", not confirmed stock; only the `AVAILABILITY_UNCONFIRMED` warning says so. | Return `enough: null` when stock is unknown. | 1 |
| F-3 | Low | Offers priced from the list price (`net_price_found: false`) are ranked together with customer net prices. | Flag or rank list-price offers separately. | 1 |
| F-4 | Info | `POOL_TRUNCATED`: 40 of 1,443 matching products were screened, so the best offer is the best of the screened pool. | Expected; agents should add search terms. | 1 |

## 1. Raw JSON-RPC from Claude Code — 2026-11-14

**Why first:** checks the server itself before any connector config. JSON-RPC was posted straight to `/mcp`;
the Claude Desktop setup from the connect page (`mcp-remote` bridge) was not exercised.

**Principal:** agent `claude-test-desktop`, customer scope `1`, environment `live`, mandate
`approval_required`, limits 1,500 EUR per order and 5,000 EUR per rolling 24 h (0 spent).

### Protocol

| Check | Result |
|---|---|
| `initialize` | ✅ HTTP 200, plain JSON (no SSE), no `Mcp-Session-Id`: stateless |
| `tools/list` | ✅ 10 tools: `get_agent_context`, `search_products`, `find_offers`, `find_basket_offers`, `create_quote`, `place_order`, `get_order_status`, `get_order_evidence`, `get_order_ubl`, `cancel_order` |
| `get_agent_context` | ✅ principal, mandate, limits, remaining budget, rate limits (lookup 60/min, mutation 120/min, read 600/min) |

### Happy path: 50 L lactose-free milk for Mon 16.11

`search_products("lactose free milk")` gave the claim ID `FREE_FROM_LACTOSE`; `find_offers` then ran with that claim,
`search_terms: ["milk"]` and 50 L. 1,443 products matched, 17 were priced, 5 returned.

| Rank | Product (ID) | Units | Net EUR | EUR/L | Notes |
|---|---|---|---|---|---|
| 1 | Valio Arki Eila semi skimmed 1 l, lactose free (464306) | 50 × ST | 50.50 | 1.010 | order by Sun 15.11 17:00 |
| 2 | Valio Arki Eila skimmed 1 l, lactose free (464401) | 50 × ST | 55.00 | 1.100 | order by Sun 15.11 17:00 |
| 3 | Valio Eila skimmed 1.75 l, lactose free (469119) | 29 × ST | 60.32 | 1.189 | ⚠️ `CUTOFF_PASSED` (12.11 15:00), list price |
| 4 | Valio Eila semi skimmed 1 l, lactose free (465039) | 10 × KI (5 L) | 68.60 | 1.372 | ⚠️ `CUTOFF_PASSED` (12.11 15:00) |
| 5 | Valio Eila 3 % 1 l, lactose free (463335) | 50 × ST | 70.00 | 1.400 | order by Sun 15.11 17:00 |

- `plans[0]` picks rank 1 (50.50 EUR net, 57.50 EUR with VAT) with `policy_preview: PENDING_APPROVAL` (`autonomy_level`).
- Every offer carries `AVAILABILITY_UNCONFIRMED`. Ranks 3 and 4 use the `Terminal` logistic model; the rest use `Stock`.
- A repeat of the same query made 0 live calls (cached); a fresh query made 1 price and 1 availability call.

### Validation and policy (customer 1)

| Input | Expected | Result |
|---|---|---|
| `deliver_by` in the past | reject | ✅ `VALIDATION_ERROR`, `retryable: false` |
| Unknown claim code | reject | ✅ `UNKNOWN_CODE`, lists the valid codes |
| 2,000 L (2,020 EUR net) | over the per-order limit | ✅ `PENDING_APPROVAL` with `autonomy_level` and `max_auto_total_eur` |

### Authorization boundary

| Request | Expected | Result |
|---|---|---|
| Invalid key | 401 | ✅ 401, RFC 9457 body, `WWW-Authenticate` with `resource_metadata` (RFC 9728) |
| No `Authorization` header | 401 | ✅ 401, same body as an invalid key |
| Valid key, customer `2` | reject | ✅ `CUSTOMER_NOT_IN_MANDATE`, no offers returned |

### Not tested

- `create_quote`, `place_order`, approval, `cancel_order`, evidence and UBL export: they write state on the live server.
- The OAuth discovery link (`resource_metadata`) end to end.
- Whether an unknown customer ID gets a different error than one outside the mandate (enumeration).

## 2. Claude Desktop with `mcp-remote` — 2026-11-14

**Setup:** the connect page config, unchanged, in `claude_desktop_config.json` on macOS:
`npx -y mcp-remote https://luotsi.online/mcp --header Authorization:${VALIO_AIMO_AUTH}`, key in `env`.
Node 26.3 and npx 11.16 from Homebrew; `mcp-remote` resolved to 0.16.6. Same agent and mandate as run 1.

| Check | Result |
|---|---|
| Bridge from a terminal, same args | ✅ `initialize` in 2.7 s (includes the npx start), 10 tools, `get_agent_context` returns `claude-test-desktop` |
| Desktop restart | ✅ server started and connected in about 1 s; no errors or warnings in `mcp-server-luotsi.log` |
| Tool discovery | ✅ Desktop started two bridge processes: one only listed tools, prompts and resources; the other served the test chat |
| Connect page test prompt | ✅ reported working by the tester; the log shows 5 `tools/call`, each answered, and no transport errors |
| Claude Code sessions in the Desktop app | ✅ also list the 10 tools after the restart, with no `.mcp.json` or `~/.claude.json` entry: the Desktop config is shared with the Code tab |

- The client sends `protocolVersion: 2025-11-25` and advertises the `io.modelcontextprotocol/ui` extension.
- `mcp-remote` rewrites `clientInfo.name` to `<client> (via mcp-remote 0.16.6)`, so the gateway can tell bridged clients apart.

### Not captured

- Which 5 tools were called, with what arguments, and whether a quote or order was attempted: `mcp-remote` 0.16.6 and
  the Desktop logs record method names only. Only the gateway side can show them.
- Claude's final answer, so whether F-1 changed the recommended offer is unknown.

### Not tested

- Windows (the `Authorization:${VAR}` form without a space is the `mcp-remote` workaround for Windows argument splitting).
- Machines where Node comes only from nvm, where a GUI app may not find `npx`.
