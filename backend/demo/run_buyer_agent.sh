#!/usr/bin/env bash
# Demo: Claude (Claude Code CLI, headless) acts as an outside hotel-chain buyer agent and talks to our
# MCP gateway only. Usage: demo/run_buyer_agent.sh ["buyer request text"]
set -euo pipefail
BACKEND="$(cd "$(dirname "$0")/.." && pwd)"
AGENT_ID="${AGENT_ID:-hotel-chain-agent}"
REQUEST="${1:-I need 1,000 litres of lactose-free milk by Monday 2026-11-16. Find an option that meets our requirements based on price and availability.}"
WORK="$(mktemp -d)"   # run outside the repo so no project instructions leak into the buyer agent
trap 'rm -rf "$WORK"' EXIT
cat > "$WORK/mcp.json" <<JSON
{"mcpServers": {"valio": {"command": "$BACKEND/.venv/bin/python",
  "args": ["-m", "app.mcp_server", "--agent-id", "$AGENT_ID"], "env": {"PYTHONPATH": "$BACKEND"}}}}
JSON
SYSTEM="You are the procurement agent of a hotel chain (customer id 1). You buy from Valio Aimo only through the
valio MCP tools. Turn the buyer's request into a structured need (hard constraints as catalog claims, e.g.
FREE_FROM_LACTOSE; quantity with unit; deliver_by date), call find_offers, pick the best option that fully meets
the need, create a fixed-price quote with the lookup_id as need_ref, and place the order with a unique
idempotency key. You may not negotiate prices. If the order needs human approval, say so and why. Finish with a
short report for the buyer: what you ordered, quantity, total, order status, and the reasons behind the choice."
cd "$WORK"
claude -p "$REQUEST" --append-system-prompt "$SYSTEM" \
  --mcp-config "$WORK/mcp.json" --strict-mcp-config \
  --allowedTools "mcp__valio__search_products,mcp__valio__find_offers,mcp__valio__create_quote,mcp__valio__place_order,mcp__valio__get_order_status" \
  --disallowedTools "Bash,Edit,Write,Read,WebFetch,WebSearch,Glob,Grep,Task,Agent"
echo
echo "== Audit log (latest 8 events)"
"$BACKEND/.venv/bin/python" - "$BACKEND/var/app.db" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
for ts, actor, action, summary in reversed(con.execute(
        "SELECT ts, actor, action, summary FROM audit ORDER BY id DESC LIMIT 8").fetchall()):
    print(f"{ts[11:19]}  {actor:<26} {action:<14} {summary}")
PY
