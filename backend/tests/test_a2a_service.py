"""Spec 34 acceptance through the installed A2A 1.0 SDK client and server."""
import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from a2a import types as t
from a2a.client.transports.jsonrpc import JsonRpcTransport
from a2a.utils.errors import InvalidParamsError
from google.protobuf.json_format import MessageToDict, ParseDict

from app.api import create_app
from app.a2a import data_part, metadata
from app.auth import Principal
from conftest import agent_headers, OPERATOR_HEADERS


@asynccontextmanager
async def sdk(gateway, agent="external", headers=None):
    app = create_app(gateway)
    app.state.gateway = gateway
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway.test",
                                headers={"A2A-Version": "1.0", **agent_headers(agent), **(headers or {})}) as http:
        response = await http.get("/.well-known/agent-card.json")
        assert response.status_code == 200, response.text
        card = ParseDict(response.json(), t.AgentCard())
        yield JsonRpcTransport(http, card, "http://gateway.test/a2a", is_shared_httpx=True), http, card


def command(name, key, task=None, **data):
    return t.SendMessageRequest(message=t.Message(message_id=key, task_id=task.id if task else "",
        context_id=task.context_id if task else "", role=t.Role.ROLE_USER,
        parts=[data_part({"version": 1, "command": name, **data})]))


def artifact_data(task, name):
    return MessageToDict(next(a for a in task.artifacts if a.name == name).parts[0])["data"]


async def lookup(client, need, key="lookup"):
    return (await client.send_message(command("lookup", key, need=need.model_dump(mode="json")))).task


async def purchase(client, need, *, prefix="journey", plan_index=0):
    task = await lookup(client, need, prefix + ":lookup")
    plan_index = next(i for i, plan in enumerate(artifact_data(task, "offers")["plans"]) if len(plan["lines"]) == 1 and plan["lines"][0]["product_id"] == "1")
    task = (await client.send_message(command("select_plan", prefix + ":select", task, plan_index=plan_index))).task
    task = (await client.send_message(command("place_order", prefix + ":place", task))).task
    return task


def test_ac_1_card_valid_1_0_bearer_shared_skills(gateway):
    async def run():
        async with sdk(gateway) as (_, http, card):
            assert card.supported_interfaces[0].protocol_version == "1.0"
            assert card.supported_interfaces[0].protocol_binding == "JSONRPC"
            assert card.security_schemes["BearerAuth"].http_auth_security_scheme.scheme == "bearer"
            assert "BearerAuth" in card.security_requirements[0].schemes
            assert card.capabilities.streaming and card.capabilities.push_notifications
            assert {s.id for s in card.skills} == {"find_offers", "find_basket_offers", "quote_and_order", "order_status", "cancel_order", "get_evidence"}
            from app.mcp_server import create_server
            tools = {tool.name: tool for tool in await create_server(gateway).list_tools()}
            schemas = MessageToDict(card.capabilities.extensions[0])["params"]["schemas"]
            assert schemas["find_offers"]["input"] == tools["find_offers"].input_schema
            # The SDK's proto-derived JSON schema validates the public card too.
            from a2a.server.routes._proto_schema import message_schema
            import jsonschema
            components = {}
            schema = message_schema(t.AgentCard.DESCRIPTOR, components)
            schema["components"] = {"schemas": components}
            jsonschema.validate((await http.get("/.well-known/agent-card.json")).json(), schema)
            # Missing/wrong wire version is refused by the SDK server helper.
            response = await http.post("/a2a", headers={"A2A-Version": "0.3"}, json={"jsonrpc": "2.0", "id": 1, "method": "ListTasks", "params": {}})
            assert "error" in response.json()
    asyncio.run(run())


def test_ac_2_lookup_selection_placement_and_state_artifacts(gateway, need, upstream, monkeypatch):
    async def run():
        small = need.model_copy(update={"quantity": need.quantity.model_copy(update={"value": 10})})
        async with sdk(gateway) as (client, http, _):
            task = await lookup(client, small)
            assert task.status.state == t.TaskState.TASK_STATE_INPUT_REQUIRED
            offers = artifact_data(task, "offers")
            assert offers["offers"] and offers["plans"]
            assert metadata(task)["context"]["customer_id"] == "1"
            task = (await client.send_message(command("select_plan", "select", task, plan_index=0))).task
            assert task.status.state == t.TaskState.TASK_STATE_INPUT_REQUIRED
            assert artifact_data(task, "quote")["signature"]
            task = (await client.send_message(command("place_order", "place", task))).task
            assert task.status.state == t.TaskState.TASK_STATE_COMPLETED
            assert artifact_data(task, "order")["status"] == "CONFIRMED"
            assert artifact_data(task, "receipt")["receipt"]
            task = (await client.send_message(command("get_evidence", "evidence", task))).task
            evidence = artifact_data(task, "evidence")
            assert evidence["order_id"] == artifact_data(task, "order")["id"]
            assert evidence["signature"] and evidence["supplier_acceptance"] == "simulated"
            quote_file = next(file for file in evidence["files"] if file["name"] == "quote.json")
            quote_response = await http.get(quote_file["href"])
            assert quote_response.status_code == 200
            assert quote_response.json()["signature"]
            from hashlib import sha256
            assert sha256(quote_response.content).hexdigest() == quote_file["sha256"]
            states = [json.loads(r["payload"])["task"]["status"]["state"] for r in gateway.a2a.updates(task.id, Principal("external"))]
            assert "TASK_STATE_WORKING" in states and states[-1] == "TASK_STATE_COMPLETED"
            # Missing request details are an interactive task, not a server crash.
            missing = (await client.send_message(command("lookup", "missing"))).task
            assert missing.status.state == t.TaskState.TASK_STATE_INPUT_REQUIRED
            # Completed protocol tasks cannot be canceled; a new command applies
            # the existing order cancellation rules and releases reservations.
            from a2a.utils.errors import TaskNotCancelableError
            with pytest.raises(TaskNotCancelableError):
                await client.cancel_task(t.CancelTaskRequest(id=task.id))
            canceled = (await client.send_message(command("cancel", "cancel-confirmed", order_id=artifact_data(task, "order")["id"], reason="Buyer changed plans"))).task
            assert canceled.id != task.id and canceled.status.state == t.TaskState.TASK_STATE_CANCELED
            assert artifact_data(canceled, "order")["status"] == "CANCELLED"
            assert (await client.get_task(t.GetTaskRequest(id=task.id))).status.state == t.TaskState.TASK_STATE_COMPLETED
            with pytest.raises(InvalidParamsError) as refused:
                await client.send_message(command("cancel", "cancel-again", order_id=artifact_data(task, "order")["id"]))
            failed = await client.get_task(t.GetTaskRequest(id=refused.value.data["data"]["task_id"]))
            assert failed.status.state == t.TaskState.TASK_STATE_FAILED
    asyncio.run(run())
    # Exercise the shipped reference client's high-level SDK client over an
    # in-process app with a genuine split plan and finite mocked stock.
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace
    module_spec = importlib.util.spec_from_file_location("a2a_demo", Path(__file__).parents[1] / "demo/a2a_client.py")
    demo = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(demo)
    app = create_app(gateway)
    app.state.gateway = gateway
    monkeypatch.setattr(demo, "httpx", SimpleNamespace(AsyncClient=lambda **kw: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), **kw), TransportError=httpx.TransportError))
    upstream.model, upstream.amount = "Stock", 0
    upstream.amounts = {"1": 2, "4": 1}
    gateway.valio.cache.clear()
    request = need.model_dump(mode="json")
    request["quantity"]["value"] = 20
    from conftest import AGENT_TOKENS
    result = asyncio.run(demo.journey("http://gateway.test", AGENT_TOKENS["external"], request, poll_limit=0))
    assert result["status"]["state"] == "TASK_STATE_COMPLETED"
    order = next(a["parts"][0]["data"] for a in result["artifacts"] if a["name"] == "order")
    assert {line["product_id"] for line in order["lines"]} == {"1", "4"}


def test_ac_3_pending_approval_then_completed_receipt(gateway, need, upstream):
    async def run():
        async with sdk(gateway) as (client, http, _):
            task = await purchase(client, need)
            assert task.status.state == t.TaskState.TASK_STATE_AUTH_REQUIRED
            order = artifact_data(task, "order")
            assert order["next_action"] == "WAIT_FOR_APPROVAL"
            response = await http.post(f"/v1/approvals/{order['id']}/approve", headers=OPERATOR_HEADERS,
                json={"approver": "Ada", "evidence_revision": order["evidence_revision"]})
            assert response.status_code == 200, response.text
            current = await client.get_task(t.GetTaskRequest(id=task.id))
            assert current.status.state == t.TaskState.TASK_STATE_COMPLETED
            assert artifact_data(current, "receipt")["receipt"] == response.json()["receipt"]
    asyncio.run(run())


def test_ac_4_message_id_idempotency_payload_conflict_and_durability(gateway, need, upstream):
    async def run():
        async with sdk(gateway) as (client, http, _):
            task = await lookup(client, need)
            task = (await client.send_message(command("select_plan", "select", task, plan_index=0))).task
            request = command("place_order", "stable-message", task)
            first = (await client.send_message(request)).task
            second = (await client.send_message(request)).task
            assert first.id == second.id
            assert artifact_data(first, "order")["id"] == artifact_data(second, "order")["id"]
            with gateway.db.connect() as conn:
                assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
                assert conn.execute("SELECT COUNT(*) FROM audit WHERE action='PLACE_ORDER'").fetchone()[0] == 1
            changed = MessageToDict(request)
            changed["message"]["parts"][0]["data"]["unexpected"] = "changed"
            response = await http.post("/a2a", headers={"A2A-Version": "1.0"}, json={"jsonrpc": "2.0", "id": 2, "method": "SendMessage", "params": changed})
            assert response.json()["error"]["data"][0]["metadata"]["code"] == "IDEMPOTENCY_CONFLICT"
            from app.a2a import ProcurementA2A
            gateway.a2a = ProcurementA2A(gateway)
            assert (await client.get_task(t.GetTaskRequest(id=first.id))).id == first.id
    asyncio.run(run())


def test_ac_5_other_agent_cannot_get_list_subscribe_or_push_task(gateway, need, upstream):
    async def run():
        async with sdk(gateway) as (client, _, _):
            task = await lookup(client, need)
        async with sdk(gateway, "other") as (_, http, _):
            for method, params in [("GetTask", {"id": task.id}), ("CancelTask", {"id": task.id}),
                ("SubscribeToTask", {"id": task.id}), ("GetTaskPushNotificationConfig", {"taskId": task.id, "id": "x"})]:
                response = await http.post("/a2a", headers={"A2A-Version": "1.0"}, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
                assert response.json()["error"]["code"] == -32001, response.text
            response = await http.post("/a2a", headers={"A2A-Version": "1.0"}, json={"jsonrpc": "2.0", "id": 1, "method": "ListTasks", "params": {}})
            assert not response.json()["result"].get("tasks")
            response = await http.post("/a2a", headers={"Authorization": "Bearer wrong", "A2A-Version": "1.0"}, json={"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": task.id}})
            assert response.status_code == 401 and response.json()["code"] == "UNAUTHENTICATED"
    asyncio.run(run())


def test_ac_6_sdk_streaming_outbox_replay_push_and_cancel(gateway, need, upstream, respx_mock, monkeypatch):
    import socket
    calls = []
    monkeypatch.setattr(gateway.webhooks, "resolver", lambda *a, **kw: [(socket.AF_INET, 1, 6, "", ("93.184.216.34", 443))])
    def receive(request):
        assert request.headers["Authorization"] == "Bearer receiver-secret"
        body = json.loads(request.content)
        if body.get("type") == "webhook.challenge":
            return httpx.Response(200, json={"challenge": body["challenge"]})
        calls.append(request)
        return httpx.Response(200)
    respx_mock.post("https://93.184.216.34/push").mock(side_effect=receive)
    async def run():
        async with sdk(gateway) as (client, http, _):
            # A slow command publishes WORKING before supplier I/O completes;
            # another SDK call can read it while the purchasing writer is busy.
            import threading
            entered, release = threading.Event(), threading.Event()
            operation = gateway.lookup.find_offers
            def paused(*args, **kwargs):
                entered.set()
                assert release.wait(5), "Test failed to release supplier lookup"
                return operation(*args, **kwargs)
            monkeypatch.setattr(gateway.lookup, "find_offers", paused)
            request = command("lookup", "working-probe", need=need.model_dump(mode="json"))
            request.configuration.return_immediately = True
            running = await client.send_message(request)
            assert await asyncio.to_thread(entered.wait, 2)
            try:
                current = await client.get_task(t.GetTaskRequest(id=running.task.id))
                assert current.status.state == t.TaskState.TASK_STATE_WORKING
                listed = await client.list_tasks(t.ListTasksRequest(include_artifacts=True))
                assert listed.tasks[0].id == current.id
            finally:
                release.set()
            for _ in range(100):
                if (await client.get_task(t.GetTaskRequest(id=running.task.id))).status.state != t.TaskState.TASK_STATE_WORKING:
                    break
                await asyncio.sleep(.01)
            monkeypatch.setattr(gateway.lookup, "find_offers", operation)
            updates = [event async for event in client.send_message_streaming(command("lookup", "stream", need=need.model_dump(mode="json")))]
            statuses = [event.status_update for event in updates if event.HasField("status_update")]
            assert [s.status.state for s in statuses] == [t.TaskState.TASK_STATE_WORKING, t.TaskState.TASK_STATE_INPUT_REQUIRED]
            task = await client.get_task(t.GetTaskRequest(id=statuses[0].task_id))
            config = await client.create_task_push_notification_config(t.TaskPushNotificationConfig(task_id=task.id,
                url="https://receiver.test/push", token="receiver-token", authentication={"scheme": "Bearer", "credentials": "receiver-secret"}))
            assert (await client.get_task_push_notification_config(t.GetTaskPushNotificationConfigRequest(task_id=task.id, id=config.id))).url == config.url
            assert len((await client.list_task_push_notification_configs(t.ListTaskPushNotificationConfigsRequest(task_id=task.id))).configs) == 1
            task = (await client.send_message(command("select_plan", "stream-select", task, plan_index=0))).task
            assert calls
            from app.webhooks import verify_webhook
            assert all(verify_webhook(r.content, r.headers, gateway.signing.jwks()) for r in calls)
            assert all("statusUpdate" in json.loads(r.content) for r in calls)
            assert all(r.headers["X-A2A-Notification-Token"] == "receiver-token" for r in calls)
            cursor = MessageToDict(statuses[-1])["metadata"]["aggregate_revision"]
            http.headers["X-A2A-After-Revision"] = str(int(cursor))
            resumed = [event async for event in client.subscribe(t.SubscribeToTaskRequest(id=task.id))]
            resumed_statuses = [e.status_update for e in resumed if e.HasField("status_update")]
            assert [int(MessageToDict(s)["metadata"]["aggregate_revision"]) for s in resumed_statuses] == [3, 4]
            http.headers.pop("X-A2A-After-Revision")
            http.headers["Last-Event-ID"] = MessageToDict(resumed_statuses[-1])["metadata"]["event_id"]
            # An interrupted task can be canceled before it owns an order.
            canceled = await client.cancel_task(t.CancelTaskRequest(id=task.id))
            assert canceled.status.state == t.TaskState.TASK_STATE_CANCELED
            events = [e async for e in client.subscribe(t.SubscribeToTaskRequest(id=task.id))]
            assert [int(MessageToDict(e.status_update)["metadata"]["aggregate_revision"]) for e in events if e.HasField("status_update")] == [5, 6]
            await client.delete_task_push_notification_config(t.DeleteTaskPushNotificationConfigRequest(task_id=task.id, id=config.id))
            assert not (await client.list_task_push_notification_configs(t.ListTaskPushNotificationConfigsRequest(task_id=task.id))).configs
    asyncio.run(run())


def test_ac_8_throttle_returns_shared_recovery_and_task_status(gateway, need, upstream):
    async def run():
        from app.db import DomainError
        consume = gateway.limiter.consume
        def throttle(principal, category):
            if category == "lookup":
                raise DomainError("RATE_LIMITED", "Wait", 429, {"retry_after_seconds": 7})
            return consume(principal, category)
        gateway.limiter.consume = throttle
        async with sdk(gateway, headers={"X-Correlation-Id": "a2a-throttle"}) as (_, http, _):
            response = await http.post("/a2a", headers={"A2A-Version": "1.0"}, json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": MessageToDict(command("lookup", "throttled", need=need.model_dump(mode="json")))})
            assert "error" in response.json(), response.text
            recovery = response.json()["error"]["data"][0]["metadata"]
            assert recovery["code"] == "RATE_LIMITED" and recovery["retry_after_seconds"] == 7
            assert recovery["retryable"] and recovery["next_action"] == "RETRY_SAME_COMMAND"
            assert recovery["correlation_id"] == "a2a-throttle"
            task_response = await http.post("/a2a", headers={"A2A-Version": "1.0"}, json={"jsonrpc": "2.0", "id": 2, "method": "GetTask", "params": {"id": recovery["data"]["task_id"]}})
            status_data = task_response.json()["result"]["status"]["message"]["parts"][1]["data"]
            assert status_data["next_action"] == "RETRY_SAME_COMMAND" and status_data["retry_after_seconds"] == 7
            gateway.limiter.consume = consume
            recovered = await http.post("/a2a", json={"jsonrpc": "2.0", "id": 3, "method": "SendMessage", "params": MessageToDict(command("lookup", "throttled", need=need.model_dump(mode="json")))})
            assert recovered.json()["result"]["task"]["id"] == recovery["data"]["task_id"]
            assert recovered.json()["result"]["task"]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
            with gateway.db.connect() as conn:
                assert conn.execute("SELECT COUNT(*) FROM a2a_tasks").fetchone()[0] == 1
            operation = gateway.lookup.find_offers
            def crashed(*args, **kwargs):
                raise RuntimeError("private upstream implementation detail")
            gateway.lookup.find_offers = crashed
            try:
                response = await http.post("/a2a", json={"jsonrpc": "2.0", "id": 4, "method": "SendMessage", "params": MessageToDict(command("lookup", "unexpected-failure", need=need.model_dump(mode="json")))})
            finally:
                gateway.lookup.find_offers = operation
            failure = response.json()["error"]["data"][0]["metadata"]
            assert failure["code"] == "INTERNAL_ERROR" and "private upstream" not in response.text
            failed = await http.post("/a2a", json={"jsonrpc": "2.0", "id": 5, "method": "GetTask", "params": {"id": failure["data"]["task_id"]}})
            assert failed.json()["result"]["status"]["state"] == "TASK_STATE_FAILED"
    asyncio.run(run())
