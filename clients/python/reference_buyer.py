"""Deterministic MCP buyer recovery loop (shared with the spec 38 connect kit).

Inject an MCP call_tool coroutine and optionally a clock/sleeper. Recovery branches
use next_action alone; domain codes and human prose are never inspected.
"""
import asyncio
import argparse
import base64
import json
import os
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
from uuid import uuid4

from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED, REQUEST_TIMEOUT


class RecoveryRequired(Exception):
    def __init__(self, payload):
        self.payload = payload
        super().__init__(payload.get("next_action", "STOP"))


class ReferenceBuyer:
    def __init__(self, call_tool, *, sleep=asyncio.sleep, max_steps=100):
        self.call_tool, self.sleep, self.max_steps = call_tool, sleep, max_steps

    async def purchase(self, need, *, product_id=None, units=1, idempotency_key=None, scenario=None):
        key = idempotency_key or str(uuid4())
        command, arguments = "find_offers", {"need": need}
        lookup, quote_arguments = None, None
        for _ in range(self.max_steps):
            try:
                response = await self.call_tool(command, arguments)
                payload = response.structured_content
            except MCPError as error:
                if error.code not in {REQUEST_TIMEOUT, CONNECTION_CLOSED}:
                    raise
                payload = {"next_action": "RETRY_SAME_COMMAND", "retry_after_seconds": 1}
            except (TimeoutError, ConnectionError):
                # A lost response is ambiguous, so retry the exact same command/key.
                payload = {"next_action": "RETRY_SAME_COMMAND", "retry_after_seconds": 1}
            action = payload.get("next_action")
            if action == "RETRY_SAME_COMMAND":
                await self.sleep(payload["retry_after_seconds"])
                continue
            if action == "LOOKUP_REQUIRED":
                command, arguments = "find_offers", {"need": need}
                continue
            if action == "REFRESH_QUOTE":
                command, arguments = "create_quote", {**quote_arguments, "idempotency_key": str(uuid4())}
                continue
            if action == "WAIT_FOR_APPROVAL":
                await self.sleep(payload["poll_after_seconds"])
                command, arguments = "get_order_status", {"order_id": payload["id"]}
                continue
            if action is not None:
                raise RecoveryRequired(payload)
            if command == "find_offers":
                lookup = payload
                quote_arguments = {"customer_id": need["customer_id"], "deliver_by": need["deliver_by"],
                                   "need_ref": lookup["lookup_id"]}
                if scenario:
                    kind = "single" if scenario == "full_stock" else "split"
                    index = next((i for i, plan in enumerate(lookup["plans"]) if plan["kind"] == kind), None)
                    if index is None:
                        raise RuntimeError(f"Provision the {scenario} sandbox scenario: no {kind} plan available")
                    quote_arguments["plan_ref"] = {"lookup_id": lookup["lookup_id"], "plan_index": index}
                else:
                    chosen = product_id or lookup["offers"][0]["product_id"]
                    quote_arguments["lines"] = [{"product_id": chosen, "quantity_sales_units": units}]
                command, arguments = "create_quote", {**quote_arguments, "idempotency_key": str(uuid4())}
            elif command == "create_quote":
                command, arguments = "place_order", {"quote_id": payload["id"], "idempotency_key": key}
            else:
                return payload
        raise RuntimeError("Recovery step budget exceeded")


def verify_receipt(order, jwks, *, customer_id, agent_id):
    """Offline Ed25519 detached-JWS verification, including supplier/environment evidence."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    def unb64(value):
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    protected, detached, signature = order["receipt"].split(".")
    header = json.loads(unb64(protected))
    if detached or set(header) != {"alg", "kid"} or header["alg"] != "EdDSA":
        raise ValueError("Unsupported receipt JWS")
    if order["customer_id"] != customer_id or order["agent_id"] != agent_id or order["commitment"] not in {"gateway-simulated", "supplier-acknowledged"}:
        raise ValueError("Receipt scope or commitment mismatch")
    labels = {k: order[k] for k in ("supplier_id", "synthetic", "environment", "execution")
              if k in order and order[k] not in ("valio", "live", False)}
    payload = {**labels, "order_id": order["id"], **{k: order[k] for k in (
        "quote_id", "quote_signature_digest", "customer_id", "agent_id", "status", "currency",
        "decided_at", "decided_by_kind", "commitment")},
        "total": format(Decimal(str(order["total"])).normalize(), "f")}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    key = next(k for k in jwks["keys"] if k["kid"] == header["kid"])
    if key["kty"] != "OKP" or key["crv"] != "Ed25519":
        raise ValueError("Unsupported receipt key")
    Ed25519PublicKey.from_public_bytes(unb64(key["x"])).verify(unb64(signature), (protected + "." + encoded).encode())
    return payload


async def run_session(session, scenario, get_jwks, *, need=None, sleep=asyncio.sleep):
    """Same entry point for real MCP HTTP and in-process acceptance tests."""
    await session.initialize()
    tools = {tool.name for tool in (await session.list_tools()).tools}
    required = {"get_agent_context", "find_offers", "create_quote", "place_order", "get_order_status"}
    if not required <= tools:
        raise RuntimeError("Wrong MCP endpoint: required procurement tools missing")
    response = await session.call_tool("get_agent_context", {})
    context = response.structured_content
    if response.is_error:
        raise RecoveryRequired(context)
    if context["environment"] != "sandbox":
        raise ValueError("Reference scenarios require an issued sandbox credential and sandbox endpoint")
    customer = context["customer_ids"][0]
    today = (datetime.fromisoformat(context["fixture_clock"]).date() if context.get("fixture_clock")
             else datetime.now(ZoneInfo("Europe/Helsinki")).date())
    need = need or {"customer_id": customer, "intent": "lactose-free milk", "search_terms": ["milk"],
        "required_claims": ["FREE_FROM_LACTOSE"], "exclude_allergens": ["AE"],
        "quantity": {"value": 10 if scenario == "full_stock" else 1000, "unit": "L"},
        "deliver_by": (today + timedelta(days=3)).isoformat()}
    order = await ReferenceBuyer(session.call_tool, sleep=sleep).purchase(need, scenario=scenario)
    verify_receipt(order, await get_jwks(), customer_id=customer, agent_id=context["principal"]["id"])
    if order["status"] != "CONFIRMED":
        raise RuntimeError("Scenario did not complete a confirmed purchase")
    return order


async def main():
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    import httpx2
    parser = argparse.ArgumentParser(description="Deterministic sandbox MCP buyer; follows next_action and verifies the receipt.")
    parser.add_argument("--scenario", choices=["full_stock", "split_stock"], required=True)
    args = parser.parse_args()
    base = os.environ["GATEWAY_URL"].rstrip("/")
    headers = {"Authorization": "Bearer " + os.environ["AGENT_TOKEN"]}
    if os.getenv("AGENT_MANDATE"):
        headers["Agent-Mandate"] = os.environ["AGENT_MANDATE"]
    async with httpx2.AsyncClient(headers=headers) as http:
        async def jwks():
            response = await http.get(base + "/.well-known/jwks.json")
            response.raise_for_status()
            return response.json()
        async with streamable_http_client(base + "/mcp", http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                order = await run_session(session, args.scenario, jwks)
    print(json.dumps({"order_id": order["id"], "status": order["status"], "receipt_verified": True}))


if __name__ == "__main__":
    asyncio.run(main())
