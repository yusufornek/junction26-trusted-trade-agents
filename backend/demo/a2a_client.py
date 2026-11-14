"""Run the split-order journey using the A2A 1.0 SDK client.

Run from backend: .venv/bin/python demo/a2a_client.py --help
The external operator approves through the gateway's authenticated review flow.
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from a2a import types as t
from a2a.client import ClientConfig, ClientFactory
from google.protobuf.json_format import MessageToDict, ParseDict


def part(value):
    return ParseDict({"data": value, "mediaType": "application/json"}, t.Part())


def artifact(task, name):
    return MessageToDict(next(a for a in task.artifacts if a.name == name).parts[0])["data"]


async def journey(base_url, token, need, *, mandate=None, sleep=asyncio.sleep, poll_limit=120):
    headers = {"Authorization": "Bearer " + token, "A2A-Version": "1.0"}
    if mandate:
        headers["Agent-Mandate"] = mandate
    async with httpx.AsyncClient(headers=headers, timeout=60) as http:
        response = await http.get(base_url.rstrip("/") + "/.well-known/agent-card.json")
        response.raise_for_status()
        card = ParseDict(response.json(), t.AgentCard())
        client = ClientFactory(ClientConfig(httpx_client=http, streaming=False,
                                supported_protocol_bindings=["JSONRPC"])).create(card)

        async def send(name, task=None, **data):
            # Keep this message id across retries, including a lost placement response.
            request = t.SendMessageRequest(message=t.Message(message_id=uuid4().hex,
                task_id=task.id if task else "", context_id=task.context_id if task else "",
                role=t.Role.ROLE_USER, parts=[part({"version": 1, "command": name, **data})]))
            for attempt in range(6):
                try:
                    async for result in client.send_message(request):
                        if result.HasField("task"):
                            return result.task
                except (httpx.TransportError, TimeoutError):
                    if attempt == 5:
                        raise
                    await sleep(1)
                except Exception as error:
                    recovery = getattr(error, "data", {}) or {}
                    # SDK 1.0 errors expose typed ErrorInfo details.
                    if isinstance(recovery, list):
                        recovery = next((d.get("metadata", {}) for d in recovery if d.get("metadata", {}).get("recovery_version")), {})
                    if recovery.get("next_action") != "RETRY_SAME_COMMAND" or attempt == 5:
                        raise
                    await sleep(recovery["retry_after_seconds"])
            raise RuntimeError("A2A command produced no task")

        task = await send("lookup", need=need)
        offers = artifact(task, "offers")
        split = next((index for index, plan in enumerate(offers["plans"]) if plan["kind"] == "split"), None)
        if split is None:
            raise RuntimeError("This request has no split plan; use a split-stock sandbox scenario or a larger need")
        task = await send("select_plan", task, plan_index=split)
        task = await send("place_order", task)
        for _ in range(poll_limit):
            if task.status.state != t.TaskState.TASK_STATE_AUTH_REQUIRED:
                break
            order = artifact(task, "order")
            print(f"Waiting for authenticated review: {base_url}/approvals/{order['id']}?revision={int(order['evidence_revision'])}", file=sys.stderr)
            await sleep(5)
            task = await client.get_task(t.GetTaskRequest(id=task.id))
        if task.status.state == t.TaskState.TASK_STATE_COMPLETED:
            task = await send("get_evidence", task)
        return MessageToDict(task)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--token", default=os.getenv("AGENT_TOKEN"))
    parser.add_argument("--mandate", type=Path)
    parser.add_argument("--need", type=Path, help="Structured NeedSpec JSON; default: 400 L lactose-free milk")
    args = parser.parse_args()
    if not args.token:
        parser.error("Pass --token or AGENT_TOKEN")
    need = json.loads(args.need.read_text()) if args.need else {"customer_id": "1", "intent": "lactose-free milk",
        "search_terms": ["milk"], "required_claims": ["FREE_FROM_LACTOSE"], "exclude_allergens": [],
        "quantity": {"value": 400, "unit": "L"}, "deliver_by": str(date.today() + timedelta(days=3))}
    print(json.dumps(asyncio.run(journey(args.url, args.token, need,
                    mandate=args.mandate.read_text().strip() if args.mandate else None)), indent=2))


if __name__ == "__main__":
    main()
