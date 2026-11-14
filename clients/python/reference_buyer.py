"""Deterministic MCP buyer recovery loop (shared with the spec 38 connect kit).

Inject an MCP call_tool coroutine and optionally a clock/sleeper. Recovery branches
use next_action alone; domain codes and human prose are never inspected.
"""
import asyncio
from uuid import uuid4


class RecoveryRequired(Exception):
    def __init__(self, payload):
        self.payload = payload
        super().__init__(payload.get("next_action", "STOP"))


class ReferenceBuyer:
    def __init__(self, call_tool, *, sleep=asyncio.sleep, max_steps=100):
        self.call_tool, self.sleep, self.max_steps = call_tool, sleep, max_steps

    async def purchase(self, need, *, product_id=None, units=1, idempotency_key=None):
        key = idempotency_key or str(uuid4())
        command, arguments = "find_offers", {"need": need}
        lookup, quote_arguments = None, None
        for _ in range(self.max_steps):
            try:
                response = await self.call_tool(command, arguments)
                payload = response.structured_content
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
                chosen = product_id or lookup["offers"][0]["product_id"]
                quote_arguments = {"customer_id": need["customer_id"], "deliver_by": need["deliver_by"],
                                   "need_ref": lookup["lookup_id"],
                                   "lines": [{"product_id": chosen, "quantity_sales_units": units}]}
                command, arguments = "create_quote", {**quote_arguments, "idempotency_key": str(uuid4())}
            elif command == "create_quote":
                command, arguments = "place_order", {"quote_id": payload["id"], "idempotency_key": key}
            else:
                return payload
        raise RuntimeError("Recovery step budget exceeded")
