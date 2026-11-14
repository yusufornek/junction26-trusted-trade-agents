"""Spec 40: one offline integration test per acceptance criterion."""
from datetime import timedelta
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app import audit
from app.api import create_app
from app.db import DomainError
from app.mandates import sign_jwt
from app.models import PolicyUpdateRequest
from app.signing import b64
from conftest import OPERATOR_HEADERS, agent_headers, quote_request, replay_recorded_demo

AGENT = "external"


def update(gateway, **sections):
    body = gateway.policy._editable(gateway.policy.get())
    body.update(sections)
    return gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Test operator"))


def approval_first(gateway):
    agents = gateway.policy._editable(gateway.policy.get())["agents"]
    agents[AGENT].pop("autonomy", None)
    update(gateway, agents=agents)


def place(gateway, need, *, product_id="1", agent=AGENT, mandate=None):
    lookup = gateway.find_offers(need, f"agent:{agent}", mandate=mandate)
    quote = gateway.orders.create_quote(quote_request(need, qty=10, product_id=product_id,
        need_ref=lookup.lookup_id), agent, mandate=mandate)
    return gateway.orders.place_order(quote.id, str(uuid4()), agent, mandate=mandate)


def change(client, level="autonomous_within_limits", *, headers=OPERATOR_HEADERS, reason="Pilot reviewed"):
    return client.post(f"/v1/agents/{AGENT}/autonomy", json={"level": level, "reason": reason}, headers=headers)


def record(client, *, headers=OPERATOR_HEADERS, days=30):
    response = client.get(f"/v1/agents/{AGENT}/track-record", params={"window_days": days}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def organisations(gateway):
    update(gateway, organisations={"hotel": {"customer_ids": ["1"]}, "other": {"customer_ids": ["2"]}},
        identities={AGENT: {"organisation_id": "hotel"},
            "hotel-agent": {"kind": "buyer_user", "role": "buyer_admin", "organisation_id": "hotel"},
            "buyer": {"kind": "buyer_user", "role": "buyer_approver", "organisation_id": "hotel"},
            "observer": {"kind": "buyer_user", "role": "buyer_viewer", "organisation_id": "hotel"},
            "other": {"kind": "buyer_user", "role": "buyer_admin", "organisation_id": "other"}})


def test_ac_1_registered_agent_defaults_to_approval_required(gateway, need, upstream):
    approval_first(gateway)
    assert gateway.policy.get().autonomy.default == "approval_required"
    order = place(gateway, need)
    assert order.total == 130 and order.status == "PENDING_APPROVAL"
    assert {h.rule for h in order.policy.hits} == {"autonomy_level"}
    assert "sign-off until autonomy is granted" in order.policy.hits[0].detail
    with TestClient(create_app(gateway)) as client:
        assert record(client, headers=agent_headers(AGENT))["orders_placed"] == 1
        assert not record(client)["autonomy_recommendation"]["eligible"]
        assert client.get(f"/v1/agents/{AGENT}/track-record", headers=agent_headers("other")).status_code == 404
        assert client.get(f"/v1/agents/{AGENT}/track-record", params={"window_days": 0}, headers=OPERATOR_HEADERS).status_code == 422
        assert client.get(f"/v1/agents/{AGENT}/track-record").status_code == 401
        assert change(client, reason="   ").status_code == 422


def test_ac_2_promotion_confirms_and_audits_reason_and_snapshot(gateway, need, upstream):
    approval_first(gateway)
    first = place(gateway, need)
    with TestClient(create_app(gateway)) as client:
        result = change(client)
        assert result.status_code == 200, result.text
        snapshot = result.json()["track_record"]
        assert snapshot["orders_placed"] == 1
        assert not snapshot["autonomy_recommendation"]["eligible"]
        assert record(client)["orders_placed"] == 1
    entry = next(e for e in gateway.audit() if e.action == "AUTONOMY_CHANGED")
    assert entry.actor == "human:operator"
    assert entry.data["reason"] == "Pilot reviewed" and entry.data["track_record"] == snapshot
    assert entry.data["before"] == "approval_required" and entry.data["level"] == "autonomous_within_limits"
    second = place(gateway, need)
    assert second.total == first.total and second.status == "CONFIRMED"
    assert gateway.orders.get_order(first.id).status == "PENDING_APPROVAL"
    from app.policy import Policy
    assert Policy(gateway.policy.path, gateway.policy.runtime_path, gateway.db).get().agents[AGENT].autonomy == "autonomous_within_limits"


def test_ac_3_window_record_counts_outcomes_changes_and_approval_time(gateway, need, upstream, monkeypatch):
    approval_first(gateway)
    clock = [audit.utcnow()]
    monkeypatch.setattr(audit, "utcnow", lambda: clock[0])
    approved = place(gateway, need)
    clock[0] += timedelta(seconds=10)
    gateway.orders.approve(approved.id, "Ada")
    gateway.orders.cancel(approved.id, "human:operator", "Duplicate")
    gateway.orders.reject(place(gateway, need).id, "Ada")
    changed = place(gateway, need)
    upstream.shift = 1
    with pytest.raises(DomainError, match="EVIDENCE_CHANGED"):
        gateway.orders.approve(changed.id, "Ada")
    assert gateway.orders.approve(changed.id, "Ada", evidence_revision=2).status == "CONFIRMED"
    upstream.shift = 0
    expired = place(gateway, need)
    clock[0] = expired.expires_at + timedelta(seconds=1)
    gateway.orders.expire()
    assert place(gateway, need, product_id="2").status == "REJECTED"
    with TestClient(create_app(gateway)) as client:
        result = record(client)
        keys = ("orders_placed", "approved_unchanged", "rejected", "expired", "evidence_changes", "cancellations", "hard_constraint_refusals")
        assert [result[k] for k in keys] == [5, 1, 2, 1, 1, 1, 1]
        assert result["average_approval_time_seconds"] == 5
        assert result["approved_unchanged_share"] == 0.2 and result["rejection_share"] == 0.4
        assert result["last_rejection_at"] and result["last_hard_constraint_refusal_at"]
        clock[0] += timedelta(days=2)
        assert record(client, days=1)["orders_placed"] == 0
        assert record(client)["orders_placed"] == 5
        clock[0] += timedelta(days=31)
        empty = record(client)
        assert empty["orders_placed"] == empty["approved_unchanged"] == empty["hard_constraint_refusals"] == 0
        assert empty["average_approval_time_seconds"] is None
        assert empty["last_rejection_at"] == result["last_rejection_at"]


def test_ac_4_readiness_requires_every_threshold_and_cooldown(gateway, need, upstream, monkeypatch):
    approval_first(gateway)
    clock = [audit.utcnow()]
    monkeypatch.setattr(audit, "utcnow", lambda: clock[0])
    update(gateway, autonomy={"default": "approval_required", "promotion": {
        "min_approved_unchanged": 5, "max_rejection_share": 0.1, "days_since_last_rejection_or_refusal": 7}})
    with TestClient(create_app(gateway)) as client:
        recommendation = record(client)["autonomy_recommendation"]
        assert not recommendation["eligible"] and "minimum 5" in recommendation["reasons"][0]
        for _ in range(5):
            gateway.orders.approve(place(gateway, need).id, "Ada")
        assert record(client)["autonomy_recommendation"]["eligible"]
        gateway.orders.reject(place(gateway, need).id, "Ada")
        rec = record(client)["autonomy_recommendation"]
        assert not rec["eligible"] and len(rec["reasons"]) == 2
        assert any("share" in r for r in rec["reasons"]) and any("7 days" in r for r in rec["reasons"])
        for _ in range(4):
            gateway.orders.approve(place(gateway, need).id, "Ada")
        assert record(client)["rejection_share"] == 0.1
        assert len(record(client)["autonomy_recommendation"]["reasons"]) == 1
        clock[0] += timedelta(days=7)
        assert record(client)["autonomy_recommendation"]["eligible"]
        upstream.cutoff = clock[0] + timedelta(hours=1)
        gateway.valio.cache.clear()
        place(gateway, need, product_id="2")
        assert any("hard-constraint refusal" in r for r in record(client)["autonomy_recommendation"]["reasons"])
        assert gateway.policy.get().agents[AGENT].autonomy is None


def test_ac_5_rejection_emits_durable_review_event_and_demotion_is_immediate(gateway, need, upstream):
    approval_first(gateway)
    delivered = []
    gateway.outbox.register("autonomy-test", delivered.append)
    with TestClient(create_app(gateway)) as client:
        assert change(client).status_code == 200
        upstream.shift = 1
        order = place(gateway, need)
        assert order.status == "PENDING_APPROVAL"
        response = client.post(f"/v1/approvals/{order.id}/reject", json={"approver": "Ada"}, headers=OPERATOR_HEADERS)
        assert response.status_code == 200 and response.json()["approval_side"] == "valio_operator"
        events = [e for e in delivered if e["type"] == "autonomy.review_suggested"]
        assert len(events) == 1 and events[0]["payload"]["agent_id"] == AGENT
        assert events[0]["customer_id"] == "1" and events[0]["payload"]["reason"] == "human_rejection"
        upstream.shift = 0
        assert place(gateway, need, product_id="2").status == "REJECTED"
        assert len([e for e in delivered if e["type"] == "autonomy.review_suggested"]) == 2
        upstream.shift = 1
        blocked_review = place(gateway, need)
        update(gateway, customers={"1": {"exclude_allergens": ["AM"]}})
        refused = client.post(f"/v1/approvals/{blocked_review.id}/approve", json={"approver": "Ada"}, headers=OPERATOR_HEADERS)
        assert refused.status_code == 409 and refused.json()["code"] == "HARD_CONSTRAINT_VIOLATION"
        assert len([e for e in delivered if e["type"] == "autonomy.review_suggested"]) == 3
        assert record(client)["hard_constraint_refusals"] == 2
        update(gateway, customers={})
        upstream.shift = 0
        assert change(client, "approval_required", reason="Review after rejection").status_code == 200
        pending = place(gateway, need)
        assert pending.status == "PENDING_APPROVAL" and any(h.rule == "autonomy_level" for h in pending.policy.hits)
        with gateway.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM outbox WHERE consumer='autonomy-test' AND status='pending'").fetchone()[0] == 0


def test_ac_6_stricter_verifiable_mandate_wins_and_missing_claim_requires_approval(gateway, need, upstream):
    key = Ed25519PrivateKey.generate()
    update(gateway, customers={"1": {"mandate_issuers": {"urn:buyer:test": [{"kid": "test", "kty": "OKP", "crv": "Ed25519",
        "x": b64(key.public_key().public_bytes_raw())}]}}})
    with TestClient(create_app(gateway)) as client:
        assert change(client).status_code == 200
    now = int(audit.utcnow().timestamp())
    def token(level, agent=AGENT):
        subject = {"agent_id": agent, "customer_id": "1", "max_auto_total_eur": 1500, "max_auto_daily_total_eur": 5000}
        if level is not None:
            subject["autonomy"] = level
        return sign_jwt(key, {"iss": "urn:buyer:test", "sub": agent, "jti": str(uuid4()), "nbf": now-1, "exp": now+86400,
            "vc": {"@context": ["https://www.w3.org/ns/credentials/v2"], "type": ["VerifiableCredential", "PurchasingMandate"],
                   "credentialSubject": subject}}, kid="test")
    for level in ("approval_required", None):
        order = place(gateway, need, mandate=token(level))
        assert order.status == "PENDING_APPROVAL" and order.mandate.autonomy == "approval_required"
        assert any(h.rule == "autonomy_level" for h in order.policy.hits)
    assert place(gateway, need, mandate=token("autonomous_within_limits")).status == "CONFIRMED"
    with TestClient(create_app(gateway)) as client:
        assert change(client, "approval_required").status_code == 200
    assert place(gateway, need, mandate=token("autonomous_within_limits")).status == "PENDING_APPROVAL"
    with pytest.raises(DomainError, match="MANDATE_INVALID"):
        place(gateway, need, mandate=token("invalid"))
    # Changing a VC-only agent's level must not create permanent purchasing authority.
    outsider = token("autonomous_within_limits", "outsider")
    assert place(gateway, need, agent="outsider", mandate=outsider).status == "CONFIRMED"
    with TestClient(create_app(gateway)) as client:
        for level, expected in (("approval_required", "PENDING_APPROVAL"), ("autonomous_within_limits", "CONFIRMED")):
            response = client.post("/v1/agents/outsider/autonomy", headers=OPERATOR_HEADERS,
                json={"level": level, "reason": "Review VC-only agent"})
            assert response.status_code == 200, response.text
            assert place(gateway, need, agent="outsider", mandate=outsider).status == expected
    assert "outsider" not in gateway.policy.get().agents
    with pytest.raises(DomainError, match="AGENT_NOT_REGISTERED"):
        place(gateway, need, agent="outsider")


def test_ac_7_foreign_admin_hidden_own_buyer_admin_and_approver_scoped(gateway, need, upstream):
    approval_first(gateway)
    organisations(gateway)
    # Shared customer codes do not override an explicit organisation boundary.
    update(gateway, organisations={"hotel": {"customer_ids": ["1"]}, "other": {"customer_ids": ["1"]}})
    pending = place(gateway, need)
    with TestClient(create_app(gateway)) as client:
        for reader in ("hotel-agent", "buyer", "observer"):
            assert record(client, headers=agent_headers(reader))["orders_placed"] == 1
        assert change(client, headers=agent_headers("other")).status_code == 404
        assert client.get(f"/v1/agents/{AGENT}/track-record", headers=agent_headers("other")).status_code == 404
        assert change(client, headers=agent_headers("buyer")).status_code == 403
        assert change(client, headers=agent_headers(AGENT)).status_code in (403, 404)
        assert client.post(f"/v1/approvals/{pending.id}/approve", json={"approver": "Foreign admin"},
            headers=agent_headers("other")).status_code == 404
        inbox = client.get("/v1/approvals", headers=agent_headers("buyer"))
        assert inbox.status_code == 200 and [i["order"]["id"] for i in inbox.json()] == [pending.id]
        assert client.get("/v1/approvals", headers=agent_headers("other")).json() == []
        signed = client.post(f"/v1/approvals/{pending.id}/approve", json={"approver": "Buyer manager"}, headers=agent_headers("buyer"))
        assert signed.status_code == 200, signed.text
        assert signed.json()["status"] == "CONFIRMED" and signed.json()["approval_side"] == "buyer"
        assert gateway.audit(order_id=pending.id)[-1].actor == "human:buyer"
        assert change(client, headers=agent_headers("hotel-agent")).status_code == 200
        assert place(gateway, need).status == "CONFIRMED"
        assert len([e for e in gateway.audit() if e.action == "AUTONOMY_CHANGED"]) == 1


def test_ac_8_demo_mandates_keep_1010_eur_confirmation(gateway, need, respx_mock):
    for agent in ("hotel-chain-agent", "web-console"):
        mandate = gateway.policy.get().agents[agent]
        assert mandate.autonomy == "autonomous_within_limits"
        assert mandate.note == "autonomy granted after the pilot's approved orders (demo setting)"
    result = replay_recorded_demo(gateway, need, respx_mock)
    quote = gateway.orders.create_quote(quote_request(result.need, qty=1000, product_id="464306", need_ref=result.lookup_id),
        "hotel-chain-agent", operator=True)
    order = gateway.orders.place_order(quote.id, "hotel-demo", "hotel-chain-agent")
    assert order.status == "CONFIRMED" and order.total == 1010
