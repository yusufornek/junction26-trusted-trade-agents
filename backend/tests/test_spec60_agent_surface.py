"""Eleven focused acceptance checks for the real SDK agent surface, offline."""
import asyncio
import copy
import importlib.util
import json
from datetime import timedelta
from pathlib import Path

import pytest
import httpx
from a2a import types as at
from fastapi.testclient import TestClient
from google.protobuf.json_format import MessageToDict, ParseDict
from jsonschema import Draft202012Validator
from mcp import Client
from mcp import types as mt
from mcp.shared.subscriptions import ResourceUpdated

from app import audit
from app.a2a import data_part, metadata
from app.agent_surface import PROMPTS, canonical_origin, rest_routes, route_audiences, surface
from app.agent_surface_cards import EXTENSION, mcp_card
from app.api import create_app
from app.auth import Principal
from app.foundation import PRINCIPAL
from app.mcp_server import create_server
from app.models import QuoteRequest
from conftest import OPERATOR_HEADERS, agent_headers, demo_catalog


def buyer(gateway, agent="external"):
    return gateway.accounts.bind(Principal(agent, customer_ids=("1",)))


def pending_order(gateway, need):
    lookup = gateway.find_offers(need, actor="agent:external")
    quote = gateway.orders.create_quote(QuoteRequest(customer_id="1", deliver_by=need.deliver_by,
        need_ref=lookup.lookup_id, lines=[{"product_id": "1", "quantity_sales_units": 200}]), "external")
    return gateway.orders.place_order(quote.id, "surface-order", "external")


def rpc(http, headers, method, params=None):
    params = {**(params or {}), "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {}}}
    return http.post("/mcp", headers={**headers, "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2026-07-28",
        "MCP-Method": method, **({"MCP-Name": params["name"]} if "name" in params else {})},
        json={"jsonrpc": "2.0", "id": 7, "method": method, "params": params})


def test_AC_1_generated_rest_parity_and_real_reads(gateway, tmp_path):
    async def run():
        s = surface(gateway)
        app = s.rest_app()
        # A future buyer route must join discovery without a manual tool mapping.
        from fastapi import Depends
        from app.auth import resolve_principal
        @app.get("/v1/surface-future", response_model=dict)
        def future(p=Depends(resolve_principal)):
            return {"customer_ids": list(p.customer_ids)}
        app.openapi_schema = None
        s.registry = None
        report = {}
        for audience in ("buyer", "tenant", "operator"):
            server = create_server(gateway, "external", audience=audience)
            tools = await server.list_tools()
            mapped = {key for tool in tools for key in (tool.meta or {}).get("restCapabilities", [])}
            expected = {method + " " + r.path for r in rest_routes(app) if audience in route_audiences(r)
                        for method in r.methods & {"GET", "POST", "PUT", "PATCH", "DELETE"}}
            assert expected <= mapped, sorted(expected - mapped)
            report[audience] = sorted(expected)
        (tmp_path / "rest-mcp-parity.json").write_text(json.dumps(report, indent=2))
        server = create_server(gateway, "external")
        product = await server.call_tool("get_product", {"id": "1"})
        assert not product.is_error and product.structured_content["id"] == "1"
        assert (await server.call_tool("future", {})).structured_content == {"customer_ids": ["1"]}
        assert (await server.call_tool("list_orders", {})).structured_content == {"result": []}
    asyncio.run(run())


def test_AC_2_all_tools_describe_annotations_and_structured_schemas(gateway):
    async def run():
        for audience in ("buyer", "tenant", "operator"):
            tools = await create_server(gateway, "external", audience=audience).list_tools()
            for tool in tools:
                assert tool.title and "Use this tool when" in tool.description and "Next call" in tool.description and "Recovery codes:" in tool.description
                assert tool.output_schema and tool.output_schema["type"] == "object"
                hints = tool.annotations.model_dump(by_alias=True)
                assert all(isinstance(hints[k], bool) for k in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"))
                if tool.name.startswith(("get_", "list_", "query_", "compare_", "search_")):
                    assert hints["readOnlyHint"], tool.name
            assert next(t for t in tools if t.name == "cancel_order").annotations.destructive_hint
        result = await create_server(gateway, "external").call_tool("get_product", {"id": "1"})
        schema = next(t.output_schema for t in await create_server(gateway, "external").list_tools() if t.name == "get_product")
        Draft202012Validator(schema).validate(result.structured_content)
    asyncio.run(run())


def test_AC_3_sdk_resource_subscription_receives_order_approval(gateway, need, upstream):
    order = pending_order(gateway, need)
    assert order.status == "PENDING_APPROVAL"
    async def run():
        server = create_server(gateway, "external")
        uri = f"luotsi://orders/{order.id}"
        async with Client(server) as client:
            read = await client.read_resource(uri)
            assert json.loads(read.contents[0].text)["status"] == "PENDING_APPROVAL"
            inbox = await client.read_resource("luotsi://approvals/pending")
            assert order.id in inbox.contents[0].text
            async with client.listen(resource_subscriptions=[uri]) as updates:
                await asyncio.to_thread(gateway.orders.approve, order.id, "Ada")
                event = await asyncio.wait_for(anext(updates), 5)
                assert isinstance(event, ResourceUpdated) and event.uri == uri
            fresh = await client.read_resource(uri, cache_mode="refresh")
            assert json.loads(fresh.contents[0].text)["status"] == "CONFIRMED"
            templates = await client.list_resource_templates()
            assert {str(t.uri_template) for t in templates.resource_templates} == {
                "luotsi://orders/{id}", "luotsi://orders/{id}/timeline", "luotsi://orders/{id}/evidence", "luotsi://catalog/products/{id}"}
    asyncio.run(run())


def test_AC_4_six_sdk_prompts_render_current_buyer_limit(gateway):
    async def run():
        async with Client(create_server(gateway, "external")) as client:
            prompts = await client.list_prompts()
            assert {p.name for p in prompts.prompts} == set(PROMPTS)
            prompt = await client.get_prompt("order_for_need", {"customer_id": "1", "job": "Milk for breakfast"})
            text = prompt.messages[0].content.text
            assert "1500" in text and "standing constraints" in text and "confirm_interpretation" in text
    asyncio.run(run())


def test_AC_5_sdk_elicitation_and_input_required_tier_substitute_and_decision(gateway, need, upstream):
    lookup = gateway.find_offers(need, actor="agent:external")
    best = next(o for o in lookup.offers if o.rank_label == "best")
    async def run():
        questions = []
        async def answer(ctx, params):
            questions.append(params.requested_schema)
            return mt.ElicitResult(action="accept", content={"tier": "best"} if "tier" in params.requested_schema["properties"] else {"confirm": True})
        # A legacy duplex client supports server-initiated elicitation/create.
        async with Client(create_server(gateway, "external"), elicitation_callback=answer, mode="legacy") as client:
            chosen = await client.call_tool("choose_offer", {"lookup_id": lookup.lookup_id})
            assert chosen.structured_content["tier"] == "best"
            substitute = await client.call_tool("confirm_substitute", {"lookup_id": lookup.lookup_id, "product_id": best.product_id})
            assert substitute.structured_content["confirmed"]
        assert len(questions) == 2
        # A modern client without elicitation support drives the same typed
        # questions explicitly through the SDK's requestState round trip.
        async with Client(create_server(gateway, "external")) as client:
            for tool, arguments, content in [
                ("choose_offer", {"lookup_id": lookup.lookup_id}, {"tier": "best"}),
                ("confirm_substitute", {"lookup_id": lookup.lookup_id, "product_id": best.product_id}, {"confirm": True})]:
                first = await client.session.call_tool(tool, arguments, allow_input_required=True)
                assert isinstance(first, mt.InputRequiredResult)
                final = await client.session.call_tool(tool, arguments, request_state=first.request_state,
                    input_responses={"decision": mt.ElicitResult(action="accept", content=content)})
                assert not final.is_error
        # The existing human decision still uses its single-use evidence-bound
        # confirmation, with elicitation as the supported transport.
        order = await asyncio.to_thread(pending_order, gateway, need)
        p = gateway.accounts.bind(gateway.settings.keys.resolve("test-operator-token", gateway.policy.get()))
        token = PRINCIPAL.set(p)
        try:
            async with Client(create_server(gateway, agent_id=None, audience="operator"), elicitation_callback=answer, mode="legacy") as client:
                decision = await client.call_tool("approve_order", {"order_id": order.id})
                assert not decision.is_error and decision.structured_content["status"] == "CONFIRMED"
        finally:
            PRINCIPAL.reset(token)
        assert len(questions) == 3
    asyncio.run(run())


def test_AC_6_a2a_schema_examples_and_cards_match_live_sdk(gateway):
    from app.a2a import agent_card
    from app.discovery import EXTENSION as MCP_EXTENSION, BACKEND
    async def run():
        for audience in ("buyer", "tenant", "operator"):
            value = MessageToDict(await agent_card(gateway, audience=audience))
            ParseDict(value, at.AgentCard())  # The installed pinned A2A 1.0 protobuf schema.
            schemas = next(e["params"]["schemas"] for e in value["capabilities"]["extensions"] if e["uri"] == EXTENSION)
            assert all(skill["examples"] and schemas[skill["id"]]["input"] and schemas[skill["id"]]["output"] for skill in value["skills"])
            assert set(value["securitySchemes"]) == {"BearerAuth", "OAuth2"}
        card = await mcp_card(gateway)
        tools = await create_server(gateway, "external").list_tools()
        assert card["_meta"][MCP_EXTENSION]["tools"] == [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools]
        assert len(card["_meta"][MCP_EXTENSION]["prompts"]) == 6
        schema = json.loads((BACKEND / "vendor/mcp-server-card/schema.json").read_text())
        Draft202012Validator({**schema, "$ref": "#/$defs/ServerCard"}).validate(card)
    asyncio.run(run())


def test_AC_7_client_setup_uses_only_canonical_live_origin(gateway):
    gateway.settings.public_url = "https://luotsi.online"
    with TestClient(create_app(gateway), base_url="http://localhost") as http:
        for client in ("claude-desktop", "claude-code", "chatgpt", "cursor", "vs-code", "generic"):
            response = http.get("/v1/connect/clients/" + client, headers=agent_headers("external"))
            assert response.status_code == 200, response.text
            setup = response.json()
            assert setup["url"] == "https://luotsi.online/mcp"
            assert setup["auth"]["resource"] == setup["url"] and setup["auth"]["audience"] == "buyer"
            assert setup["url"] in setup["snippet"] and "localhost" not in setup["snippet"]
            if client == "chatgpt":
                assert json.loads(setup["snippet"])["authentication"] == "oauth"
            # A harmless authenticated read proves the published URL is the
            # actual gateway's transport, without external network traffic.
            reply = http.post(setup["url"], headers={**agent_headers("external"), "Accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_agent_context", "arguments": {}}})
            assert reply.status_code == 200 and not reply.json()["result"].get("isError")


def test_AC_8_connection_health_harmless_probe_and_unchanged_demo(gateway, need, upstream, tmp_path):
    demo_catalog(gateway, tmp_path, upstream)
    with TestClient(create_app(gateway), base_url="http://localhost") as http:
        connection = http.post("/v1/connections", headers={**OPERATOR_HEADERS, "Idempotency-Key": "surface-connection"},
            json={"agent_id": "hotel-chain-agent", "client": "claude_code", "customer_ids": ["1"]}).json()
        cid = connection["connection_id"]
        headers = {"Authorization": "Bearer " + connection["key"]}
        first = rpc(http, headers, "tools/call", {"name": "get_product", "arguments": {"id": "missing"}})
        assert first.json()["result"]["isError"]
        probe = http.post(f"/v1/connections/{cid}/test", headers=OPERATOR_HEADERS)
        assert probe.status_code == 200, probe.text
        assert probe.json()["successful_read"] and probe.json()["scope"]["customer_ids"] == ["1"]
        assert not upstream.price_calls and not upstream.availability_calls
        health = http.get(f"/v1/connections/{cid}/health", headers=OPERATOR_HEADERS).json()
        assert health["last_used_at"] and health["calls_last_24h"]["get_agent_context"] == 1 and health["error_codes"]
        lookup = http.post("/v1/offers", headers=headers, json=need.model_dump(mode="json")).json()
        assert lookup["offers"][0]["product_id"] == "464306"
        quote = http.post("/v1/quotes", headers=headers, json={"customer_id": "1", "deliver_by": str(need.deliver_by),
            "need_ref": lookup["lookup_id"], "lines": [{"product_id": "464306", "quantity_sales_units": 1000}]}).json()
        order = http.post("/v1/orders", headers={**headers, "Idempotency-Key": "surface-demo"}, json={"quote_id": quote["id"]}).json()
        assert (order["total"], order["status"]) == (1010, "CONFIRMED")


def test_AC_9_text_a2a_confirmation_missing_fields_and_no_model(gateway, need, upstream, respx_mock):
    from app.agent_surface_interpret import interpret
    monday = audit.utcnow().date() + timedelta(days=(7 - audit.utcnow().weekday()) % 7 or 7)
    def fake_model(request):
        body = json.loads(request.content)
        assert body["model"] == "gpt-6-luna" and request.headers["authorization"] == "Bearer test-key"
        inputs = json.loads(body["input"])
        context = inputs["context"]
        assert context["customer_ids"] == ["1"] and "standing_constraints" in context and context["units"]
        structured = {**need.model_dump(mode="json"), "deliver_by": monday.isoformat()}
        if "1000" not in inputs["text"]:
            structured.pop("quantity")
        return httpx.Response(200, json={"output_text": json.dumps({"kind": "need", "request": structured})})
    respx_mock.post("https://api.openai.com/v1/responses").mock(side_effect=fake_model)
    p = buyer(gateway)
    def message(key, *, text=None, task=None, command=None):
        return at.SendMessageRequest(message=at.Message(message_id=key, role=at.Role.ROLE_USER,
            task_id=task.id if task else "", context_id=task.context_id if task else "",
            parts=[at.Part(text=text)] if text else [data_part(command)]))
    with TestClient(create_app(gateway, serve_mcp=False), base_url="http://localhost") as http:
        def send(request):
            response = http.post("/a2a", headers={**agent_headers("external"), "A2A-Version": "1.0"},
                json={"jsonrpc": "2.0", "id": request.message.message_id, "method": "SendMessage", "params": MessageToDict(request)})
            assert response.status_code == 200, response.text
            return ParseDict(response.json()["result"]["task"], at.Task())
        task = send(message("text", text="Need 1000 litres of lactose-free milk by Monday"))
        assert task.status.state == at.TaskState.TASK_STATE_INPUT_REQUIRED
        result = MessageToDict(next(a for a in task.artifacts if a.name == "interpreted_request").parts[0])["data"]
        draft = result["draft"]["request"]
        assert draft["customer_id"] == "1" and draft["quantity"] == {"value": 1000, "unit": "L"} and draft["deliver_by"] == monday.isoformat()
        assert not upstream.price_calls and not upstream.availability_calls
        missing = send(message("missing", text="Need lactose-free milk by Monday"))
        assert "quantity" in MessageToDict(missing)["status"]["message"]["parts"][1]["data"]["missing_fields"]
        continued = send(message("confirm", task=task, command={"version": 1, "command": "confirm_interpretation", "confirm": True}))
        assert "offers" in {a.name for a in continued.artifacts} and upstream.price_calls
    gateway.decisions.key = ""
    fallback = interpret(gateway, p, "Need some milk")
    assert fallback["status"] == "INPUT_REQUIRED" and fallback["schema"]["properties"]["request"] and not fallback["model_configured"]
    # The MCP entry has the same draft-only boundary, and supports basket drafts.
    async def run():
        server = create_server(gateway, "external")
        result = await server.call_tool("interpret_request", {"text": "Need some milk"})
        assert result.structured_content["next_action"] == "FILL_STRUCTURED_REQUEST"
        confirmed = await server.call_tool("confirm_interpretation", {"draft_id": result.structured_content["draft_id"],
            "confirm": True, "request": need.model_dump(mode="json")})
        assert not confirmed.is_error and "offers" in confirmed.structured_content
    asyncio.run(run())


def test_AC_10_reference_verifies_signed_cards_rejects_tampering_and_result_issuer(gateway):
    module_spec = importlib.util.spec_from_file_location("surface_reference", Path(__file__).resolve().parents[2] / "clients/python/reference_buyer.py")
    reference = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(reference)
    from app.a2a import agent_card
    async def run():
        for card in (MessageToDict(await agent_card(gateway)), await mcp_card(gateway)):
            reference.verify_card(card, gateway.signing.jwks(), expected_origin=canonical_origin(gateway))
            tampered = copy.deepcopy(card)
            if "supportedInterfaces" in card:
                tampered["supportedInterfaces"][0]["url"] += "/copied"
            else:
                tampered["remotes"][0]["url"] += "/copied"
            with pytest.raises(Exception):
                reference.verify_card(tampered, gateway.signing.jwks(), expected_origin=canonical_origin(gateway))
        server = create_server(gateway, "external")
        for name, args in (("get_agent_context", {}), ("get_product", {"id": "missing"}), ("choose_offer", {"lookup_id": "missing"}), ("unknown_tool", {})):
            result = await server.call_tool(name, args)
            assert result.meta["issuer"] == canonical_origin(gateway)
    asyncio.run(run())


def test_AC_11_same_url_private_discovery_varies_by_audience_tenant_and_scope(gateway):
    with TestClient(create_app(gateway), base_url="http://localhost") as http:
        buyer_list = rpc(http, agent_headers("external"), "tools/list")
        operator_list = rpc(http, OPERATOR_HEADERS, "tools/list")
        assert buyer_list.status_code == operator_list.status_code == 200
        buyers = {t["name"] for t in buyer_list.json()["result"]["tools"]}
        operators = {t["name"] for t in operator_list.json()["result"]["tools"]}
        assert "find_offers" in buyers and "approve_order" not in buyers
        assert "approve_order" in operators and "find_offers" not in operators
        for response in (buyer_list, operator_list, rpc(http, agent_headers("external"), "resources/list"),
                         rpc(http, agent_headers("external"), "prompts/list"),
                         http.get("/mcp/server-card", headers=agent_headers("external")),
                         http.get("/.well-known/agent-card.json", headers=agent_headers("external"))):
            assert response.headers["cache-control"] == "private, no-store" and response.headers["vary"] == "Authorization"
        public = http.get("/.well-known/agent-card.json").json()
        discovery = next(e["params"]["discovery"] for e in public["capabilities"]["extensions"] if e["uri"] == EXTENSION)
        assert "tenant_id" not in discovery
