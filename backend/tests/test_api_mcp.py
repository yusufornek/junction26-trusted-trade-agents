import asyncio
import json
import select
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from conftest import OPERATOR_HEADERS, TEST_KEYS, agent_headers
from app.mcp_server import create_server


@pytest.mark.parametrize("registered_agents", [("hotel-agent",)], indirect=True)
def test_full_rest_flow_1000_l(gateway, need, upstream):
    with TestClient(create_app(gateway), headers=agent_headers("hotel-agent")) as client:
        result = client.post("/v1/offers", json=need.model_dump(mode="json"))
        assert result.status_code == 200, result.text
        lookup = result.json()
        offer = next(o for o in lookup["offers"] if o["product_id"] == "1")
        assert offer["units_needed"] == 200
        quote_result = client.post("/v1/quotes", json={"customer_id": "1", "deliver_by": str(need.deliver_by),
            "need_ref": lookup["lookup_id"], "lines": [{"product_id": "1", "quantity_sales_units": offer["units_needed"]}]})
        assert quote_result.status_code == 200, quote_result.text
        quote = quote_result.json()
        headers = {"Idempotency-Key": "rest-demo", **agent_headers("hotel-agent")}
        order_response = client.post("/v1/orders", json={"quote_id": quote["id"]}, headers=headers)
        assert order_response.status_code == 200, order_response.text
        order = order_response.json()
        assert order["status"] == "PENDING_APPROVAL"
        assert client.post("/v1/orders", json={"quote_id": quote["id"]}, headers=headers).json()["id"] == order["id"]
        client.headers.update(OPERATOR_HEADERS)
        inbox = client.get("/v1/approvals").json()
        assert inbox[0]["quote"]["id"] == quote["id"]
        assert inbox[0]["order"]["policy"]["hits"][0]["rule"] == "max_auto_total_eur"
        approved = client.post(f"/v1/approvals/{order['id']}/approve", json={"approver": "Ada", "comment": "Approved 1000 L"})
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "CONFIRMED"
        assert client.get(f"/v1/orders/{order['id']}").json()["status"] == "CONFIRMED"
        assert client.get("/v1/orders").json()[0]["total"] == 2600
        assert client.get("/v1/approvals").json() == []
        assert client.get("/v1/products/1").json()["claims"] == ["FREE_FROM_LACTOSE"]
        assert client.get("/health").json() == {"status": "ok", "catalog_rows": 7,
            "coverage": gateway.catalog.metadata("coverage"),
            "upstream": {"blocked_until": None, "calls_last_minute": 6}}
        entries = client.get("/v1/audit", params={"quote_id": quote["id"]}).json()
        assert [a["action"] for a in entries] == ["CREATE_QUOTE", "PLACE_ORDER", "ORDER_REPLAY", "APPROVE"]
        assert entries[1]["data"]["live_calls"] == {"price": 0, "availability": 1}
        assert client.get("/v1/orders/missing").json()["code"] == "ORDER_NOT_FOUND"
        assert client.post("/v1/orders", json={"quote_id": quote["id"]}).status_code == 422
        assert client.post("/v1/orders", json={"quote_id": quote["id"]}).json()["code"] == "VALIDATION_ERROR"
        assert client.get("/health", headers={"Origin": "http://localhost:5173"}).headers["access-control-allow-origin"] == "http://localhost:5173"


@pytest.mark.parametrize("registered_agents", [("mcp-agent",)], indirect=True)
def test_mcp_tools_use_shared_core(gateway, need, upstream):
    server = create_server(gateway)

    async def run():
        tools = await server.list_tools()
        assert {t.name for t in tools} == {"search_products", "find_offers", "create_quote", "place_order", "get_order_status", "get_order_receipt", "cancel_order", "get_order_ubl", "get_order_evidence", "find_basket_offers", "list_proposals"}
        assert all(t.input_schema and t.output_schema for t in tools)
        result = await server.call_tool("search_products", {"query": "milk", "required_claims": ["FREE_FROM_LACTOSE"], "exclude_allergens": ["AE"]})
        assert not result.is_error
        assert not upstream.price_calls and not upstream.availability_calls
        offers = await server.call_tool("find_offers", {"need": need.model_dump(mode="json")})
        data = offers.structured_content
        assert data["need"]["quantity"]["value"] == 1000
        quote_result = await server.call_tool("create_quote", {"customer_id": "1", "deliver_by": str(need.deliver_by),
            "need_ref": data["lookup_id"], "lines": [{"product_id": "1", "quantity_sales_units": 200}]})
        quote = quote_result.structured_content
        order_result = await server.call_tool("place_order", {"quote_id": quote["id"], "idempotency_key": "mcp-demo"})
        order = order_result.structured_content
        assert order["status"] == "PENDING_APPROVAL"
        gateway.orders.approve(order["id"], "Ada")
        status = await server.call_tool("get_order_status", {"order_id": order["id"]})
        assert status.structured_content["status"] == "CONFIRMED"

    asyncio.run(run())


def test_streamable_http_transport(gateway):
    app = create_server(gateway).streamable_http_app(stateless_http=True, json_response=True)
    headers = {"Accept": "application/json, text/event-stream"}
    with TestClient(app, base_url="http://localhost:8001", headers=agent_headers("mcp-agent")) as client:
        initialized = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}}})
        assert initialized.status_code == 200
        assert initialized.json()["result"]["serverInfo"]["name"] == "Valio Aimo Trusted Trade Agents"
        listed = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        assert listed.status_code == 200
        assert len(listed.json()["result"]["tools"]) == 11


def test_stdio_transport(gateway):
    # A real subprocess and JSON-RPC pipes, using a fixture DB and no credential loader.
    runner = """
import sys
from pathlib import Path
from app.config import Settings
from app.service import Gateway
from app.mcp_server import create_server
gateway = Gateway(Settings(db_path=Path(sys.argv[1])))
try:
    create_server(gateway).run(transport='stdio')
finally:
    gateway.close()
"""
    process = subprocess.Popen([sys.executable, "-c", runner, str(gateway.settings.db_path)],
                               cwd=Path(__file__).resolve().parents[1],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    def rpc(message):
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
        # Import and schema startup can exceed five seconds on a loaded runner.
        ready, _, _ = select.select([process.stdout], [], [], 15)
        assert ready, "MCP stdio did not respond"
        return json.loads(process.stdout.readline())

    try:
        result = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}}})
        assert result["result"]["serverInfo"]["name"] == "Valio Aimo Trusted Trade Agents"
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        process.stdin.flush()
        tools = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        assert len(tools["result"]["tools"]) == 11
        search = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "search_products", "arguments": {"query": "milk", "limit": 2}}})
        assert not search["result"].get("isError", False)
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdout.close()
    assert process.returncode == 0


def test_contract_all_routes_have_response_schema():
    schema = create_app(keys=TEST_KEYS).openapi()
    assert set(schema["paths"]) == {"/health", "/v1/offers", "/v1/products/{id}", "/v1/quotes", "/v1/quotes/{id}",
                                    "/v1/orders", "/v1/orders/{id}", "/v1/orders/{id}/receipt", "/v1/approvals", "/v1/approvals/{order_id}/approve",
                                    "/v1/approvals/{order_id}/reject", "/v1/audit", "/v1/lookups/{lookup_id}",
                                    "/v1/policy", "/v1/policy/reset", "/v1/stats", "/v1/orders/{order_id}/cancel",
                                    "/v1/orders/{order_id}/timeline", "/.well-known/jwks.json", "/v1/audit/head",
                                    "/v1/audit/verify", "/v1/orders/{id}/ubl", "/v1/audit/anchors", "/v1/mandates/revocations", "/v1/meta", "/v1/me",
                                    "/v1/agents/{agent_id}/track-record", "/v1/agents/{agent_id}/autonomy",
                                    "/.well-known/jwks-history.json", "/v1/orders/{order_id}/evidence",
                                    "/v1/buyer/agents", "/v1/buyer/mandates", "/v1/buyer/spend", "/v1/buyer/orders",
                                    "/v1/buyer/approvals", "/v1/buyer/activity", "/v1/buyer/agents/{agent_id}/suspend",
                                    "/v1/buyer/agents/{agent_id}/resume", "/v1/buyer/mandates/{jti}/revoke",
                                    "/v1/buyer/approvals/{order_id}/approve", "/v1/buyer/approvals/{order_id}/reject",
                                    "/v1/basket-lookups", "/v1/basket-lookups/{lookup_id}",
                                    "/v1/recurring-orders", "/v1/recurring-orders/{id}/runs",
                                    "/v1/recurring-orders/{id}/baseline", "/v1/recurring-orders/{id}/run-now",
                                    "/v1/buyer/proposal-preferences", "/v1/proposals", "/v1/proposals/inbox",
                                    "/v1/proposals/campaigns/{id}", "/v1/proposals/{id}/accept",
                                    "/v1/proposals/{id}/decline", "/v1/proposals/{id}/mute"}
    for path in schema["paths"].values():
        for operation in path.values():
            content = operation["responses"]["200"]["content"]
            assert content.get("application/json", content.get("application/xml"))["schema"]
    # Route inventory includes spec 10, 11 and 13 trust and UBL endpoints.
    models = schema["components"]["schemas"]
    assert {"plans", "no_match_reason"} <= models["LookupResult"]["properties"].keys()
    assert "preferences" in models["NeedSpec"]["properties"]
    assert {"lines", "plan_ref"} <= models["QuoteRequest"]["properties"].keys()
    assert "authoritative" in models["FulfilmentPlan"]["properties"]["policy_preview"]["description"]
