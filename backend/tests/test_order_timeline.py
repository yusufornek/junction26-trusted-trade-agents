from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app import audit
from app.api import create_app
from conftest import quote_request


@pytest.fixture
def client(gateway):
    with TestClient(create_app(gateway)) as client:
        yield client


def place(gateway, need, *, qty=200, with_lookup=True, key="timeline"):
    lookup = gateway.find_offers(need, actor="agent:buyer") if with_lookup else None
    quote = gateway.orders.create_quote(
        quote_request(need, qty=qty, need_ref=lookup.lookup_id if lookup else None), "buyer")
    order = gateway.orders.place_order(quote.id, key, "buyer")
    return lookup, quote, order


def calls(upstream):
    return len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls)


@pytest.mark.parametrize("decision,status", [("approve", "CONFIRMED"), ("reject", "REJECTED")])
@pytest.mark.parametrize("registered_agents", [("buyer",)], indirect=True)
def test_timeline_ac1_full_story_with_actors(client, gateway, need, upstream, decision, status):
    lookup, quote, order = place(gateway, need)
    response = client.post(f"/v1/approvals/{order.id}/{decision}", json={"approver": "Ada"})
    assert response.status_code == 200
    before = calls(upstream)
    response = client.get(f"/v1/orders/{order.id}/timeline")
    assert response.status_code == 200, response.text
    timeline = response.json()
    assert timeline["order_id"] == order.id and timeline["quote_id"] == quote.id
    assert timeline["lookup_id"] == lookup.lookup_id
    assert timeline["agent_id"] == "buyer" and timeline["status"] == status
    steps = timeline["steps"]
    assert [s["step"] for s in steps] == ["lookup", "quote", "order", "decision"]
    assert [s["action"] for s in steps] == ["LOOKUP", "CREATE_QUOTE", "PLACE_ORDER", decision.upper()]
    assert [s["actor"] for s in steps] == ["agent:buyer"] * 3 + ["human:Ada"]
    assert [s["ts"] for s in steps] == sorted(s["ts"] for s in steps)
    events = gateway.audit()
    for step, event in zip(steps, events, strict=True):
        assert step["summary"] == event.summary
        assert step["data"] == event.data
        assert step["live_calls"] == event.data.get("live_calls", {"price": 0, "availability": 0})
        assert step["decision_calls"] == event.data.get("decision_calls", 0)
    assert steps[0]["data"]["inputs"] == need.model_dump(mode="json")
    assert calls(upstream) == before


@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("registered_agents", [("buyer",)], indirect=True)
def test_timeline_ac2_quote_without_lookup(client, gateway, need, upstream, monkeypatch, expired):
    _, quote, order = place(gateway, need, with_lookup=False)
    if expired:
        monkeypatch.setattr(audit, "utcnow", lambda: order.expires_at + timedelta(seconds=1))
    before = calls(upstream)
    timeline = client.get(f"/v1/orders/{order.id}/timeline").json()
    assert timeline["lookup_id"] is None
    assert timeline["steps"][0]["step"] == "quote"
    assert [s["action"] for s in timeline["steps"]] == ["CREATE_QUOTE", "PLACE_ORDER"] + (
        ["ORDER_EXPIRED"] if expired else [])
    assert timeline["status"] == ("EXPIRED" if expired else "PENDING_APPROVAL")
    if expired:
        step = timeline["steps"][-1]
        assert step["step"] == "system" and step["actor"] == "system"
        assert step["live_calls"] == {"price": 0, "availability": 0}
        assert step["decision_calls"] == 0
        assert client.get(f"/v1/orders/{order.id}/timeline").json() == timeline
    assert timeline["quote_id"] == quote.id
    assert calls(upstream) == before


@pytest.mark.parametrize("registered_agents", [("buyer",)], indirect=True)
def test_timeline_ac3_replay_failures_and_cancellation(client, gateway, need, upstream):
    _, quote, order = place(gateway, need)
    # A separate order, even by the same agent, must never enter this timeline.
    unrelated = place(gateway, need, qty=1, key="unrelated")[2]
    assert gateway.orders.place_order(quote.id, "timeline", "buyer").id == order.id
    assert client.post(f"/v1/orders/{order.id}/cancel", json={"cancelled_by": "Ada"}).status_code == 409
    # This failed placement carries only the quote ID, not the order ID.
    assert client.post("/v1/orders", json={"quote_id": quote.id}, headers={
        "Idempotency-Key": "another-key", "X-Agent-Id": "buyer"}).status_code == 409
    assert client.post(f"/v1/approvals/{order.id}/approve", json={"approver": "Ada"}).status_code == 200
    assert client.post(f"/v1/approvals/{order.id}/reject", json={"approver": "Ada"}).status_code == 409
    assert client.post(f"/v1/orders/{order.id}/cancel", json={"cancelled_by": "Ada", "reason": "Wrong product"}).status_code == 200
    before = calls(upstream)
    timeline = client.get(f"/v1/orders/{order.id}/timeline").json()
    steps = timeline["steps"]
    assert [s["action"] for s in steps] == [
        "LOOKUP", "CREATE_QUOTE", "PLACE_ORDER", "ORDER_REPLAY", "CANCEL_FAILED",
        "PLACE_ORDER_FAILED", "APPROVE", "REJECT_FAILED", "CANCEL",
    ]
    assert steps[3]["step"] == "order"
    assert steps[3]["live_calls"] == {"price": 0, "availability": 0}
    assert steps[4]["step"] == "cancellation" and steps[4]["data"]["error"] == "INVALID_TRANSITION"
    assert steps[5]["step"] == "order" and steps[5]["data"]["error"] == "QUOTE_ALREADY_ACCEPTED"
    assert steps[7]["step"] == "decision" and steps[7]["data"]["error"] == "INVALID_TRANSITION"
    assert steps[-1]["step"] == "cancellation"
    assert steps[-1]["data"]["reason"] == "Wrong product"
    assert timeline["status"] == "CANCELLED"
    assert unrelated.id != timeline["order_id"]
    assert calls(upstream) == before


@pytest.mark.parametrize("registered_agents", [("buyer",)], indirect=True)
def test_timeline_ac4_totals_sum_actual_step_calls(client, gateway, need, upstream, respx_mock, monkeypatch):
    # Both lookups share the process cache. Only the referenced one belongs to the order.
    gateway.find_offers(need)
    lookup, quote, order = place(gateway, need)
    assert lookup.live_calls.price == lookup.live_calls.availability == 0
    respx_mock.post("https://valio.test/api/availability").respond(503)
    assert client.post(f"/v1/approvals/{order.id}/approve", json={"approver": "Ada"}).status_code == 502
    respx_mock.post("https://valio.test/api/availability").mock(side_effect=upstream.availability_response)
    assert client.post(f"/v1/approvals/{order.id}/approve", json={"approver": "Ada"}).status_code == 200
    assert gateway.orders.place_order(quote.id, "timeline", "buyer").id == order.id
    # Timestamp ties must retain audit insertion order and never duplicate a row matching both IDs.
    now = audit.utcnow()
    monkeypatch.setattr(audit, "utcnow", lambda: now)
    assert client.post(f"/v1/orders/{order.id}/cancel", json={"cancelled_by": "Ada"}).status_code == 200
    assert client.post(f"/v1/orders/{order.id}/cancel", json={"cancelled_by": "Ada"}).status_code == 409
    before = calls(upstream)
    timeline = client.get(f"/v1/orders/{order.id}/timeline").json()
    assert timeline["total_live_calls"] == {"price": 1, "availability": 4}
    assert timeline["total_decision_calls"] == 1
    for endpoint in ("price", "availability"):
        assert timeline["total_live_calls"][endpoint] == sum(s["live_calls"][endpoint] for s in timeline["steps"])
    assert timeline["total_decision_calls"] == sum(s["decision_calls"] for s in timeline["steps"])
    assert [s["action"] for s in timeline["steps"]][-2:] == ["CANCEL", "CANCEL_FAILED"]
    assert len(timeline["steps"]) == 8
    assert calls(upstream) == before


def test_timeline_ac5_unknown_order_and_generated_contract(client, upstream):
    before = calls(upstream)
    response = client.get("/v1/orders/missing/timeline")
    assert response.status_code == 404
    assert response.json() == {"code": "ORDER_NOT_FOUND", "message": "Order was not found"}
    assert calls(upstream) == before
    contract = Path(__file__).resolve().parents[2] / "contracts/openapi.yaml"
    schema = yaml.safe_load(contract.read_text())
    assert schema == create_app().openapi()
    operation = schema["paths"]["/v1/orders/{order_id}/timeline"]["get"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"] == "#/components/schemas/OrderTimeline"
    models = schema["components"]["schemas"]
    for name in ("OrderTimeline", "TimelineStep"):
        assert all(field.get("description") for field in models[name]["properties"].values())
    assert models["TimelineStep"]["properties"]["step"]["enum"] == [
        "lookup", "quote", "order", "decision", "cancellation", "system"]
