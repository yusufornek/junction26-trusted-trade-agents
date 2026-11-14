"""Spec 28: buyer scope, authority, ledger and activity (one test per AC)."""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.models import PolicyUpdateRequest
from conftest import OPERATOR_HEADERS, agent_headers, quote_request
from test_verifiable_mandate import buyer as vc_issuer, issue
from app.mandates import decode
from app import audit
from datetime import timedelta


@pytest.fixture
def buyer_setup(gateway, monkeypatch):
    # Align synthetic registrations before first use. Withdrawing customer scope
    # must not migrate an established agent or its history to another tenant.
    original_update = gateway.policy.update
    def update(request, *args, **kwargs):
        result = original_update(request, *args, **kwargs)
        with gateway.db.connect(write=True) as conn:
            for aid, mandate in result.agents.items():
                tenants = {r[0] for customer in mandate.customer_ids for r in conn.execute(
                    "SELECT tenant_id FROM tenant_customers WHERE customer_id=?", (customer,))}
                used = conn.execute("SELECT 1 FROM lookups WHERE json_extract(data,'$.agent_id')=? "
                    "UNION ALL SELECT 1 FROM quotes WHERE json_extract(data,'$.agent_id')=? "
                    "UNION ALL SELECT 1 FROM orders WHERE json_extract(data,'$.agent_id')=? LIMIT 1",
                    (aid, aid, aid)).fetchone()
                if len(tenants) == 1 and not used:
                    tid = next(iter(tenants))
                    conn.execute("UPDATE tenant_agents SET tenant_id=?,owner_user_id=(SELECT id FROM users WHERE tenant_id=? AND role='tenant_admin' ORDER BY created_at,id LIMIT 1) WHERE agent_id=?", (tid, tid, aid))
        return result
    monkeypatch.setattr(gateway.policy, "update", update)
    body = gateway.policy._editable(gateway.policy.get())
    body["organisations"] = {"H": {"customer_ids": ["1"]}, "B": {"customer_ids": ["2"]}}
    body["identities"] = {
        "external": {"organisation_id": "H"}, "outsider": {"organisation_id": "H"},
        "agent-b": {"organisation_id": "B"},
        "hotel-agent": {"kind": "buyer_user", "role": "buyer_admin", "organisation_id": "H"},
        "buyer": {"kind": "buyer_user", "role": "buyer_approver", "organisation_id": "H"},
        "observer": {"kind": "buyer_user", "role": "buyer_viewer", "organisation_id": "H"}}
    body["agents"]["agent-b"] = {"customer_ids": ["2"], "max_auto_daily_total_eur": 5000,
                                    "autonomy": "autonomous_within_limits"}
    gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Fixture operator"))


def place(gateway, need, *, qty=10, agent="external", mandate=None):
    lookup = gateway.find_offers(need, f"agent:{agent}", mandate=mandate)
    quote = gateway.orders.create_quote(quote_request(need, qty=qty, need_ref=lookup.lookup_id), agent, mandate=mandate)
    return gateway.orders.place_order(quote.id, str(uuid4()), agent, mandate=mandate)


def test_ac_1_viewer_sees_only_organisation_agents_and_orders(gateway, need, upstream, buyer_setup):
    own = place(gateway, need)
    other = place(gateway, need.model_copy(update={"customer_id": "2"}), agent="agent-b")
    with TestClient(create_app(gateway), headers=agent_headers("observer")) as client:
        agents = client.get("/v1/buyer/agents").json()
        assert agents["customer_ids"] == ["1"] and agents["organisation_id"] == "H"
        assert "external" in {a["agent_id"] for a in agents["agents"]}
        assert "agent-b" not in {a["agent_id"] for a in agents["agents"]}
        result = client.get("/v1/buyer/orders").json()
        assert [o["id"] for o in result["orders"]] == [own.id]
        assert other.id not in str(result)
        for endpoint in ("mandates", "spend", "approvals", "activity"):
            response = client.get("/v1/buyer/" + endpoint)
            assert response.status_code == 200 and response.json()["customer_ids"] == ["1"]
        assert client.get("/v1/buyer/agents", headers=agent_headers("external")).status_code == 403
        assert client.get("/v1/buyer/orders", headers=OPERATOR_HEADERS).status_code == 200


def test_review_activity_shared_customer_isolation(gateway, need, upstream, buyer_setup):
    body = gateway.policy._editable(gateway.policy.get())
    body["organisations"]["B"]["customer_ids"] = ["1"]
    body["agents"]["agent-b"]["customer_ids"] = ["1"]
    gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Fixture"))
    own = place(gateway, need)
    foreign = place(gateway, need, agent="agent-b")
    with TestClient(create_app(gateway), headers=agent_headers("observer")) as client:
        events = client.get("/v1/buyer/activity").json()["activity"]
        assert own.id in str(events)
        assert foreign.id not in str(events)
        assert foreign.quote_id not in str(events)
        assert all(e["actor"] != "agent:agent-b" for e in events)


def test_review_unassigned_agent_not_shared_by_customer(gateway, need, upstream, buyer_setup):
    body = gateway.policy._editable(gateway.policy.get())
    body["agents"]["agent-b"]["customer_ids"] = ["1"]
    body["identities"].pop("agent-b")
    gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Fixture"))
    foreign = place(gateway, need, agent="agent-b")
    with TestClient(create_app(gateway), headers=agent_headers("observer")) as client:
        for path in ("orders", "activity", "agents"):
            response = client.get("/v1/buyer/" + path)
            assert foreign.id not in response.text and "agent-b" not in response.text


@pytest.mark.parametrize("invalid", ["revoked", "expired", "issuer"])
def test_review_spend_ignores_invalid_mandates(gateway, need, upstream, buyer_setup, vc_issuer, monkeypatch, invalid):
    from app.mandates import sign_jwt
    claims = decode(issue(vc_issuer, sub="external", daily=100))[1]
    claims["vc"]["credentialSubject"]["agent_id"] = "external"
    token = sign_jwt(vc_issuer, claims, kid="buyer-1")
    place(gateway, need, mandate=token)
    if invalid == "revoked":
        with TestClient(create_app(gateway), headers=agent_headers("hotel-agent")) as client:
            assert client.post(f"/v1/buyer/mandates/{decode(token)[1]['jti']}/revoke").status_code == 200
    elif invalid == "expired":
        now = audit.utcnow()
        monkeypatch.setattr(audit, "utcnow", lambda: now + timedelta(days=8))
    else:
        body = gateway.policy._editable(gateway.policy.get())
        body["customers"]["1"]["mandate_issuers"] = {}
        gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Fixture"))
    with TestClient(create_app(gateway), headers=agent_headers("observer")) as client:
        row = next(r for r in client.get("/v1/buyer/spend").json()["spend"] if r["agent_id"] == "external")
        assert (row["limit"], row["limit_source"]) == (5000, "policy")


def test_review_spend_expires_pending_commitments(gateway, need, upstream, buyer_setup, monkeypatch):
    order = place(gateway, need, qty=200)
    assert order.status == "PENDING_APPROVAL"
    monkeypatch.setattr(audit, "utcnow", lambda: order.expires_at + timedelta(seconds=1))
    with TestClient(create_app(gateway), headers=agent_headers("observer")) as client:
        row = next(r for r in client.get("/v1/buyer/spend").json()["spend"] if r["agent_id"] == "external")
        assert row["pending_commitments"] == 0


@pytest.mark.parametrize("surface", ["lookup", "quote", "order", "timeline", "evidence", "buyer_orders", "activity", "task", "webhook", "owner_timeline", "owner_evidence"])
def test_review_shared_customer_read_paths(gateway, need, upstream, buyer_setup, monkeypatch, respx_mock, surface):
    """Agent B and B's buyer must not see A's objects or linked foreign refusals."""
    import asyncio
    import json
    import socket
    import httpx
    from app.auth import Principal
    from app.webhooks import SubscriptionCreate
    from app.a2a import metadata
    from test_a2a_service import sdk, purchase, artifact_data
    from zipfile import ZipFile
    import io

    body = gateway.policy._editable(gateway.policy.get())
    body["organisations"]["B"]["customer_ids"] = ["1"]
    body["agents"]["agent-b"]["customer_ids"] = ["1"]
    body["identities"]["buyer-b"] = {"kind": "buyer_user", "role": "buyer_viewer", "organisation_id": "B"}
    gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Fixture"))
    received = []
    monkeypatch.setattr(gateway.webhooks, "resolver", lambda *a, **kw: [(socket.AF_INET, 1, 6, "", ("93.184.216.34", 443))])
    def receive(request):
        data = json.loads(request.content)
        if data.get("type") == "webhook.challenge":
            return httpx.Response(200, json={"challenge": data["challenge"]})
        received.append(data)
        return httpx.Response(200)
    respx_mock.post("https://93.184.216.34/scope").mock(side_effect=receive)
    sub = gateway.webhooks.create(SubscriptionCreate(url="https://receiver.test/scope", customer_ids=["1"], event_types=["order.confirmed"]),
                                 Principal("agent-b", customer_ids=("1",), organisation_id="B"))
    small = need.model_copy(update={"quantity": need.quantity.model_copy(update={"value": 10})})
    async def buy():
        async with sdk(gateway) as (client, _, _):
            return await purchase(client, small)
    task = asyncio.run(buy())
    order = artifact_data(task, "order")
    with TestClient(create_app(gateway), headers=agent_headers("agent-b")) as client:
        denied = client.post(f"/v1/orders/{order['id']}/cancel", json={"cancelled_by": "B", "reason": "B PRIVATE REASON"})
        assert denied.status_code == 404
        paths = {"lookup": f"/v1/lookups/{metadata(task)['request_id']}", "quote": f"/v1/quotes/{order['quote_id']}",
                 "order": f"/v1/orders/{order['id']}", "timeline": f"/v1/orders/{order['id']}/timeline", "evidence": f"/v1/orders/{order['id']}/evidence"}
        if surface in paths:
            response = client.get(paths[surface])
            assert response.status_code == 404
            assert response.json()["data"] == {}
        elif surface in {"buyer_orders", "activity"}:
            principal = Principal("buyer-b", kind="buyer_user", role="buyer_viewer", organisation_id="B", customer_ids=("1",))
            result = gateway.buyer.orders(principal) if surface == "buyer_orders" else gateway.buyer.activity(principal)
            assert order["id"] not in str(result) and order["quote_id"] not in str(result)
        elif surface == "task":
            response = client.post("/a2a", headers={"A2A-Version": "1.0"}, json={"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": task.id}})
            assert response.json()["error"]["code"] == -32001
            assert order["id"] not in response.text
        elif surface == "webhook":
            gateway.webhooks.dispatch()
            assert not received
        elif surface == "owner_timeline":
            response = client.get(f"/v1/orders/{order['id']}/timeline", headers=agent_headers("external"))
            assert response.status_code == 200
            assert "B PRIVATE REASON" not in response.text and "agent:agent-b" not in response.text
        else:
            response = client.get(f"/v1/orders/{order['id']}/evidence", headers=agent_headers("external"))
            assert response.status_code == 200
            with ZipFile(io.BytesIO(response.content)) as bundle:
                assert all(b"B PRIVATE REASON" not in bundle.read(name) and b"agent:agent-b" not in bundle.read(name) for name in bundle.namelist())


def test_ac_2_spend_equals_policy_ledger(gateway, need, upstream, buyer_setup):
    first, second = place(gateway, need), place(gateway, need, qty=20)
    human = place(gateway, need, qty=200)
    assert human.status == "PENDING_APPROVAL"
    gateway.orders.approve(human.id, "Ada")
    pending = place(gateway, need, qty=200)
    with TestClient(create_app(gateway), headers=agent_headers("observer")) as client:
        row = next(r for r in client.get("/v1/buyer/spend").json()["spend"] if r["agent_id"] == "external")
    with gateway.db.connect() as conn:
        assert row["autonomous_spend"] == float(gateway.policy.autonomous_total(conn, "external")) == first.total + second.total
    assert row["limit"] == 5000 and row["limit_source"] == "policy" and row["currency"] == "EUR"
    assert row["remaining"] == 5000 - first.total - second.total
    assert row["human_approved_spend"] == human.total and row["pending_commitments"] == pending.total


def test_ac_3_suspension_blocks_next_lookup_and_resume_restores(gateway, need, upstream, buyer_setup):
    own = place(gateway, need)
    with TestClient(create_app(gateway), headers=agent_headers("hotel-agent")) as client:
        assert client.post("/v1/buyer/agents/external/suspend", headers=agent_headers("buyer")).status_code == 403
        assert client.post("/v1/buyer/agents/agent-b/suspend").status_code == 404
        for _ in range(2):
            assert client.post("/v1/buyer/agents/external/suspend", json={"reason": "Emergency stop"},
                               headers={"Idempotency-Key": "stop-agent"}).status_code == 200
        assert len([e for e in gateway.audit() if e.action == "AGENT_SUSPENDED"]) == 1
        refusal = client.post("/v1/offers", json=need.model_dump(mode="json"), headers=agent_headers("external"))
        assert refusal.status_code == 403 and refusal.json()["code"] == "AGENT_SUSPENDED"
        assert refusal.json()["next_action"] == "CONTACT_ADMIN"
        assert client.get(f"/v1/orders/{own.id}", headers=agent_headers("external")).status_code == 200
        agents = client.get("/v1/buyer/agents").json()["agents"]
        suspended = next(a for a in agents if a["agent_id"] == "external")["suspension"]
        assert suspended["source"] == "buyer_local" and suspended["reason"] == "Emergency stop"
        assert client.post("/v1/buyer/agents/external/resume").status_code == 200
        assert client.post("/v1/offers", json=need.model_dump(mode="json"), headers=agent_headers("external")).status_code == 200


def test_ac_4_revocation_blocks_commands_and_pending_approval(gateway, need, upstream, buyer_setup, vc_issuer):
    token = issue(vc_issuer)
    confirmed = place(gateway, need, agent="outsider", mandate=token)
    pending = place(gateway, need, qty=200, agent="outsider", mandate=token)
    jti = decode(token)[1]["jti"]
    with TestClient(create_app(gateway), headers=agent_headers("hotel-agent")) as client:
        assert client.post(f"/v1/buyer/mandates/{jti}/revoke").status_code == 200
        result = client.post("/v1/offers", json=need.model_dump(mode="json"),
                             headers={**agent_headers("outsider"), "Agent-Mandate": token})
        assert result.status_code == 403 and result.json()["data"]["check"] == "revoked"
        result = client.post(f"/v1/buyer/approvals/{pending.id}/approve", json={"approver": "Ada"})
        assert result.status_code == 403 and result.json()["code"] == "MANDATE_INVALID"
        assert confirmed.id in {o["id"] for o in client.get("/v1/buyer/orders").json()["orders"]}
        assert next(m for m in client.get("/v1/buyer/mandates").json()["mandates"] if m["jti"] == jti)["revoked_at"]


def test_ac_5_approver_can_review_but_viewer_cannot(gateway, need, upstream, buyer_setup):
    order = place(gateway, need, qty=200)
    with TestClient(create_app(gateway), headers=agent_headers("buyer")) as client:
        path = f"/v1/buyer/approvals/{order.id}/approve"
        assert client.post(path, json={"approver": "Ada"}, headers=agent_headers("observer")).status_code == 403
        stale = client.post(path, json={"approver": "Ada", "evidence_revision": 2})
        assert stale.status_code == 409 and stale.json()["code"] == "EVIDENCE_CHANGED"
        upstream.shift = 1
        changed = client.post(path, json={"approver": "Ada", "evidence_revision": 1})
        assert changed.status_code == 409 and changed.json()["code"] == "EVIDENCE_CHANGED"
        result = client.post(path, json={"approver": "Ada", "evidence_revision": 2})
        assert result.status_code == 200, result.text
        assert result.json()["order"]["approval_side"] == "buyer"
        assert result.json()["order"]["status"] == "CONFIRMED"
        assert result.json()["customer_ids"] == ["1"]
        upstream.shift = 0
        rejected = place(gateway, need, qty=200)
        response = client.post(f"/v1/buyer/approvals/{rejected.id}/reject", json={"approver": "Ada"})
        assert response.status_code == 200 and response.json()["order"]["status"] == "REJECTED"
    entry = next(e for e in gateway.audit(order_id=order.id) if e.action == "APPROVE")
    assert entry.actor == "human:buyer_approver:agent:buyer:key:1" and entry.data["organisation_id"] == "H"


def test_ac_6_other_organisation_approval_is_not_found(gateway, need, upstream, buyer_setup):
    order = place(gateway, need.model_copy(update={"customer_id": "2"}), qty=200, agent="agent-b")
    with TestClient(create_app(gateway), headers=agent_headers("buyer")) as client:
        for action in ("approve", "reject"):
            for prefix in ("/v1/buyer/approvals", "/v1/approvals"):
                result = client.post(f"{prefix}/{order.id}/{action}", json={"approver": "Ada"})
                assert result.status_code == 404 and result.json()["code"] == "ORDER_NOT_FOUND"


def test_ac_7_activity_contains_controls_and_approval_actors(gateway, need, upstream, buyer_setup, vc_issuer):
    token = issue(vc_issuer)
    place(gateway, need, agent="outsider", mandate=token)
    pending = place(gateway, need, qty=200)
    with TestClient(create_app(gateway), headers=agent_headers("hotel-agent")) as client:
        client.post("/v1/buyer/agents/outsider/suspend")
        client.post(f"/v1/buyer/mandates/{decode(token)[1]['jti']}/revoke")
        response = client.post(f"/v1/buyer/approvals/{pending.id}/approve", json={"approver": "Ada"}, headers=agent_headers("buyer"))
        assert response.status_code == 200, response.text
        events = client.get("/v1/buyer/activity").json()["activity"]
    assert [e["ts"] for e in events] == sorted((e["ts"] for e in events), reverse=True)
    for kind, role in (("agent.suspended", "buyer_admin"), ("mandate.revoked", "buyer_admin"), ("order.confirmed", "buyer_approver")):
        event = next(e for e in events if e["type"] == kind)
        assert event["actor"].startswith("human:" + role + ":") and event["summary"]


@pytest.mark.parametrize("action", ["suspend", "resume", "revoke"])
def test_review_buyer_commands_bind_payload_and_reauthorize_replays(
        gateway, need, upstream, buyer_setup, vc_issuer, action):
    token = issue(vc_issuer)
    place(gateway, need, agent="outsider", mandate=token)
    if action == "revoke":
        url = f"/v1/buyer/mandates/{decode(token)[1]['jti']}/revoke"
        changed_url = "/v1/buyer/mandates/another-mandate/revoke"
        body = {}
    else:
        url = f"/v1/buyer/agents/outsider/{action}"
        changed_url = f"/v1/buyer/agents/external/{action}"
        body = {"reason": "Emergency control"}
    with TestClient(create_app(gateway), headers=agent_headers("hotel-agent")) as client:
        def call(target, key):
            return client.post(target, json=body, headers={"Idempotency-Key": key})

        first = call(url, "buyer-command")
        assert first.status_code == 200, first.text
        entries = len(gateway.audit())
        assert call(url, "buyer-command").json() == first.json()
        assert len(gateway.audit()) == entries
        conflict = call(changed_url, "buyer-command")
        assert (conflict.status_code, conflict.json()["code"]) == (409, "IDEMPOTENCY_CONFLICT")

        policy = gateway.policy._editable(gateway.policy.get())
        policy["identities"]["hotel-agent"]["organisation_id"] = "B"
        gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Fixture operator"))
        fresh = call(url, "fresh-buyer-command")
        replay = call(url, "buyer-command")
        assert (fresh.status_code, replay.status_code) == (404, 404)
        assert fresh.json()["code"] == replay.json()["code"] == "AGENT_NOT_FOUND"
