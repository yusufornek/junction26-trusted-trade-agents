import asyncio
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

from app import audit
from app.api import create_app
from conftest import OPERATOR_HEADERS, TEST_KEYS, agent_headers
from app.mcp_server import create_server
from app.models import QuoteLineRequest
from conftest import quote_request


@pytest.fixture
def client(gateway):
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        yield client


def place_order(gateway, need, *, qty=6, product_ids=("1",), agent_id="buyer"):
    lookup = gateway.find_offers(need, actor=f"agent:{agent_id}")
    request = quote_request(need, qty=qty, need_ref=lookup.lookup_id)
    request.lines = [QuoteLineRequest(product_id=pid, quantity_sales_units=qty) for pid in product_ids]
    quote = gateway.orders.create_quote(request, agent_id=agent_id)
    return gateway.orders.place_order(quote.id, str(uuid4()), agent_id=agent_id)


def call_counts(upstream):
    return len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls)


@pytest.mark.parametrize("reason", [None, "Agent selected the wrong product"])
@pytest.mark.parametrize("registered_agents", [("buyer", "external")], indirect=True)
def test_cancel_ac1_human_confirmed_order(client, gateway, need, upstream, reason):
    order = place_order(gateway, need)
    assert order.status == "CONFIRMED"
    before_calls = call_counts(upstream)
    body = {"cancelled_by": "Ada"}
    if reason is not None:
        body["reason"] = reason
    response = client.post(f"/v1/orders/{order.id}/cancel", json=body)
    assert response.status_code == 200, response.text
    cancelled = response.json()
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["cancelled_by"] == "Ada"
    assert cancelled["cancel_reason"] == (reason or "")
    assert cancelled["total"] == order.total
    assert cancelled["lines"] == order.model_dump(mode="json")["lines"]
    assert client.get(f"/v1/orders/{order.id}").json() == cancelled
    assert gateway.orders.get_quote(order.quote_id).status == "ACCEPTED"
    assert call_counts(upstream) == before_calls


@pytest.mark.parametrize("missing_ids", [{"1"}, {"1", "4"}])
@pytest.mark.parametrize("registered_agents", [("buyer", "external")], indirect=True)
def test_cancel_ac1_missing_cutoffs_do_not_block(client, gateway, need, upstream, respx_mock, missing_ids):
    def availability_response(request):
        body = upstream.availability_response(request).json()
        for product in body["products"]:
            if product["productId"] in missing_ids:
                product["closingTime"] = None
        return httpx.Response(200, json=body)

    respx_mock.post("https://valio.test/api/availability").mock(side_effect=availability_response)
    order = place_order(gateway, need, product_ids=("1", "4"))
    assert order.status == "PENDING_APPROVAL"
    assert "CUTOFF_UNKNOWN" in {hit.rule for hit in order.policy.hits}
    order = gateway.orders.approve(order.id, "Ada")
    assert client.post(f"/v1/orders/{order.id}/cancel", json={"cancelled_by": "Ada"}).status_code == 200


@pytest.mark.parametrize("registered_agents", [("buyer", "external")], indirect=True)
def test_cancel_ac2_stock_reservation_released(client, gateway, need, upstream):
    upstream.model, upstream.amount = "Stock", 10
    order = place_order(gateway, need, product_ids=("1", "4"))
    assert order.status == "CONFIRMED"
    before = gateway.find_offers(need)
    assert {o.availability.available_amount for o in before.offers if o.product_id in {"1", "4"}} == {4}
    assert client.post(f"/v1/orders/{order.id}/cancel", json={"cancelled_by": "Ada"}).status_code == 200
    after = gateway.find_offers(need)
    assert {o.availability.available_amount for o in after.offers if o.product_id in {"1", "4"}} == {10}
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM reservations WHERE order_id=?", (order.id,)).fetchone()[0] == 0


@pytest.mark.parametrize("seconds_after", [0, 1])
@pytest.mark.parametrize("registered_agents", [("buyer", "external")], indirect=True)
def test_cancel_ac3_cutoff_passed(client, gateway, need, upstream, respx_mock, monkeypatch, seconds_after):
    # Only the second line has reached its cutoff; checking just the first is insufficient.
    upstream.amount = 2000  # Finite stock is reserved; sentinel availability is not.
    first_cutoff = upstream.cutoff + timedelta(hours=1)

    def availability_response(request):
        body = upstream.availability_response(request).json()
        for product in body["products"]:
            if product["productId"] == "1":
                product["closingTime"] = first_cutoff.isoformat()
        return httpx.Response(200, json=body)

    respx_mock.post("https://valio.test/api/availability").mock(side_effect=availability_response)
    upstream.model = "Stock"
    order = place_order(gateway, need, product_ids=("1", "4"))
    assert order.status == "CONFIRMED"
    monkeypatch.setattr(audit, "utcnow", lambda: upstream.cutoff + timedelta(seconds=seconds_after))
    before_calls = call_counts(upstream)
    response = client.post(f"/v1/orders/{order.id}/cancel", json={"cancelled_by": "Ada"})
    assert response.status_code == 409
    assert response.json()["code"] == "CUTOFF_PASSED"
    assert gateway.orders.get_order(order.id) == order
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM reservations WHERE order_id=?", (order.id,)).fetchone()[0] == 2
    assert call_counts(upstream) == before_calls


@pytest.mark.parametrize("status", ["PENDING_APPROVAL", "REJECTED", "EXPIRED", "CANCELLED"])
@pytest.mark.parametrize("registered_agents", [("buyer", "external")], indirect=True)
def test_cancel_ac4_invalid_transition(client, gateway, need, upstream, monkeypatch, status):
    order = place_order(gateway, need, qty=200 if status != "CANCELLED" else 6)
    if status == "REJECTED":
        gateway.orders.reject(order.id, "Ada")
    elif status == "EXPIRED":
        monkeypatch.setattr(audit, "utcnow", lambda: order.expires_at + timedelta(seconds=1))
    elif status == "CANCELLED":
        gateway.orders.cancel(order.id, actor="human:Ada", reason="Initial cancellation")
    before = gateway.orders.get_order(order.id)
    assert before.status == status
    response = client.post(f"/v1/orders/{order.id}/cancel", json={"cancelled_by": "Ada"})
    assert response.status_code == 409
    assert response.json()["code"] == "INVALID_TRANSITION"
    assert gateway.orders.get_order(order.id) == before
    assert gateway.audit(order_id=order.id)[-1].data["error"] == "INVALID_TRANSITION"


def test_cancel_ac5_unknown_order(client, gateway):
    response = client.post("/v1/orders/missing/cancel", json={"cancelled_by": "Ada"})
    assert response.status_code == 404
    assert response.json()["code"] == "ORDER_NOT_FOUND"
    event = gateway.audit(order_id="missing")[-1]
    assert event.action == "CANCEL_FAILED"
    assert event.data["error"] == "ORDER_NOT_FOUND"


@pytest.mark.parametrize("body", [{"cancelled_by": ""}, {"cancelled_by": " \t\n "}, {}, {"cancelled_by": None}])
@pytest.mark.parametrize("registered_agents", [("buyer", "external")], indirect=True)
def test_cancel_ac6_blank_cancelled_by(client, gateway, need, upstream, body):
    order = place_order(gateway, need)
    before = gateway.audit(order_id=order.id)
    response = client.post(f"/v1/orders/{order.id}/cancel", json=body)
    assert response.status_code == 422
    assert response.json()["code"] == ("ARGUMENT_REQUIRED" if "cancelled_by" not in body else "VALIDATION_ERROR")
    assert gateway.orders.get_order(order.id) == order
    if body.get("cancelled_by") != " \t\n ":
        # Spec 63 rejects invalid wire types/lengths before the command boundary.
        assert gateway.audit(order_id=order.id) == before
        return
    event = gateway.audit(order_id=order.id)[-1]
    assert event.action == "CANCEL_FAILED"
    assert event.quote_id == order.quote_id
    assert event.data["error"] == "VALIDATION_ERROR"
    # Credentials establish the actor; invalid typed names stay in normalised audit data.
    name = body.get("cancelled_by") if isinstance(body, dict) else None
    expected = name.strip() if isinstance(name, str) and name.strip() else "unknown"
    assert event.actor == "human:operator"
    assert event.data["stated_name"] == expected
    assert event.data["inputs"]["order_id"] == order.id


@pytest.mark.parametrize("registered_agents", [("buyer", "external")], indirect=True)
def test_cancel_ac7_success_and_failure_audit(client, gateway, need, upstream, monkeypatch):
    order = place_order(gateway, need)
    body = {"cancelled_by": "Ada", "reason": "Wrong product"}
    assert client.post(f"/v1/orders/{order.id}/cancel", json=body).status_code == 200
    assert gateway.orders.get_order(order.id).cancelled_by == "Ada"
    assert client.post(f"/v1/orders/{order.id}/cancel", json=body).status_code == 409
    events = client.get("/v1/audit", params={"order_id": order.id}).json()
    success = [e for e in events if e["action"] == "CANCEL"]
    failed = [e for e in events if e["action"] == "CANCEL_FAILED"]
    assert len(success) == len(failed) == 1
    assert success[0]["data"]["reason"] == "Wrong product"
    assert failed[0]["data"]["error"] == "INVALID_TRANSITION"
    assert failed[0]["data"]["inputs"]["reason"] == "Wrong product"
    for event in success + failed:
        assert event["actor"] == "human:operator"
        assert event["data"]["stated_name"] == "Ada"
        assert event["order_id"] == order.id
        assert event["quote_id"] == order.quote_id
        assert event["data"]["live_calls"] == {"price": 0, "availability": 0}

    other = place_order(gateway, need)
    monkeypatch.setattr(audit, "utcnow", lambda: upstream.cutoff)
    assert client.post(f"/v1/orders/{other.id}/cancel", json=body).status_code == 409
    event = client.get("/v1/audit", params={"order_id": other.id}).json()[-1]
    assert event["action"] == "CANCEL_FAILED"
    assert event["data"]["error"] == "CUTOFF_PASSED"
    assert event["quote_id"] == other.quote_id


@pytest.mark.parametrize("registered_agents", [("buyer", "external")], indirect=True)
def test_cancel_ac8_mcp_owner_and_other_agent(gateway, need, upstream):
    owner = create_server(gateway, agent_id="buyer")
    other = create_server(gateway, agent_id="other")
    lookup = gateway.find_offers(need, actor="agent:buyer")
    quote = gateway.orders.create_quote(quote_request(need, qty=6, need_ref=lookup.lookup_id), "buyer")

    async def run():
        tools = await owner.list_tools()
        cancel_tool = next(t for t in tools if t.name == "cancel_order")
        assert set(cancel_tool.input_schema["properties"]) == {"order_id", "reason", "idempotency_key"}
        placed = await owner.call_tool("place_order", {"quote_id": quote.id, "idempotency_key": "mcp-cancel"})
        assert not placed.is_error
        order_id = placed.structured_content["id"]
        before = gateway.orders.get_order(order_id)
        assert before.agent_id == "buyer" and before.status == "CONFIRMED"
        before_calls = call_counts(upstream)
        tool_failure = await other.call_tool("cancel_order", {"order_id": order_id, "reason": "Not mine"})
        assert tool_failure.is_error
        assert tool_failure.structured_content["code"] == "ORDER_NOT_FOUND"
        assert gateway.orders.get_order(order_id) == before
        failed = gateway.audit(order_id=order_id)[-1]
        assert failed.action == "CANCEL_FAILED" and failed.actor == "agent:other"
        assert failed.data["error"] == "ORDER_NOT_FOUND" and failed.quote_id == quote.id
        cancelled = await owner.call_tool("cancel_order", {"order_id": order_id, "reason": "Buyer correction"})
        assert not cancelled.is_error
        assert cancelled.structured_content["status"] == "CANCELLED"
        assert cancelled.structured_content["cancelled_by"] == "buyer"
        assert cancelled.structured_content["cancel_reason"] == "Buyer correction"
        assert cancelled.structured_content["total"] == before.total
        success = [e for e in gateway.audit(order_id=order_id) if e.action == "CANCEL"]
        assert len(success) == 1
        assert success[0].actor == "agent:buyer" and success[0].quote_id == quote.id
        assert success[0].data["reason"] == "Buyer correction"
        assert call_counts(upstream) == before_calls

    asyncio.run(run())


@pytest.mark.parametrize("registered_agents", [("buyer", "external")], indirect=True)
def test_cancel_ac8_mcp_transport_exposes_owner_error(gateway, need, upstream):
    order = place_order(gateway, need)
    server = create_server(gateway, agent_id="other")
    app = server.streamable_http_app(stateless_http=True, json_response=True)
    with TestClient(app, base_url="http://localhost:8001", headers=agent_headers("other")) as client:
        response = client.post("/mcp", headers={"Accept": "application/json, text/event-stream"},
                               json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                                   "name": "cancel_order", "arguments": {"order_id": order.id, "reason": "Not mine"}}})
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["isError"]
        assert "ORDER_NOT_FOUND" in result["content"][0]["text"]
    assert gateway.orders.get_order(order.id) == order


@pytest.mark.parametrize("registered_agents", [("buyer", "external")], indirect=True)
def test_cancel_ac9_stats_exclude_cancelled_spend(client, gateway, need, upstream):
    order = place_order(gateway, need)
    retained = place_order(gateway, need)
    before = client.get("/v1/stats").json()
    assert before["orders_by_status"]["CONFIRMED"] == 2
    assert before["confirmed_spend_total_eur"] == order.total + retained.total
    assert client.post(f"/v1/orders/{order.id}/cancel", json={"cancelled_by": "Ada"}).status_code == 200
    stats = client.get("/v1/stats").json()
    assert stats["orders_by_status"]["CANCELLED"] == 1
    assert stats["orders_by_status"]["CONFIRMED"] == 1
    assert stats["confirmed_spend_total_eur"] == retained.total
    assert stats["placement_outcomes"] == before["placement_outcomes"]


def test_cancel_ac10_generated_openapi_contract():
    contract = Path(__file__).resolve().parents[2] / "contracts/openapi.yaml"
    schema = yaml.safe_load(contract.read_text())
    assert schema == create_app(keys=TEST_KEYS).openapi()
    operation = schema["paths"]["/v1/orders/{order_id}/cancel"]["post"]
    assert operation["requestBody"]["required"]
    assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"] == "#/components/schemas/CancelRequest"
    assert {item["$ref"] for item in operation["responses"]["200"]["content"]["application/json"]["schema"]["anyOf"]} == {
        "#/components/schemas/Order", "#/components/schemas/DecisionPreview"}
    models = schema["components"]["schemas"]
    assert models["CancelRequest"]["required"] == ["cancelled_by"]
    assert models["CancelRequest"]["properties"]["reason"]["default"] == ""
    assert {"cancelled_by", "cancel_reason"} <= models["Order"]["properties"].keys()
    assert not {"cancelled_by", "cancel_reason"} & set(models["Order"]["required"])
