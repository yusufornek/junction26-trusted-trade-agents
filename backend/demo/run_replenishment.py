"""Offline monthly milk demo; uses temporary sandbox state and never backend/var.

From backend: .venv/bin/python demo/run_replenishment.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.mcp_server import create_server
from app.sandbox import scenario_gateway


async def run(gateway):
    server = create_server(gateway, "sandbox-agent")
    suggestions = await server.call_tool("get_replenishment_suggestions", {"horizon": 60})
    if suggestions.is_error:
        raise RuntimeError(str(suggestions))
    rows = suggestions.structured_content["result"]
    suggestion = rows[0]
    print(suggestion["sentence"])
    args = {"suggestion_id": suggestion["id"]}
    prepared = await server.call_tool("prepare_suggestion", args)
    if prepared.is_error:
        raise RuntimeError(str(prepared))
    lookup = prepared.structured_content
    print(f"Prepared lookup {lookup['lookup_id']} with {len(lookup['plans'])} plans; no quote or order created.")
    recurring = await server.call_tool("create_recurring_from_suggestion", args)
    if recurring.is_error:
        raise RuntimeError(str(recurring))
    row = recurring.structured_content
    print(f"Created recurring workflow {row['id']}: {row['cadence']['rrule']}; baseline {row['template']['quantity']}.")


def main():
    with scenario_gateway("monthly_milk") as gateway:
        asyncio.run(run(gateway))


if __name__ == "__main__":
    main()
