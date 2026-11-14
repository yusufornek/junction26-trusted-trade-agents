"""MCP exception journey, with a resumable placement key and a demo transcript.

The sandbox reviewer is a separate, explicitly scripted human-side action. Live
mode only polls; an authorised person decides through the normal approval inbox.
"""
import argparse
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile
from uuid import uuid4

import httpx
import httpx2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import audit
from app.auth import Principal
from app.foundation import PRINCIPAL
from app.mcp_server import create_server
from app.sandbox import scenario_gateway


def reference_need(customer="hotel"):
    return {"customer_id": customer, "intent": "400 L lactose-free milk",
            "search_terms": ["milk"], "required_claims": ["FREE_FROM_LACTOSE"],
            "quantity": {"value": 400, "unit": "L"},
            "deliver_by": str(audit.utcnow().date() + timedelta(days=3))}


class ExceptionJourney:
    def __init__(self, call_tool, *, sleep=asyncio.sleep, emit=print, save=lambda state: None,
                 max_polls=360, max_retries=3):
        self.call_tool, self.sleep, self.emit, self.save = call_tool, sleep, emit, save
        self.max_polls, self.max_retries = max_polls, max_retries

    async def call(self, name, arguments):
        for attempt in range(self.max_retries + 1):
            try:
                result = await self.call_tool(name, arguments)
                payload = result.structured_content
                if not result.is_error:
                    return payload
                if payload.get("next_action") != "RETRY_SAME_COMMAND":
                    raise RuntimeError(json.dumps(payload))
                delay = payload["retry_after_seconds"]
            except (TimeoutError, ConnectionError, httpx.TransportError, httpx2.TransportError):
                delay = 1
            if attempt == self.max_retries:
                raise RuntimeError("Response still uncertain; resume with the saved state and the same key")
            self.emit(f"Retrying {name} with the same arguments and idempotency key")
            await self.sleep(delay)

    async def run(self, need=None, *, state=None):
        state = state if state is not None else {}
        if not state:
            state.update(need=need or reference_need(), placement_key=str(uuid4()), quote_key=str(uuid4()))
            self.save(state)
        need = state.get("need")
        if not state.get("order_id"):
            if not state.get("quote_id"):
                if not state.get("quote_arguments"):
                    lookup = await self.call("find_offers", {"need": need})
                    plans = [(i, p) for i, p in enumerate(lookup["plans"]) if p["kind"] == "split"
                             and p["uncovered"]["value"] == 0]
                    if not plans:
                        raise RuntimeError("No complete split plan is available; no order was placed")
                    index, plan = min(plans, key=lambda item: item[1]["total"])
                    self.emit(f"Lookup {lookup['lookup_id']}: split plan at {plan['total']:.2f} EUR")
                    self.emit(plan["shortage"]["reason"])
                    # Persist the exact quote command before sending it, too.
                    state["quote_arguments"] = {"customer_id": need["customer_id"], "deliver_by": need["deliver_by"],
                        "plan_ref": {"lookup_id": lookup["lookup_id"], "plan_index": index},
                        "idempotency_key": state["quote_key"]}
                    self.save(state)
                quote = await self.call("create_quote", state["quote_arguments"])
                state["quote_id"] = quote["id"]
                self.save(state)
            order = await self.call("place_order", {"quote_id": state["quote_id"],
                                                    "idempotency_key": state["placement_key"]})
            state["order_id"] = order["id"]
            self.save(state)
        else:
            self.emit(f"Resuming order {state['order_id']}")
            order = await self.call("get_order_status", {"order_id": state["order_id"]})
        for _ in range(self.max_polls):
            self.emit(f"Order {order['id']}: {order['status']} (revision {order['order_revision']})")
            if order["next_action"] != "WAIT_FOR_APPROVAL":
                break
            self.emit(f"{order['approval_reason']} Polling after {order['poll_after_seconds']} seconds")
            await self.sleep(order["poll_after_seconds"])
            order = await self.call("get_order_status", {"order_id": order["id"]})
        else:
            if order["status"] == "PENDING_APPROVAL":
                raise RuntimeError("Still pending; resume using the saved order id")
        self.emit(f"Outcome: {order['status']}; next_action={order['next_action']}; comment={order.get('comment')}")
        receipt = await self.call("get_order_receipt", {"order_id": order["id"]})
        evidence = await self.call("get_order_evidence", {"order_id": order["id"]})
        self.emit(f"Receipt id: {receipt['id']}; signed receipt: {receipt['receipt']}")
        self.emit("Evidence manifest: " + json.dumps(evidence, sort_keys=True))
        return {"order": order, "receipt": receipt, "evidence": evidence}


async def run_cli(args, state, save):
    if args.live:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        secret = os.environ.get(args.token_env)
        if not secret:
            raise RuntimeError(f"Set {args.token_env} to the agent's MCP bearer credential")
        headers = {"Authorization": "Bearer " + secret}
        if args.mandate:
            headers["Agent-Mandate"] = args.mandate.read_text().strip()
        async with httpx2.AsyncClient(headers=headers, timeout=60) as http:
            async with streamable_http_client(args.mcp_url, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await ExceptionJourney(session.call_tool, save=save).run(reference_need(args.customer), state=state)
    with scenario_gateway("split_stock") as gateway:
        server = create_server(gateway, "sandbox-agent")

        async def sandbox_reviewer(seconds):
            await asyncio.sleep(seconds)
            if not state.get("order_id"):
                return
            order = gateway.orders.get_order(state["order_id"], "sandbox-agent")
            if order.status != "PENDING_APPROVAL":
                return
            print("Sandbox human-side reviewer (scripted): approves the current evidence")
            token = PRINCIPAL.set(Principal("sandbox-reviewer", operator=True, kind="operator", role="valio_operator"))
            try:
                gateway.orders.approve(order.id, "Sandbox reviewer", "Reviewed the split plan and budget",
                                       evidence_revision=order.evidence_revision)
            finally:
                PRINCIPAL.reset(token)

        return await ExceptionJourney(server.call_tool, sleep=sandbox_reviewer, save=save).run(state=state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Use a running live MCP gateway; never approve automatically")
    parser.add_argument("--mcp-url", default="http://127.0.0.1:8001/mcp")
    parser.add_argument("--token-env", default="AGENT_SECRET", help="Environment variable holding the live agent credential")
    parser.add_argument("--mandate", type=Path, help="Optional buyer VC-JWT file")
    parser.add_argument("--customer", default="1", help="Live customer id")
    parser.add_argument("--state", type=Path, help="Live restart state outside the repository; contains ids and retry keys")
    args = parser.parse_args()
    if args.state and args.state.resolve().is_relative_to(Path(__file__).resolve().parents[2]):
        parser.error("Keep scratch state outside the repository, for example under $TMPDIR")
    if args.state and not args.live:
        parser.error("--state resumes a running live gateway; sandbox state is temporary per run")
    if args.state:
        target = args.state
    else:
        with NamedTemporaryFile(prefix="exception-journey-", suffix=".json", delete=False) as file:
            target = Path(file.name)
    state = json.loads(target.read_text()) if target.exists() and target.stat().st_size else {}

    def save(value):
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2))
        temporary.replace(target)

    print(f"Journey state: {target}")
    asyncio.run(run_cli(args, state, save))


if __name__ == "__main__":
    main()
