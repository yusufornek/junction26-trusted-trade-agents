"""Spec 30: an outside agent waits, resumes and collects a decided receipt."""
import asyncio
from datetime import timedelta
import json

from fastapi.testclient import TestClient
import pytest

from app import audit
from app.api import create_app
from app.auth import Credentials
from app.mcp_server import create_server
from app.sandbox import scenario_gateway
from app.signing import receipt_payload, verify_jws
from demo.run_exception_journey import ExceptionJourney, reference_need

OPERATOR = {"Authorization": "Bearer journey-operator"}
AGENT = {"Authorization": "Bearer journey-agent"}


def call(server, name, arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    assert not result.is_error, result.structured_content
    return result.structured_content


@pytest.fixture
def sandbox():
    with scenario_gateway("split_stock") as gateway:
        keys = Credentials("journey-operator", "sandbox-agent:journey-agent,outsider:journey-outsider", environment="sandbox")
        with TestClient(create_app(gateway, keys=keys), headers=AGENT) as client:
            yield gateway, create_server(gateway, "sandbox-agent"), client


def place(sandbox):
    gateway, server, client = sandbox
    need = reference_need()
    lookup = call(server, "find_offers", {"need": need})
    index = next(i for i, plan in enumerate(lookup["plans"]) if plan["kind"] == "split")
    quote = call(server, "create_quote", {"customer_id": need["customer_id"], "deliver_by": need["deliver_by"],
        "plan_ref": {"lookup_id": lookup["lookup_id"], "plan_index": index}, "idempotency_key": "journey-quote"})
    order = call(server, "place_order", {"quote_id": quote["id"], "idempotency_key": "journey-placement"})
    return order, lookup


def review(client, order, action="approve", comment="Budget and evidence reviewed"):
    return client.post(f"/v1/approvals/{order['id']}/{action}", headers=OPERATOR,
        json={"approver": "Ada", "comment": comment, "evidence_revision": order["evidence_revision"]})


def test_ac_1_split_plan_waits_with_reason_interval_and_shortage(sandbox):
    gateway, server, client = sandbox
    order, lookup = place(sandbox)
    for status in (order, client.get(f"/v1/orders/{order['id']}").json(),
                   call(server, "get_order_status", {"order_id": order["id"]})):
        assert status["status"] == "PENDING_APPROVAL"
        assert status["next_action"] == "WAIT_FOR_APPROVAL" and status["poll_after_seconds"] > 0
        assert status["order_revision"] == 1 and status["expires_at"]
        assert "1708.00" in status["approval_reason"] and "1500.00" in status["approval_reason"]
        assert status["approval_reason"].endswith(".")
    shortage = next(p["shortage"] for p in lookup["plans"] if p["kind"] == "split")
    assert shortage["requested"] == {"value": 400, "unit": "L"}
    assert shortage["best_single_coverage"] == {"value": 200, "unit": "L"}
    assert "No single" in shortage["reason"] and "200 L" in shortage["reason"]
    gateway.valio.rows["milk-b"]["stock"] = 0
    # Existing order reservations leave no usable milk; release them for this separate lookup.
    with gateway.db.connect(write=True) as conn:
        conn.execute("DELETE FROM reservations WHERE order_id=?", (order["id"],))
    partial = call(server, "find_offers", {"need": reference_need()})
    plan = next(p for p in partial["plans"] if p["kind"] == "partial")
    assert plan["shortage"]["requested"]["value"] == 400
    assert plan["shortage"]["best_single_coverage"]["value"] == 200
    assert plan["uncovered"]["value"] == 200


def test_ac_2_approval_increases_revision_and_receipt_verifies(sandbox):
    gateway, server, client = sandbox
    order, _ = place(sandbox)
    approved = review(client, order)
    assert approved.status_code == 200
    for status in (approved.json(), client.get(f"/v1/orders/{order['id']}").json(),
                   call(server, "get_order_status", {"order_id": order["id"]})):
        assert status["status"] == "CONFIRMED" and status["next_action"] is None
        assert status["poll_after_seconds"] is None
        assert status["order_revision"] == order["order_revision"] + 1
        assert status["evidence_revision"] == order["evidence_revision"]
    receipt = client.get(f"/v1/orders/{order['id']}/receipt")
    assert receipt.status_code == 200
    assert verify_jws(receipt.json()["receipt"], receipt_payload(receipt.json()), gateway.signing.jwks())
    mcp = call(server, "get_order_receipt", {"order_id": order["id"]})
    assert mcp == receipt.json()
    assert mcp["links"]["receipt_id"] == order["id"]
    # Cancellation is another distinct terminal outcome, with a new signed decision.
    cancelled = call(server, "cancel_order", {"order_id": order["id"], "reason": "Event cancelled"})
    assert cancelled["status"] == "CANCELLED" and cancelled["next_action"] == "STOP"
    assert cancelled["order_revision"] == 3 and cancelled["cancel_reason"] == "Event cancelled"
    assert verify_jws(cancelled["receipt"], receipt_payload(cancelled), gateway.signing.jwks())


def test_ac_3_rejection_keeps_comment_and_stops(sandbox):
    gateway, server, client = sandbox
    order, _ = place(sandbox)
    response = review(client, order, "reject", "The event has been cancelled")
    assert response.status_code == 200
    status = call(server, "get_order_status", {"order_id": order["id"]})
    assert status["status"] == "REJECTED" and status["next_action"] == "STOP"
    assert status["comment"] == "The event has been cancelled" and status["approver"] == "Ada"
    assert status["order_revision"] == 2 and status["poll_after_seconds"] is None
    receipt = call(server, "get_order_receipt", {"order_id": order["id"]})
    assert verify_jws(receipt["receipt"], receipt_payload(receipt), gateway.signing.jwks())


def test_ac_4_expiry_while_waiting_requires_lookup(sandbox, monkeypatch):
    gateway, server, client = sandbox
    order, _ = place(sandbox)
    deadline = gateway.orders.get_order(order["id"]).expires_at
    monkeypatch.setattr(audit, "utcnow", lambda: deadline + timedelta(seconds=1))
    status = client.get(f"/v1/orders/{order['id']}").json()
    assert status["status"] == "EXPIRED" and status["next_action"] == "LOOKUP_REQUIRED"
    assert status["order_revision"] == 2 and status["poll_after_seconds"] is None
    assert call(server, "get_order_status", {"order_id": order["id"]}) == status
    receipt = call(server, "get_order_receipt", {"order_id": order["id"]})
    assert verify_jws(receipt["receipt"], receipt_payload(receipt), gateway.signing.jwks())
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM reservations WHERE order_id=?", (order["id"],)).fetchone()[0] == 0


def test_ac_5_receipt_before_decision_returns_not_decided(sandbox):
    gateway, server, client = sandbox
    order, _ = place(sandbox)
    response = client.get(f"/v1/orders/{order['id']}/receipt")
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "NOT_DECIDED" and body["next_action"] == "WAIT_FOR_APPROVAL"
    assert body["data"]["poll_after_seconds"] == order["poll_after_seconds"]
    mcp = asyncio.run(server.call_tool("get_order_receipt", {"order_id": order["id"]}))
    assert mcp.is_error and mcp.structured_content["code"] == "NOT_DECIDED"
    assert mcp.structured_content["next_action"] == body["next_action"]
    assert client.get(f"/v1/orders/{order['id']}/receipt", headers={"Authorization": "Bearer journey-outsider"}).status_code == 404
    other = asyncio.run(create_server(gateway, "outsider").call_tool("get_order_receipt", {"order_id": order["id"]}))
    assert other.is_error and other.structured_content["code"] == "ORDER_NOT_FOUND"


def test_ac_6_changed_evidence_increases_revision_and_stays_pending(sandbox):
    gateway, server, client = sandbox
    order, _ = place(sandbox)
    gateway.valio.rows["milk-a"]["stock"] = 39
    changed = review(client, order)
    assert changed.status_code == 409 and changed.json()["code"] == "EVIDENCE_CHANGED"
    status = call(server, "get_order_status", {"order_id": order["id"]})
    assert status["status"] == "PENDING_APPROVAL" and status["next_action"] == "WAIT_FOR_APPROVAL"
    assert status["order_revision"] == 2 and status["evidence_revision"] == 2
    assert status["poll_after_seconds"] > 0 and status["approval_reason"]
    assert client.get(f"/v1/orders/{order['id']}").json() == status
    assert client.get(f"/v1/orders/{order['id']}/receipt").json()["code"] == "NOT_DECIDED"
    assert review(client, order).status_code == 409  # stale revision does not increment it
    assert gateway.orders.get_order(order["id"]).order_revision == 2
    approved = review(client, status)
    assert approved.status_code == 200 and approved.json()["order_revision"] == 3


def test_ac_7_reference_run_prints_receipt_and_recovers_lost_placement(sandbox, capsys, tmp_path):
    gateway, server, client = sandbox
    state, placements, sleeps = {}, [], []
    saved = tmp_path / "journey-state.json"

    async def uncertain(name, arguments):
        result = await server.call_tool(name, arguments)
        if name == "place_order":
            placements.append(dict(arguments))
            if len(placements) == 1:
                raise TimeoutError("The order was committed but its response was lost")
        return result

    async def wait(seconds):
        sleeps.append(seconds)
        if state.get("order_id"):
            order = gateway.orders.get_order(state["order_id"]).model_dump(mode="json")
            assert review(client, order).status_code == 200

    result = asyncio.run(ExceptionJourney(uncertain, sleep=wait,
        save=lambda s: saved.write_text(json.dumps(s))).run(state=state))
    assert result["order"]["status"] == "CONFIRMED"
    assert len(placements) == 2 and placements[0] == placements[1]
    assert sleeps == [1, 5]
    assert len(gateway.orders.list_orders()) == 1
    assert json.loads(saved.read_text())["order_id"] == result["order"]["id"]
    transcript = capsys.readouterr().out
    assert f"Receipt id: {result['order']['id']}" in transcript
    assert "PENDING_APPROVAL" in transcript and "CONFIRMED" in transcript and "Evidence manifest:" in transcript
    assert result["evidence"]["environment"] == "sandbox"
    assert verify_jws(result["receipt"]["receipt"], receipt_payload(result["receipt"]), gateway.signing.jwks())


def test_ac_8_restarted_client_polls_same_order_to_same_outcome(sandbox, tmp_path):
    gateway, server, client = sandbox
    saved = tmp_path / "pending.json"

    async def no_decision(seconds):
        pass

    with pytest.raises(RuntimeError, match="Still pending"):
        asyncio.run(ExceptionJourney(server.call_tool, sleep=no_decision, max_polls=1,
            save=lambda s: saved.write_text(json.dumps(s))).run())
    state = json.loads(saved.read_text())
    original = gateway.orders.get_order(state["order_id"])
    assert original.status == "PENDING_APPROVAL"
    polled = []
    restarted_server = create_server(gateway, "sandbox-agent")

    async def resumed_call(name, arguments):
        polled.append(name)
        return await restarted_server.call_tool(name, arguments)

    async def human_decides(seconds):
        assert seconds == original.poll_after_seconds
        assert review(client, original.model_dump(mode="json")).status_code == 200

    result = asyncio.run(ExceptionJourney(resumed_call, sleep=human_decides).run(state={"order_id": state["order_id"]}))
    assert result["order"]["id"] == original.id and result["order"]["status"] == "CONFIRMED"
    assert polled == ["get_order_status", "get_order_status", "get_order_receipt", "get_order_evidence"]
    assert gateway.orders.get_order(original.id).order_revision == original.order_revision + 1
    assert len(gateway.orders.list_orders()) == 1
