"""Spec 36 acceptance: buyer consent, supplier privacy and normal purchase limits."""
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import audit
from app.api import create_app
from app.auth import Principal
from app.models import PolicyUpdateRequest
from conftest import agent_headers
from test_webhooks import receiver, subscribe, due
from test_verifiable_mandate import buyer as vc_issuer, issue


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


@pytest.mark.parametrize("channels", [["inbox"], ["inbox", "webhook"]])
def test_ac_1_opt_in_delivery_inbox_mcp_and_outbox_unconsented_recorded(gateway, proposal_setup, receiver, channels):
    import asyncio
    from app.mcp_server import create_server
    delivered = []
    subscribe(gateway, event_types=["proposal.received"])
    gateway.outbox.register("proposal-webhook-test", lambda event: delivered.append(event) if event["type"] == "proposal.received" else None)
    with TestClient(create_app(gateway), base_url="http://localhost") as client:
        opt_in(client, channels=channels)
        result = submit(client, targets=["1", "2"])
        assert result["delivery_count"] == 1 and result["held_back_count"] == 1
        own = inbox(client)
        assert own[0]["customer_id"] == "1" and own[0]["products"] == ["1"]
        assert client.get("/v1/proposals/inbox", headers=agent_headers("agent-b")).json() == []
        tool = asyncio.run(create_server(gateway, "external").call_tool("list_proposals", {}))
        assert not tool.is_error and own[0]["id"] in str(tool)
        # The shared HTTP MCP endpoint resolves the caller on every request.
        # Proposal reads must use that identity for both scope and throttling.
        for agent, expected in (("external", own), ("agent-b", [])):
            response = client.post("/mcp", headers={**agent_headers(agent),
                "Accept": "application/json, text/event-stream"}, json={
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "list_proposals", "arguments": {}}})
            assert response.status_code == 200, response.text
            result = response.json()["result"]
            assert not result.get("isError", False)
            assert result["structuredContent"]["result"] == expected
            assert (agent, "read") in gateway.limiter.buckets
        assert (None, "read") not in gateway.limiter.buckets
    assert len(delivered) == 1 and delivered[0]["customer_id"] == "1"
    assert delivered[0]["aggregate_id"] == own[0]["id"]
    assert delivered[0]["payload"]["channels"] == channels
    assert len(receiver["calls"]) == int("webhook" in channels)
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
        partial = gateway.policy._editable(gateway.policy.get())
        partial.pop("proposal_quotas")
        gateway.policy.update(PolicyUpdateRequest(policy=partial, updated_by="Unrelated policy change"))
        assert gateway.policy.get().proposal_quotas.model_dump() == body["proposal_quotas"]
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
        # Expiry bypasses the active-campaign merge path: only mute can block this.
        with gateway.db.connect(write=True) as conn:
            campaign_row = json.loads(conn.execute("SELECT data FROM proposal_campaigns WHERE id=?", (original["id"],)).fetchone()[0])
            campaign_row["valid_until"] = (audit.utcnow() - timedelta(seconds=1)).isoformat()
            conn.execute("UPDATE proposal_campaigns SET data=? WHERE id=?", (json.dumps(campaign_row), original["id"]))
        assert submit(client, name="milk", price=14)["delivery_count"] == 0
        with gateway.db.connect() as conn:
            assert conn.execute("SELECT reason FROM proposal_filter_log ORDER BY rowid DESC LIMIT 1").fetchone()[0] == "muted campaign"
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


@pytest.mark.parametrize("channels", [["inbox"], ["webhook"], ["inbox", "webhook"]])
def test_review_proposal_webhook_requires_channel_consent(gateway, proposal_setup, receiver, channels):
    subscribe(gateway, event_types=["proposal.received"], principal=Principal("operator", operator=True, kind="operator", role="valio_operator"))
    with TestClient(create_app(gateway)) as client:
        opt_in(client, channels=channels)
        submit(client)
    assert len(receiver["calls"]) == int("webhook" in channels)


def test_review_proposal_retry_rechecks_current_webhook_consent(gateway, proposal_setup, receiver):
    subscribe(gateway, event_types=["proposal.received"], principal=Principal("operator", operator=True, kind="operator", role="valio_operator"))
    receiver["status"] = 503
    with TestClient(create_app(gateway)) as client:
        opt_in(client)
        submit(client)
        assert len(receiver["calls"]) == 1
        opt_in(client, channels=["inbox"])
        receiver["status"] = 200
        due(gateway)
    assert len(receiver["calls"]) == 1


@pytest.mark.parametrize("renamed", [False, True])
def test_review_campaign_merge_delivers_to_new_targets(gateway, proposal_setup, renamed):
    with TestClient(create_app(gateway)) as client:
        opt_in(client)
        client.put("/v1/buyer/proposal-preferences", json={"categories": ["10400"]}, headers=agent_headers("agent-b")).raise_for_status()
        first = submit(client)
        second = submit(client, name="renamed" if renamed else "milk", targets=["2"])
        assert first["id"] == second["id"] and second["merged"] is True
        assert second["delivery_count"] == 2 and second["new_delivery_count"] == 1
        assert len(client.get("/v1/proposals/inbox", headers=agent_headers("agent-b")).json()) == 1
        assert submit(client, targets=["1", "2"])["new_delivery_count"] == 0


def test_review_merged_campaign_rechecks_previously_held_customer(gateway, proposal_setup):
    with TestClient(create_app(gateway)) as client:
        opt_in(client)
        original = submit(client, targets=["1", "2"])
        client.put("/v1/buyer/proposal-preferences", json={"categories": ["10400"]}, headers=agent_headers("agent-b")).raise_for_status()
        merged = submit(client, targets=["2"])
        assert merged["id"] == original["id"]
        assert merged["new_delivery_count"] == 1


@pytest.mark.parametrize("action", ["decline", "mute"])
def test_review_proposal_actions_forward_signed_mandate(gateway, proposal_setup, vc_issuer, action):
    with TestClient(create_app(gateway)) as client:
        opt_in(client)
        submit(client)
        token = issue(vc_issuer)
        headers = {**agent_headers("outsider"), "Agent-Mandate": token}
        own = client.get("/v1/proposals/inbox", headers=headers)
        assert own.status_code == 200 and own.json()
        response = client.post(f"/v1/proposals/{own.json()[0]['id']}/{action}", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["proposal"]["status"] == ("declined" if action == "decline" else "muted")


def test_review_proposal_accept_includes_advertised_sku_before_truncation(gateway, need, upstream, proposal_setup):
    upstream.prices["4"] = 1
    upstream.fits["4"] = .99
    need.max_candidates = 1
    with TestClient(create_app(gateway)) as client:
        opt_in(client)
        submit(client)
        id = inbox(client)[0]["id"]
        response = client.post(f"/v1/proposals/{id}/accept", json={"need": need.model_dump(mode="json")}, headers=agent_headers("external"))
        assert response.status_code == 200, response.text
        assert response.json()["order"]["lines"][0]["product_id"] == "1"


def test_review_duplicate_targets_are_bounded_without_repeat_log_writes(gateway, proposal_setup):
    from app.proposals import ProposalRequest
    from pydantic import ValidationError
    with TestClient(create_app(gateway)) as client:
        opt_in(client)
        first = submit(client)
        targets = [f"unknown-{i}" for i in range(20)]
        submit(client, targets=targets)
        with gateway.db.connect() as conn:
            before = conn.execute("SELECT count(*) FROM proposal_filter_log").fetchone()[0]
        submit(client, targets=targets)
        with gateway.db.connect() as conn:
            assert conn.execute("SELECT count(*) FROM proposal_filter_log").fetchone()[0] == before
        with pytest.raises(ValidationError):
            ProposalRequest.model_validate(campaign(targets=[f"unknown-{i}" for i in range(1000)]))


@pytest.mark.parametrize("action", ["accept", "decline", "mute"])
def test_review_proposal_decisions_are_keyed_commands(gateway, need, upstream, proposal_setup, action):
    with TestClient(create_app(gateway)) as client:
        opt_in(client)
        submit(client)
        id = inbox(client)[0]["id"]
        headers = {**agent_headers("external"), "Idempotency-Key": "proposal-decision"}
        payload = {"need": need.model_dump(mode="json")} if action == "accept" else None
        first = client.post(f"/v1/proposals/{id}/{action}", json=payload, headers=headers)
        again = client.post(f"/v1/proposals/{id}/{action}", json=payload, headers=headers)
        assert first.status_code == again.status_code == 200
        assert first.json() == again.json()
        assert sum(e.action == "PROPOSAL_ACTION" for e in gateway.audit()) == 1
        with gateway.db.connect() as conn:
            assert conn.execute("SELECT count(*) FROM commands WHERE key='proposal-decision'").fetchone()[0] == 1


def test_review_proposal_quote_rechecks_revoked_buyer_consent(gateway, need, upstream, proposal_setup):
    with TestClient(create_app(gateway)) as client:
        opt_in(client)
        submit(client)
        id = inbox(client)[0]["id"]
        upstream.price = 14
        response = client.post(f"/v1/proposals/{id}/accept", json={"need": need.model_dump(mode="json")}, headers=agent_headers("external"))
        assert response.json()["status"] == "REQUOTE_REQUIRED"
        q = response.json()["quote"]
        opt_in(client, categories=[], interests=[], channels=[])
        placed = gateway.orders.place_order(q["id"], "revoked-proposal", "external")
        assert placed.status == "REJECTED"
        assert any("consent" in h.detail for h in placed.policy.hits)


def test_review_proposal_writes_payload_bound_keys(gateway, proposal_setup):
    with TestClient(create_app(gateway)) as client:
        prefs = {"categories": ["10400"]}
        headers = {**agent_headers("hotel-agent"), "Idempotency-Key": "preferences"}
        first = client.put("/v1/buyer/proposal-preferences", json=prefs, headers=headers)
        assert first.status_code == 200
        assert client.put("/v1/buyer/proposal-preferences", json=prefs, headers=headers).json() == first.json()
        assert client.put("/v1/buyer/proposal-preferences", json={"categories": []}, headers=headers).status_code == 409
        body = campaign()
        headers = {**agent_headers("other"), "Idempotency-Key": "campaign"}
        first = client.post("/v1/proposals", json=body, headers=headers)
        assert first.status_code == 200
        assert client.post("/v1/proposals", json=body, headers=headers).json() == first.json()
        assert client.post("/v1/proposals", json={**body, "rationale": "changed"}, headers=headers).status_code == 409
        row = inbox(client)[0]
        for action in ["decline", "mute"]:
            headers = {**agent_headers("external"), "Idempotency-Key": action}
            url = f"/v1/proposals/{row['id']}/{action}"
            first = client.post(url, headers=headers)
            assert first.status_code == 200
            assert client.post(url, headers=headers).json() == first.json()


@pytest.mark.parametrize("keyed", [False, True])
def test_review_proposal_preferences_charge_once_per_command(gateway, proposal_setup, monkeypatch, keyed):
    from unittest.mock import Mock

    consume = Mock(wraps=gateway.limiter.consume)
    monkeypatch.setattr(gateway.limiter, "consume", consume)
    headers = agent_headers("hotel-agent")
    if keyed:
        headers["Idempotency-Key"] = "single-preference-charge"
    with TestClient(create_app(gateway)) as client:
        response = client.put("/v1/buyer/proposal-preferences", json={"categories": ["10400"]}, headers=headers)
        assert response.status_code == 200, response.text
        assert [call.args for call in consume.call_args_list] == [("hotel-agent", "mutation")]
        if keyed:
            consume.reset_mock()
            replay = client.put("/v1/buyer/proposal-preferences", json={"categories": ["10400"]}, headers=headers)
            assert replay.json() == response.json()
            assert [call.args for call in consume.call_args_list] == [("hotel-agent", "read")]
