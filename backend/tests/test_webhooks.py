"""Spec 33 acceptance: actual HTTP delivery through the durable outbox."""
import json
import socket
from datetime import datetime, timedelta
from urllib.parse import urlsplit, parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.auth import Principal
from app.webhooks import SubscriptionCreate, SubscriptionPatch, verify_webhook
from conftest import OPERATOR_HEADERS, agent_headers
from test_shared_foundation import make_quote


@pytest.fixture
def receiver(gateway, respx_mock, monkeypatch):
    state = {"status": 200, "calls": [], "processed": set(), "challenge": True}
    monkeypatch.setattr(gateway.webhooks, "resolver", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    gateway.webhooks.jitter = lambda: 0

    def receive(request):
        body = json.loads(request.content)
        if body.get("type") == "webhook.challenge":
            return httpx.Response(200, json={"challenge": body["challenge"] if state["challenge"] else "wrong"})
        state["calls"].append(request)
        if state["status"] == 200:
            state["processed"].add(request.headers["webhook-id"])
        return httpx.Response(state["status"])

    respx_mock.post("https://93.184.216.34/hook").mock(side_effect=receive)
    return state


def subscribe(gateway, *, customers=("1",), audience="agent", event_types=None, principal=None):
    return gateway.webhooks.create(SubscriptionCreate(url="https://receiver.test/hook", customer_ids=list(customers),
        event_types=event_types or ["order.approval_required", "order.confirmed"], audience=audience),
        principal or Principal("external", customer_ids=("1",)))


def due(gateway):
    with gateway.db.connect(write=True) as conn:
        conn.execute("UPDATE outbox SET next_attempt_at='2000-01-01T00:00:00+00:00' WHERE consumer LIKE 'webhook:%' AND status='pending'")
    gateway.webhooks.dispatch()


def test_ac_1_pending_order_one_valid_signed_delivery(gateway, need, upstream, receiver):
    sub = subscribe(gateway)
    order = gateway.orders.place_order(make_quote(gateway, need, qty=200).id, "pending-webhook")
    assert order.status == "PENDING_APPROVAL"
    assert len(receiver["calls"]) == 1
    request = receiver["calls"][0]
    assert verify_webhook(request.content, request.headers, gateway.signing.jwks())
    body = json.loads(request.content)
    assert body["id"] == request.headers["webhook-id"]
    assert body["type"] == "order.approval_required" and body["aggregate_revision"] == 1
    assert request.headers["host"] == "receiver.test"
    assert request.extensions["sni_hostname"] == "receiver.test"
    rows = gateway.webhooks.deliveries(sub["id"], Principal("external", customer_ids=("1",)))
    assert len(rows) == 1 and rows[0]["status"] == "delivered"


def test_ac_2_retry_grows_recovery_and_receiver_deduplicates(gateway, need, upstream, receiver, monkeypatch):
    from app import audit
    clock = [audit.utcnow()]
    monkeypatch.setattr(audit, "utcnow", lambda: clock[0])
    sub = subscribe(gateway)
    receiver["status"] = 503
    gateway.orders.place_order(make_quote(gateway, need, qty=200).id, "retry-webhook")
    principal = Principal("external", customer_ids=("1",))
    first = gateway.webhooks.deliveries(sub["id"], principal)[0]
    first_time = datetime.fromisoformat(first["next_attempt_at"])
    assert first_time - clock[0] == timedelta(seconds=2)
    clock[0] = first_time - timedelta(microseconds=1)
    gateway.webhooks.dispatch()
    assert len(receiver["calls"]) == 1
    clock[0] = first_time
    gateway.webhooks.dispatch()
    second = gateway.webhooks.deliveries(sub["id"], principal)[0]
    second_time = datetime.fromisoformat(second["next_attempt_at"])
    assert second_time - clock[0] == timedelta(seconds=4)
    receiver["status"] = 200
    clock[0] = second_time - timedelta(microseconds=1)
    gateway.webhooks.dispatch()
    assert len(receiver["calls"]) == 2
    clock[0] = second_time
    gateway.webhooks.dispatch()
    assert gateway.webhooks.deliveries(sub["id"], principal)[0]["status"] == "delivered"
    ids = [r.headers["webhook-id"] for r in receiver["calls"]]
    assert len(ids) == 3 and len(set(ids)) == 1
    assert len({r.content for r in receiver["calls"]}) == 1
    gateway.webhooks.redeliver(sub["id"], first["id"], principal)
    gateway.webhooks.dispatch()
    assert len(receiver["processed"]) == 1


@pytest.mark.parametrize("historical", [False, True])
def test_review_disabled_backlog_does_not_starve_consumers(gateway, need, upstream, receiver, historical):
    from app import audit
    gateway.db.on_commit = None
    sub = subscribe(gateway)
    with gateway.db.connect(write=True) as conn:
        for i in range(105):
            audit.append(conn, "buyer", "MANDATE_REVOKED", "Revoked", data={"customer_id": "1", "issuer": "buyer", "jti_revoked": [str(i)], "mandate_id": str(i)})
    gateway.webhooks.disable(sub["id"], Principal("external", customer_ids=("1",)))
    if historical:
        # Older releases left these pending; startup/dispatch must tolerate that state.
        with gateway.db.connect(write=True) as conn:
            conn.execute("UPDATE outbox SET status='pending' WHERE consumer=?", ("webhook:" + sub["id"],))
    active = subscribe(gateway)
    received = []
    gateway.outbox.register("review:other", lambda event: received.append(event["id"]))
    order = gateway.orders.place_order(make_quote(gateway, need, qty=200).id, "active-after-disabled")
    for _ in range(3):
        gateway.outbox.dispatch()
        gateway.webhooks.dispatch()
    assert received
    assert gateway.webhooks.deliveries(active["id"], Principal("external", customer_ids=("1",)))[0]["status"] == "delivered"
    assert any(json.loads(r.content)["aggregate_id"] == order.id for r in receiver["calls"])
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM outbox WHERE consumer=? AND status='pending'", ("webhook:" + sub["id"],)).fetchone()[0] == 0


@pytest.mark.parametrize("keyed", [False, True])
def test_review_challenge_bounded_outside_writer(gateway, receiver, monkeypatch, keyed):
    import sqlite3
    consumed = []
    writer_available = []
    class Body(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(128):
                consumed.append(1)
                yield b" " * 65536
    def receive(request):
        with sqlite3.connect(gateway.db.path, timeout=0) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                writer_available.append(True)
            except sqlite3.OperationalError:
                writer_available.append(False)
        return httpx.Response(200, stream=Body())
    gateway.webhooks.client.close()
    gateway.webhooks.client = httpx.Client(transport=httpx.MockTransport(receive))
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        response = client.post("/v1/webhook-subscriptions", json={"url": "https://receiver.test/hook", "customer_ids": ["1"],
            "event_types": ["order.confirmed"]}, headers={"Idempotency-Key": "bounded"} if keyed else {})
        assert response.status_code == 422
    assert writer_available == [True]
    assert len(consumed) <= 2


def test_review_delivery_does_not_read_body(gateway, need, upstream, receiver):
    sub = subscribe(gateway)
    class Body(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError("Delivery response body must not be consumed")
        def close(self):
            closed.append(True)
    closed = []
    gateway.webhooks.client.close()
    gateway.webhooks.client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Body())))
    gateway.orders.place_order(make_quote(gateway, need, qty=200).id, "bodyless-delivery")
    assert closed == [True]
    assert gateway.webhooks.deliveries(sub["id"], Principal("external", customer_ids=("1",)))[0]["status"] == "delivered"


def test_review_challenge_overall_deadline(gateway, receiver):
    ticks, chunks = [0], []
    gateway.webhooks.clock = lambda: ticks[0]
    class Body(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(100):
                ticks[0] += 1
                chunks.append(1)
                yield b" "
    gateway.webhooks.client.close()
    gateway.webhooks.client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=Body())))
    with pytest.raises(Exception, match="WEBHOOK_CHALLENGE_FAILED"):
        subscribe(gateway)
    assert len(chunks) <= 5


def test_review_retry_test_rejects_constant_delay(gateway, need, upstream, receiver, monkeypatch):
    import inspect
    import textwrap
    import types
    from app import webhooks
    source = textwrap.dedent(inspect.getsource(webhooks.Webhooks.dispatch))
    original = 'self.base_delay * 2 ** min(row["attempts"], 12)'
    assert original in source
    namespace = dict(vars(webhooks))
    exec(compile(source.replace(original, "self.base_delay"), "<constant-delay-mutation>", "exec"), namespace)
    monkeypatch.setattr(gateway.webhooks, "dispatch", types.MethodType(namespace["dispatch"], gateway.webhooks))
    with pytest.raises(AssertionError):
        test_ac_2_retry_grows_recovery_and_receiver_deduplicates(gateway, need, upstream, receiver, monkeypatch)


def test_review_failing_subscription_does_not_own_batch(gateway, need, upstream, receiver, respx_mock):
    from app import audit
    gateway.db.on_commit = None
    subscribe(gateway, event_types=["mandate.revoked"])
    receiver["status"] = 503
    with gateway.db.connect(write=True) as conn:
        for index in range(105):
            audit.append(conn, "buyer", "MANDATE_REVOKED", "Revoked", data={"customer_id": "1", "issuer": "buyer",
                         "agent_id": "external", "jti_revoked": [str(index)], "mandate_id": str(index)})
    respx_mock.post("https://93.184.216.34/active").mock(side_effect=lambda req: httpx.Response(200,
        json={"challenge": json.loads(req.content).get("challenge")}))
    active = subscribe(gateway)
    gateway.webhooks.update(active["id"], SubscriptionPatch(url="https://receiver.test/active"), Principal("external", customer_ids=("1",)))
    gateway.orders.place_order(make_quote(gateway, need, qty=200).id, "fair-delivery")
    gateway.webhooks.dispatch()
    rows = gateway.webhooks.deliveries(active["id"], Principal("external", customer_ids=("1",)))
    assert rows and rows[0]["status"] == "delivered"


def test_ac_3_tampering_fails_published_key_verification(gateway, need, upstream, receiver):
    subscribe(gateway)
    gateway.orders.place_order(make_quote(gateway, need, qty=200).id, "tamper-webhook")
    request = receiver["calls"][0]
    with pytest.raises(ValueError, match="signature"):
        verify_webhook(request.content + b" ", request.headers, gateway.signing.jwks())
    with pytest.raises(ValueError, match="timestamp"):
        verify_webhook(request.content, request.headers, gateway.signing.jwks(), now=int(request.headers["webhook-timestamp"]) + 301)


def test_ac_4_unsafe_destinations_rebinding_challenge_and_redirects(gateway, need, upstream, receiver, monkeypatch, respx_mock):
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        body = {"customer_ids": ["1"], "event_types": ["order.approval_required"]}
        assert client.post("/v1/webhook-subscriptions", json={**body, "url": "http://receiver.test/hook"}).status_code == 422
        monkeypatch.setattr(gateway.webhooks, "resolver", lambda *a, **kw: [(socket.AF_INET, 1, 6, "", ("127.0.0.1", 443))])
        assert client.post("/v1/webhook-subscriptions", json={**body, "url": "https://receiver.test/hook"}).status_code == 422
    monkeypatch.setattr(gateway.webhooks, "resolver", lambda *a, **kw: [(socket.AF_INET, 1, 6, "", ("93.184.216.34", 443))])
    receiver["challenge"] = False
    with pytest.raises(Exception, match="WEBHOOK_CHALLENGE_FAILED"):
        subscribe(gateway)
    receiver["challenge"] = True
    subscribe(gateway)
    monkeypatch.setattr(gateway.webhooks, "resolver", lambda *a, **kw: [(socket.AF_INET, 1, 6, "", ("10.0.0.1", 443))])
    gateway.orders.place_order(make_quote(gateway, need, qty=200).id, "dns-webhook")
    assert receiver["calls"] == []
    # Redirects are failed deliveries; the Location is never requested.
    monkeypatch.setattr(gateway.webhooks, "resolver", lambda *a, **kw: [(socket.AF_INET, 1, 6, "", ("93.184.216.34", 443))])
    respx_mock.post("https://93.184.216.34/hook").respond(302, headers={"Location": "https://10.0.0.1/private"})
    private = respx_mock.post("https://10.0.0.1/private").respond(200)
    due(gateway)
    assert not private.called
    # The one HTTP exception is explicit sandbox localhost, pinned to loopback.
    gateway.settings.webhook_sandbox = True
    monkeypatch.setattr(gateway.webhooks, "resolver", lambda *a, **kw: [(socket.AF_INET, 1, 6, "", ("127.0.0.1", 80))])
    respx_mock.post("http://127.0.0.1/hook").mock(side_effect=lambda req: httpx.Response(200, json={"challenge": json.loads(req.content)["challenge"]}))
    local = gateway.webhooks.create(SubscriptionCreate(url="http://localhost/hook", customer_ids=["1"], event_types=["order.confirmed"]), Principal("external", customer_ids=("1",)))
    assert local["verified"]


def test_ac_5_exhausted_failed_manual_redelivery_and_crud(gateway, need, upstream, receiver):
    receiver["status"] = 503
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        body = {"url": "https://receiver.test/hook", "customer_ids": ["1"], "event_types": ["order.approval_required"]}
        sub = client.post("/v1/webhook-subscriptions", json=body, headers={"Idempotency-Key": "create-sub"}).json()
        assert client.post("/v1/webhook-subscriptions", json=body, headers={"Idempotency-Key": "create-sub"}).json() == sub
        assert client.get("/v1/webhook-subscriptions").json()[0]["id"] == sub["id"]
        assert client.patch(f"/v1/webhook-subscriptions/{sub['id']}", json={"event_types": ["order.approval_required", "order.confirmed"]}).status_code == 200
        order = gateway.orders.place_order(make_quote(gateway, need, qty=200).id, "failed-webhook")
        for _ in range(gateway.webhooks.max_attempts - 1):
            due(gateway)
        rows = client.get(f"/v1/webhook-subscriptions/{sub['id']}/deliveries").json()
        assert rows[0]["status"] == "failed" and rows[0]["attempts"] == gateway.webhooks.max_attempts
        assert gateway.orders.get_order(order.id).status == "PENDING_APPROVAL"
        receiver["status"] = 200
        assert client.post(f"/v1/webhook-subscriptions/{sub['id']}/deliveries/{rows[0]['id']}/redeliver").status_code == 200
        assert client.get(f"/v1/webhook-subscriptions/{sub['id']}/deliveries").json()[0]["status"] == "delivered"
        assert client.delete(f"/v1/webhook-subscriptions/{sub['id']}").json()["enabled"] is False


def test_ac_6_customer_and_subscription_ownership_isolation(gateway, need, upstream, receiver):
    own = subscribe(gateway, event_types=["order.approval_required", "mandate.revoked", "proposal.received"])
    subscribe(gateway, customers=("2",), principal=Principal("customer-two", customer_ids=("2",)))
    gateway.orders.place_order(make_quote(gateway, need, qty=200).id, "scoped-webhook")
    assert len(receiver["calls"]) == 1
    with TestClient(create_app(gateway), headers=agent_headers("other")) as client:
        assert client.get(f"/v1/webhook-subscriptions/{own['id']}/deliveries").status_code == 404
        assert client.patch(f"/v1/webhook-subscriptions/{own['id']}", json={"enabled": False}).status_code == 404
        assert client.get("/v1/webhook-subscriptions").json() == []
        assert client.post("/v1/webhook-subscriptions", json={"url": "https://receiver.test/hook", "customer_ids": ["1"], "event_types": ["order.confirmed"]}).status_code == 403
    from app import audit
    with gateway.db.connect(write=True) as conn:
        # This test isolates ownership with a consented event. Spec 36 forbids
        # proposal delivery without both recorded and current webhook consent.
        conn.execute("INSERT INTO proposal_preferences VALUES(?,?)", ("1", json.dumps({
            "categories": ["10400"], "channels": ["webhook"]})))
        audit.append(conn, "buyer", "MANDATE_REVOKED", "Revoked", data={"issuer": "buyer", "customer_ids": ["1", "2"], "jti_revoked": ["authority"], "agent_id": "external"})
        audit.append(conn, "supplier", "PROPOSAL_RECEIVED", "Proposal", data={"proposal_id": "proposal-one", "customer_id": "1", "agent_id": "external", "channels": ["webhook"]})
    received = [json.loads(r.content) for r in receiver["calls"]]
    assert [r["type"] for r in received] == ["order.approval_required", "mandate.revoked", "proposal.received"]
    assert all(r["customer_id"] == "1" for r in received)
    assert "customer_ids" not in received[1]["payload"]


def test_ac_7_revision_bound_authenticated_review(gateway, need, upstream, receiver):
    subscribe(gateway, audience="approver", principal=Principal("web-console", operator=True, kind="operator", role="valio_operator"))
    order = gateway.orders.place_order(make_quote(gateway, need, qty=200).id, "review-webhook")
    link = json.loads(receiver["calls"][0].content)["payload"]["review_url"]
    assert link == f"/approvals/{order.id}?revision=1"
    with TestClient(create_app(gateway)) as client:
        assert client.get(link).status_code == 401
        response = client.get(link, headers=OPERATOR_HEADERS)
        assert response.status_code == 200 and "notification_delivery" in response.text
        assert client.get(f"/approvals/{order.id}?revision=2", headers=OPERATOR_HEADERS).json()["code"] == "EVIDENCE_CHANGED"
        with gateway.db.connect(write=True) as conn:
            stored = gateway.orders._order(conn, order.id)
            stored.evidence_revision = 2
            gateway.orders._save_order(conn, stored)
        stale = client.post(f"/v1/approvals/{order.id}/approve", headers=OPERATOR_HEADERS,
                            json={"approver": "Ada", "evidence_revision": 1})
        assert stale.status_code == 409 and stale.json()["code"] == "EVIDENCE_CHANGED"


@pytest.mark.parametrize("command", ["create", "update", "disable", "redeliver"])
def test_review_ac_6_keyed_webhooks_reauthorize_and_bind_targets(gateway, need, upstream, receiver, command):
    """Lane B webhook writes retain the shared core's current-scope replay guard."""
    from app.models import PolicyUpdateRequest

    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        body = {"url": "https://receiver.test/hook", "customer_ids": ["1"],
                "event_types": ["order.approval_required"]}
        first_sub = client.post("/v1/webhook-subscriptions", json=body).json()
        second_sub = client.post("/v1/webhook-subscriptions", json=body).json()
        method, url, payload = "POST", "/v1/webhook-subscriptions", body
        changed_url, changed_payload = url, {**body, "event_types": ["order.confirmed"]}
        if command in {"update", "disable"}:
            method = "PATCH" if command == "update" else "DELETE"
            url = f"/v1/webhook-subscriptions/{first_sub['id']}"
            changed_url = f"/v1/webhook-subscriptions/{second_sub['id']}"
            payload = {"event_types": ["order.confirmed"]} if command == "update" else None
            changed_payload = payload
        elif command == "redeliver":
            gateway.orders.place_order(make_quote(gateway, need, qty=200).id, "replay-webhook-order")
            first_delivery = client.get(f"/v1/webhook-subscriptions/{first_sub['id']}/deliveries").json()[0]
            second_delivery = client.get(f"/v1/webhook-subscriptions/{second_sub['id']}/deliveries").json()[0]
            url = f"/v1/webhook-subscriptions/{first_sub['id']}/deliveries/{first_delivery['id']}/redeliver"
            changed_url = f"/v1/webhook-subscriptions/{second_sub['id']}/deliveries/{second_delivery['id']}/redeliver"
            payload = changed_payload = None

        def call(key):
            return client.request(method, url, json=payload, headers={"Idempotency-Key": key})

        first = call("webhook-review")
        assert first.status_code == 200, first.text
        calls = len(receiver["calls"])
        replay = call("webhook-review")
        assert replay.status_code == 200 and replay.json() == first.json()
        assert len(receiver["calls"]) == calls
        conflict = client.request(method, changed_url, json=changed_payload,
                                  headers={"Idempotency-Key": "webhook-review"})
        assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"

        policy = gateway.policy._editable(gateway.policy.get())
        policy["agents"]["external"]["customer_ids"] = ["2"]
        gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Operator"))
        fresh, replay = call("fresh-webhook-review"), call("webhook-review")
        assert fresh.status_code == replay.status_code == 403
        assert fresh.json()["code"] == replay.json()["code"] == "CUSTOMER_NOT_IN_MANDATE"
        assert "url" not in replay.json()


def test_review_ac_6_lane_legacy_subscription_replay_keeps_authorization(gateway, receiver):
    """Commands persisted by lane B before the merge still replay under current scope."""
    from hashlib import sha256
    from app.canonical import canonical_json
    from app.models import PolicyUpdateRequest

    body = SubscriptionCreate(url="https://receiver.test/hook", customer_ids=["1"],
                              event_types=["order.confirmed"])
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        response = client.post("/v1/webhook-subscriptions", json=body.model_dump(),
                               headers={"Idempotency-Key": "lane-legacy-webhook"})
        assert response.status_code == 200
        with gateway.db.connect(write=True) as conn:
            row = conn.execute("SELECT result FROM commands WHERE principal=? AND key=?",
                               ("agent:external", "lane-legacy-webhook")).fetchone()
            saved = json.loads(row["result"])
            saved.pop("fingerprint_version")
            digest = sha256(canonical_json({"command": "create", "arguments": {
                "body": body.model_dump(), "task_id": None, "push_config": None}})).hexdigest()
            conn.execute("UPDATE commands SET digest=?,result=? WHERE principal=? AND key=?",
                         (digest, json.dumps(saved), "agent:external", "lane-legacy-webhook"))
        replay = client.post("/v1/webhook-subscriptions", json=body.model_dump(),
                             headers={"Idempotency-Key": "lane-legacy-webhook"})
        assert replay.status_code == 200 and replay.json() == response.json()
        assert len(client.get("/v1/webhook-subscriptions").json()) == 1
        policy = gateway.policy._editable(gateway.policy.get())
        policy["agents"]["external"]["customer_ids"] = ["2"]
        gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Operator"))
        refused = client.post("/v1/webhook-subscriptions", json=body.model_dump(),
                              headers={"Idempotency-Key": "lane-legacy-webhook"})
        assert refused.status_code == 403 and refused.json()["code"] == "CUSTOMER_NOT_IN_MANDATE"
