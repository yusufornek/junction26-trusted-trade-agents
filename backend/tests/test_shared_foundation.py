"""Spec 22 acceptance coverage: shared state, durable events and identity."""
import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import audit
from app.api import create_app
from app.auth import Credentials
from app.mcp_server import create_server
from app.models import PolicyUpdateRequest
from conftest import agent_headers, quote_request


def make_quote(gateway, need, agent="external", qty=1):
    lookup = gateway.find_offers(need, actor=f"agent:{agent}")
    return gateway.orders.create_quote(quote_request(need, need_ref=lookup.lookup_id, qty=qty), agent)


def event_rows(gateway, aggregate_id):
    with gateway.db.connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM events WHERE aggregate_id=? ORDER BY aggregate_revision", (aggregate_id,))]


def test_ac_1_rest_mcp_same_policy_and_single_events(gateway, need, upstream):
    rest_quote = make_quote(gateway, need)
    mcp_quote = make_quote(gateway, need)
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        response = client.post("/v1/orders", json={"quote_id": rest_quote.id}, headers={"Idempotency-Key": "rest"})
        assert response.status_code == 200, response.text
    remote = asyncio.run(create_server(gateway, "external").call_tool("place_order", {"quote_id": mcp_quote.id, "idempotency_key": "mcp"}))
    assert not remote.is_error
    first, second = response.json(), remote.structured_content
    for field in ("status", "policy", "total", "currency", "customer_id", "agent_id", "commitment", "next_action"):
        assert first[field] == second[field]
    assert {k: v for k, v in first["context"].items() if k != "request_ref"} == {k: v for k, v in second["context"].items() if k != "request_ref"}
    for order in (first, second):
        entries = [e for e in gateway.audit(order_id=order["id"]) if e.action == "PLACE_ORDER"]
        assert len(entries) == 1
        assert len(event_rows(gateway, order["id"])) == 1
        assert event_rows(gateway, order["id"])[0]["type"] == "order.placed"


@pytest.mark.parametrize("registered_agents", [("external", "other")], indirect=True)
def test_ac_2_cross_transport_principal_scoped_payload_binding(gateway, need, upstream):
    quote = make_quote(gateway, need)
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        first = client.post("/v1/orders", json={"quote_id": quote.id}, headers={"Idempotency-Key": "shared"}).json()
        server = create_server(gateway, "external")
        replay = asyncio.run(server.call_tool("place_order", {"quote_id": quote.id, "idempotency_key": "shared"}))
        assert replay.structured_content["id"] == first["id"]
        changed = make_quote(gateway, need)
        conflict = client.post("/v1/orders", json={"quote_id": changed.id}, headers={"Idempotency-Key": "shared"})
        assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    other = make_quote(gateway, need, "other")
    assert gateway.orders.place_order(other.id, "shared", "other").id != first["id"]
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 2


def test_ac_3_audit_failure_rolls_back_state_events_and_command(gateway, need, upstream):
    quote = make_quote(gateway, need)
    with gateway.db.connect(write=True) as conn:
        conn.execute("CREATE TRIGGER fail_order_audit BEFORE INSERT ON audit WHEN NEW.action='PLACE_ORDER' BEGIN SELECT RAISE(ABORT,'audit failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="audit failed"):
        gateway.orders.place_order(quote.id, "rollback")
    with gateway.db.connect() as conn:
        for table in ("orders", "reservations", "commands"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM quotes WHERE id=?", (quote.id,)).fetchone()[0] == "OPEN"
        assert conn.execute("SELECT COUNT(*) FROM events WHERE aggregate_type='order'").fetchone()[0] == 0
    assert audit.verify(gateway.db)["valid"]


def test_ac_4_revisions_outbox_delivery_and_retry(gateway, need, upstream):
    received = []
    def unavailable(event):
        raise RuntimeError("offline")
    gateway.outbox.register("webhooks", unavailable)
    gateway.outbox.register("activity", unavailable)
    quote = make_quote(gateway, need)
    order = gateway.orders.place_order(quote.id, "revision")
    gateway.orders.cancel(order.id, actor="agent:external")
    events = event_rows(gateway, order.id)
    assert [e["aggregate_revision"] for e in events] == [1, 2]
    with gateway.db.connect() as conn:
        for event in events:
            rows = conn.execute("SELECT consumer,status FROM outbox WHERE event_id=?", (event["id"],)).fetchall()
            assert {(r[0], r[1]) for r in rows} == {("webhooks", "pending"), ("activity", "pending")}
    gateway.outbox.register("webhooks", lambda event: received.append(event))
    with gateway.db.connect(write=True) as conn:
        conn.execute("UPDATE outbox SET next_attempt_at='2000-01-01T00:00:00+00:00'")
    gateway.outbox.dispatch()
    assert {e["id"] for e in events} <= {e["id"] for e in received}
    assert all(e["payload"]["links"]["event_id"] == e["id"] for e in received)


def test_ac_5_correlation_and_linked_identifiers(gateway, need, upstream):
    quote = make_quote(gateway, need)
    with TestClient(create_app(gateway), headers={**agent_headers("external"), "X-Correlation-Id": "procurement-123"}) as client:
        response = client.post("/v1/orders", json={"quote_id": quote.id}, headers={"Idempotency-Key": "correlated"})
        assert response.headers["X-Correlation-Id"] == "procurement-123"
        order = response.json()
        assert order["correlation_id"] == "procurement-123"
        event = event_rows(gateway, order["id"])[0]
        assert event["correlation_id"] == "procurement-123"
        assert event["causation_id"] == "correlated"
        assert gateway.audit(order_id=order["id"])[0].data["correlation_id"] == "procurement-123"
        detail = client.get("/v1/orders/" + order["id"]).json()
        assert detail["links"]["request_id"] == quote.need_ref
        assert detail["links"]["event_ids"] == [event["id"]]
        assert detail["links"]["receipt_id"] == order["id"]
        assert client.get("/v1/orders/missing").headers["X-Correlation-Id"] == "procurement-123"


def test_ac_6_public_pinned_compatibility_manifest(gateway):
    with TestClient(create_app(gateway)) as client:
        response = client.get("/v1/meta")
    assert response.status_code == 200
    manifest = response.json()
    assert set(manifest["specifications"]) == {"openapi", "mcp_protocol", "mcp_server_card", "a2a", "ubl", "vc_data_model", "vc_securing_profile", "problem_details", "standard_webhooks"}
    assert all(value for value in manifest["specifications"].values())
    for value in manifest["specifications"].values():
        if isinstance(value, dict):
            assert value.get("version") or value.get("draft_commit") or value.get("commit")
    assert manifest["specifications"]["mcp_server_card"]["draft_commit"] == "526201bbc80231daa40ffcdecfc9da4e54e5dc93"
    assert manifest["specifications"]["standard_webhooks"]["commit"] == "36afae792227eae8a182a7a1a5711dbdc08a4334"
    assert "fastapi" in manifest["packages"] and "fastapi" not in manifest["specifications"]
    assert manifest["limiter_scope"] == "in-process, single instance"


def test_ac_7_buyer_and_supplier_principals_and_key_provisioning(gateway, tmp_path):
    from scripts.gen_keys import provision
    policy = gateway.policy.get().model_dump(mode="json")
    policy["organisations"] = {"hotel": {"customer_ids": ["1", "42"]}}
    policy["identities"] = {"viewer": {"kind": "buyer_user", "role": "buyer_viewer", "organisation_id": "hotel"},
                            "supplier": {"kind": "supplier_agent", "role": "supplier_agent"}}
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Ada"))
    keys = Credentials("operator-secret", "viewer:viewer-secret,supplier:supplier-secret")
    viewer = keys.resolve("viewer-secret", gateway.policy.get())
    supplier = keys.resolve("supplier-secret", gateway.policy.get())
    assert (viewer.kind, viewer.role, viewer.organisation_id, viewer.customer_ids) == ("buyer_user", "buyer_viewer", "hotel", ("1", "42"))
    assert supplier.kind == supplier.role == "supplier_agent"
    assert viewer.credential_ref == "agent:viewer:key:1"
    assert supplier.credential_ref == "agent:supplier:key:1"
    assert keys.resolve("viewer-secret").credential_ref == viewer.credential_ref
    env = tmp_path / ".env.local"
    provision(env, set(gateway.policy.get().identities))
    assert "viewer:" in env.read_text() and "supplier:" in env.read_text()


def test_quote_and_cancel_idempotency_across_transports(gateway, need, upstream):
    lookup = gateway.find_offers(need)
    body = quote_request(need, need_ref=lookup.lookup_id, qty=1).model_dump(mode="json")
    server = create_server(gateway, "external")
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        first = client.post("/v1/quotes", json=body, headers={"Idempotency-Key": "quote-key"})
        assert first.status_code == 200
        before = len(upstream.price_calls), len(upstream.availability_calls)
        replay = asyncio.run(server.call_tool("create_quote", {**body, "idempotency_key": "quote-key"}))
        assert replay.structured_content == first.json()
        assert before == (len(upstream.price_calls), len(upstream.availability_calls))
        changed = {**body, "lines": [{"product_id": "1", "quantity_sales_units": 2}]}
        assert client.post("/v1/quotes", json=changed, headers={"Idempotency-Key": "quote-key"}).status_code == 409
        order = client.post("/v1/orders", json={"quote_id": first.json()["id"]}, headers={"Idempotency-Key": "order-key"}).json()
        cancelled = client.post(f"/v1/orders/{order['id']}/cancel", json={"cancelled_by": "ignored for agents", "reason": "correction"}, headers={"Idempotency-Key": "cancel-key"})
        assert cancelled.status_code == 200
        again = asyncio.run(server.call_tool("cancel_order", {"order_id": order["id"], "reason": "correction", "idempotency_key": "cancel-key"}))
        assert not again.is_error and again.structured_content == cancelled.json()
    assert len([e for e in gateway.audit(order_id=order["id"]) if e.action == "CANCEL"]) == 1


def test_approval_replay_and_conflict(gateway, need, upstream):
    from conftest import OPERATOR_HEADERS
    quote = make_quote(gateway, need, qty=200)
    order = gateway.orders.place_order(quote.id, "needs-human")
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        url = f"/v1/approvals/{order.id}/approve"
        body = {"approver": "Ada", "comment": "reviewed", "evidence_revision": 1}
        first = client.post(url, json=body, headers={"Idempotency-Key": "review-key"})
        assert first.status_code == 200
        before = len(upstream.availability_calls)
        assert client.post(url, json=body, headers={"Idempotency-Key": "review-key"}).json() == first.json()
        assert len(upstream.availability_calls) == before
        assert client.post(url, json={**body, "comment": "changed"}, headers={"Idempotency-Key": "review-key"}).status_code == 409


def test_mcp_wire_correlation_metadata(gateway, need, upstream):
    server = create_server(gateway, "external")
    app = server.streamable_http_app(stateless_http=True, json_response=True)
    with TestClient(app, base_url="http://localhost:8001", headers={**agent_headers("external"), "Accept": "application/json, text/event-stream"}) as client:
        response = client.post("/mcp", headers={"X-Correlation-Id": "http-correlation"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "find_offers", "arguments": {"need": need.model_dump(mode="json")}, "_meta": {"correlation_id": "metadata-correlation"}}})
        assert response.status_code == 200, response.text
        assert response.headers["X-Correlation-Id"] == "http-correlation"
        payload = response.json()["result"]["structuredContent"]
        assert payload["correlation_id"] == "metadata-correlation"
        event = event_rows(gateway, payload["lookup_id"])[0]
        assert event["correlation_id"] == "metadata-correlation"
