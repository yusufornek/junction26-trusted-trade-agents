"""Spec 36 acceptance: buyer consent, supplier privacy and normal purchase limits."""
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import audit
from app.api import create_app
from app.models import PolicyUpdateRequest
from conftest import agent_headers


@pytest.fixture
def proposal_setup(gateway):
    body = gateway.policy._editable(gateway.policy.get())
    body["organisations"] = {"H": {"customer_ids": ["1"]}, "B": {"customer_ids": ["2"]}}
    body["identities"] = {"external": {"organisation_id": "H"},
        "hotel-agent": {"kind": "buyer_user", "role": "buyer_admin", "organisation_id": "H"},
        "buyer": {"kind": "buyer_user", "role": "buyer_approver", "organisation_id": "H"},
        "observer": {"kind": "buyer_user", "role": "buyer_viewer", "organisation_id": "H"},
        "other": {"kind": "supplier_agent", "role": "supplier_agent"},
        "agent-b": {"kind": "buyer_user", "role": "buyer_admin", "organisation_id": "B"}}
    body["customers"]["1"] = {"required_claims": ["FREE_FROM_LACTOSE"], "exclude_allergens": ["AE"]}
    gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Fixture operator"))


def campaign(name="milk", products=None, price=13, targets=None):
    products = products or ["1"]
    return {"campaign_id": name, "products": products, "rationale": "Lactose-free milk for breakfast. Supplier content only.",
        "price_evidence": [{"product_id": p, "unit_price": price, "currency": "EUR",
                            "fetched_at": audit.utcnow().isoformat()} for p in products],
        "valid_until": (audit.utcnow() + timedelta(days=2)).isoformat(),
        "target": {"customer_ids": targets or ["1"]}}


def opt_in(client, **updates):
    body = {"categories": ["10400"], "channels": ["inbox", "webhook"], "cadence_limit_per_week": 2, **updates}
    result = client.put("/v1/buyer/proposal-preferences", json=body, headers=agent_headers("hotel-agent"))
    assert result.status_code == 200, result.text
    return result.json()


def submit(client, **kwargs):
    result = client.post("/v1/proposals", json=campaign(**kwargs), headers=agent_headers("other"))
    assert result.status_code == 200, result.text
    return result.json()


def inbox(client):
    result = client.get("/v1/proposals/inbox", headers=agent_headers("external"))
    assert result.status_code == 200, result.text
    return result.json()


def test_ac_1_opt_in_delivery_inbox_mcp_and_outbox_unconsented_recorded(gateway, proposal_setup):
    import asyncio
    from app.mcp_server import create_server
    delivered = []
    gateway.outbox.register("proposal-webhook-test", lambda event: delivered.append(event) if event["type"] == "proposal.received" else None)
    with TestClient(create_app(gateway)) as client:
        opt_in(client)
        result = submit(client, targets=["1", "2"])
        assert result["delivery_count"] == 1 and result["held_back_count"] == 1
        own = inbox(client)
        assert own[0]["customer_id"] == "1" and own[0]["products"] == ["1"]
        assert client.get("/v1/proposals/inbox", headers=agent_headers("agent-b")).json() == []
        tool = asyncio.run(create_server(gateway, "external").call_tool("list_proposals", {}))
        assert not tool.is_error and own[0]["id"] in str(tool)
    assert len(delivered) == 1 and delivered[0]["customer_id"] == "1"
    assert delivered[0]["payload"]["channels"] == ["inbox", "webhook"]
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT reason FROM proposal_filter_log WHERE customer_id='2'").fetchone()[0] == "no opt-in"


def test_ac_2_standing_allergen_exclusion_filters_before_delivery(gateway, proposal_setup):
    with TestClient(create_app(gateway)) as client:
        opt_in(client)
        submit(client, products=["1", "3"])
        own = inbox(client)
        assert own[0]["products"] == ["1"]
        assert [e["product_id"] for e in own[0]["price_evidence"]] == ["1"]
    removed = next(e for e in gateway.audit() if e.action == "PROPOSAL_FILTERED")
    assert "3" in removed.data["removed"] and "AE" in str(removed.data)


def test_ac_3_accept_uses_buyer_limits_and_revalidates_stock(gateway, need, upstream, proposal_setup):
    body = gateway.policy._editable(gateway.policy.get())
    body["agents"]["external"]["max_auto_total_eur"] = 1
    gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Buyer limit"))
    with TestClient(create_app(gateway)) as client:
        opt_in(client)
        submitted = submit(client)
        id = inbox(client)[0]["id"]
        args = {"need": need.model_dump(mode="json")}
        result = client.post(f"/v1/proposals/{id}/accept", json=args, headers=agent_headers("external"))
        assert result.status_code == 200, result.text
        order = result.json()["order"]
        assert order["status"] == "PENDING_APPROVAL" and order["agent_id"] == "external"
        assert any(h["rule"] == "max_auto_total_eur" for h in order["policy"]["hits"])
        replay = client.post(f"/v1/proposals/{id}/accept", json=args, headers=agent_headers("external"))
        assert replay.json()["order"]["id"] == order["id"]
        totals = client.get(f"/v1/proposals/campaigns/{submitted['id']}", headers=agent_headers("other")).json()
        assert totals["accept_total"] == 1
    assert len(upstream.price_calls) >= 2 and len(upstream.availability_calls) >= 3
    assert len(gateway.orders.list_orders()) == 1


def test_ac_4_expired_refused_changed_price_requoted_without_purchase(gateway, need, upstream, proposal_setup):
    with TestClient(create_app(gateway)) as client:
        opt_in(client)
        submit(client)
        id = inbox(client)[0]["id"]
        args = {"need": need.model_dump(mode="json")}
        upstream.price = 14
        result = client.post(f"/v1/proposals/{id}/accept", json=args, headers=agent_headers("external"))
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["status"] == "REQUOTE_REQUIRED" and body["previous_unit_price"] == 13 and body["new_unit_price"] == 14
        assert body["quote"]["lines"][0]["unit_price"] == 14
        assert gateway.orders.list_orders() == []
        with gateway.db.connect(write=True) as conn:
            proposal = json.loads(conn.execute("SELECT data FROM proposals WHERE id=?", (id,)).fetchone()[0])
            proposal["valid_until"] = (audit.utcnow() - timedelta(seconds=1)).isoformat()
            conn.execute("UPDATE proposals SET data=? WHERE id=?", (json.dumps(proposal), id))
        refused = client.post(f"/v1/proposals/{id}/accept", json=args, headers=agent_headers("external"))
        assert refused.status_code == 409 and refused.json()["code"] == "PROPOSAL_EXPIRED"


def test_ac_5_third_weekly_proposal_held_and_policy_quotas_quiet_hours(gateway, proposal_setup):
    with TestClient(create_app(gateway)) as client:
        opt_in(client)
        assert submit(client, name="one", price=13)["delivery_count"] == 1
        assert submit(client, name="two", price=14)["delivery_count"] == 1
        assert submit(client, name="three", price=15)["delivery_count"] == 0
        assert len(inbox(client)) == 2
        assert next(e for e in gateway.audit() if e.action == "PROPOSAL_HELD").summary == "weekly cadence or customer quota"
        opt_in(client, cadence_limit_per_week=10, quiet_periods=[{"start": "00:00", "end": "00:00"}])
        assert submit(client, name="quiet", price=16)["delivery_count"] == 0
        assert any(e.action == "PROPOSAL_HELD" and e.summary == "quiet period" for e in gateway.audit())
        opt_in(client, cadence_limit_per_week=10)
        body = gateway.policy._editable(gateway.policy.get())
        body["proposal_quotas"] = {"per_supplier_per_week": 4, "per_customer_per_week": 2}
        gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Quota test"))
        assert submit(client, name="supplier-quota", price=17)["reason"] == "supplier quota"
        body["proposal_quotas"]["per_supplier_per_week"] = 10
        gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Quota test"))
        assert submit(client, name="customer-quota", price=18)["delivery_count"] == 0


def test_ac_6_mute_and_equivalent_campaign_merge_and_decline_counts(gateway, proposal_setup):
    with TestClient(create_app(gateway)) as client:
        opt_in(client)
        original = submit(client)
        equivalent = submit(client, name="renamed")
        assert equivalent["id"] == original["id"] and equivalent["merged"] is True
        assert len(inbox(client)) == 1
        id = inbox(client)[0]["id"]
        assert client.post(f"/v1/proposals/{id}/mute", headers=agent_headers("external")).status_code == 200
        assert submit(client, name="milk", price=14)["new_delivery_count"] == 0
        assert len(inbox(client)) == 1
        another = submit(client, name="different", price=15)
        second = next(p for p in inbox(client) if p["campaign_id"] == another["id"])
        assert client.post(f"/v1/proposals/{second['id']}/decline", headers=agent_headers("buyer")).status_code == 200
        counts = client.get(f"/v1/proposals/campaigns/{another['id']}", headers=agent_headers("other")).json()
        assert counts["decline_total"] == 1
    assert any(e.action == "PROPOSAL_ACTION" and e.data["action"] == "mute" for e in gateway.audit())


def test_ac_7_supplier_cannot_read_buyer_spend_limits_preferences_or_inbox(gateway, proposal_setup):
    with TestClient(create_app(gateway), headers=agent_headers("other")) as client:
        for path in ("/v1/buyer/spend", "/v1/policy", "/v1/buyer/proposal-preferences", "/v1/buyer/mandates", "/v1/proposals/inbox"):
            response = client.get(path)
            assert response.status_code == 403, (path, response.text)
        assert client.put("/v1/buyer/proposal-preferences", json={"categories": ["10400"]}).status_code == 403
        assert client.get("/v1/buyer/proposal-preferences?customer_id=2", headers=agent_headers("hotel-agent")).status_code == 403


def test_ac_8_buyer_agent_cannot_submit_supplier_proposals(gateway, proposal_setup):
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        response = client.post("/v1/proposals", json=campaign())
        assert response.status_code == 403 and response.json()["code"] == "SUPPLIER_REQUIRED"
        assert inbox(client) == []
