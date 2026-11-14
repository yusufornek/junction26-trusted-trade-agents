"""Spec 28: buyer scope, authority, ledger and activity (one test per AC)."""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.models import PolicyUpdateRequest
from conftest import OPERATOR_HEADERS, agent_headers, quote_request
from test_verifiable_mandate import buyer as vc_issuer, issue
from app.mandates import decode


@pytest.fixture
def buyer_setup(gateway):
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
