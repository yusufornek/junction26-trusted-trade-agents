"""Spec 40: one offline integration test per acceptance criterion."""
import json
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


@pytest.mark.parametrize("scope_change", ["organisation", "customer", "deleted", "role"])
def test_review_autonomy_replay_rechecks_target_scope(gateway, need, upstream, scope_change):
    organisations(gateway)
    place(gateway, need)
    body = {"level": "approval_required", "reason": "Pilot review"}
    headers = {**agent_headers("hotel-agent"), "Idempotency-Key": "scope"}
    url = f"/v1/agents/{AGENT}/autonomy"
    with TestClient(create_app(gateway)) as client:
        first = client.post(url, json=body, headers=headers)
        assert first.status_code == 200 and first.json()["track_record"]["orders_placed"] == 1
        policy = gateway.policy._editable(gateway.policy.get())
        if scope_change == "organisation":
            policy["identities"][AGENT]["organisation_id"] = "other"
        elif scope_change == "customer":
            policy["agents"][AGENT]["customer_ids"] = ["2"]
        elif scope_change == "deleted":
            del policy["agents"][AGENT]
        else:
            policy["identities"]["hotel-agent"]["role"] = "buyer_viewer"
        update(gateway, **policy)
        fresh = client.post(url, json=body, headers={**headers, "Idempotency-Key": "fresh"})
        replay = client.post(url, json=body, headers=headers)
        assert fresh.status_code == (403 if scope_change == "role" else 404)
        assert (replay.status_code, replay.json()["code"]) == (fresh.status_code, fresh.json()["code"])
        assert "track_record" not in replay.json()
    assert len([e for e in gateway.audit() if e.action == "AUTONOMY_CHANGED"]) == 1


def test_review_autonomy_key_binds_target(gateway):
    body = {"level": "approval_required", "reason": "Review required"}
    headers = {**OPERATOR_HEADERS, "Idempotency-Key": "target"}
    with TestClient(create_app(gateway)) as client:
        first = client.post(f"/v1/agents/{AGENT}/autonomy", json=body, headers=headers)
        assert first.status_code == 200
        assert client.post(f"/v1/agents/{AGENT}/autonomy", json=body, headers=headers).json() == first.json()
        other = client.post("/v1/agents/hotel-chain-agent/autonomy", json=body, headers=headers)
        assert other.status_code == 409 and other.json()["code"] == "IDEMPOTENCY_CONFLICT"
        assert gateway.policy.get().agents["hotel-chain-agent"].autonomy == "autonomous_within_limits"


def test_review_failed_autonomy_command_keeps_approval_required(gateway, need, upstream):
    approval_first(gateway)
    before = gateway.policy.runtime_path.read_bytes()
    with gateway.db.connect(write=True) as conn:
        conn.execute("CREATE TRIGGER fail_command BEFORE INSERT ON commands "
                     "WHEN NEW.command='change' BEGIN SELECT RAISE(ABORT,'command failed'); END")
    with TestClient(create_app(gateway), raise_server_exceptions=False) as client:
        response = client.post(f"/v1/agents/{AGENT}/autonomy",
            json={"level": "autonomous_within_limits", "reason": "Pilot review"},
            headers={**OPERATOR_HEADERS, "Idempotency-Key": "failed-promotion"})
    assert response.status_code == 500
    assert gateway.policy.runtime_path.read_bytes() == before
    assert gateway.policy.get().agents[AGENT].autonomy is None
    assert not [e for e in gateway.audit() if e.action == "AUTONOMY_CHANGED"]
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 0
    assert place(gateway, need).status == "PENDING_APPROVAL"


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
    assert gateway.governance.resolve(AGENT).fields["autonomy_level"].value == "approval_required"
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
        assert gateway.audit(order_id=pending.id)[-1].actor == "human:buyer_approver:agent:buyer:key:1"
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


@pytest.mark.parametrize("action", ["approve", "reject"])
@pytest.mark.parametrize("scope_change", ["organisation", "customers", "role"])
def test_review_cached_approval_rechecks_scope(gateway, need, upstream, action, scope_change):
    approval_first(gateway)
    organisations(gateway)
    pending = place(gateway, need)
    headers = {**agent_headers("buyer"), "Idempotency-Key": "buyer-review"}
    url = f"/v1/approvals/{pending.id}/{action}"
    body = {"approver": "Buyer manager"}
    with TestClient(create_app(gateway)) as client:
        first = client.post(url, json=body, headers=headers)
        assert first.status_code == 200, first.text
        calls = len(upstream.availability_calls)
        assert client.post(url, json=body, headers=headers).json() == first.json()
        assert len(upstream.availability_calls) == calls
        policy = gateway.policy._editable(gateway.policy.get())
        if scope_change == "organisation":
            # Even overlapping customer codes must not cross the organisation boundary.
            policy["organisations"]["other"]["customer_ids"] = ["1"]
            policy["identities"]["buyer"]["organisation_id"] = "other"
        elif scope_change == "customers":
            policy["organisations"]["hotel"]["customer_ids"] = []
        else:
            policy["identities"]["buyer"]["role"] = "buyer_viewer"
        update(gateway, **policy)
        fresh = client.post(url, json=body, headers={**headers, "Idempotency-Key": "fresh"})
        replay = client.post(url, json=body, headers=headers)
        assert fresh.status_code == (403 if scope_change == "role" else 404)
        assert replay.status_code == fresh.status_code
        assert replay.json()["code"] == fresh.json()["code"]
        assert "lines" not in replay.json()
    assert len([e for e in gateway.audit(order_id=pending.id) if e.action == action.upper()]) == 1


@pytest.mark.parametrize("signed_level,invalid_authority", [
    ("autonomous_within_limits", None), ("approval_required", None), (None, None),
    ("autonomous_within_limits", "expired"), ("autonomous_within_limits", "revoked")])
@pytest.mark.parametrize("vc_only", [False, True])
def test_review_current_vc_autonomy_after_promotion(gateway, need, upstream, monkeypatch, signed_level, invalid_authority, vc_only):
    approval_first(gateway)
    key = Ed25519PrivateKey.generate()
    update(gateway, customers={"1": {"mandate_issuers": {"urn:buyer:review": [{"kid": "review", "kty": "OKP", "crv": "Ed25519",
        "x": b64(key.public_key().public_bytes_raw())}]}}})
    agent = "outsider" if vc_only else AGENT
    subject = {"agent_id": agent, "customer_id": "1", "max_auto_total_eur": 1500, "max_auto_daily_total_eur": 5000}
    if signed_level is not None:
        subject["autonomy"] = signed_level
    now = int(audit.utcnow().timestamp())
    mandate_id = str(uuid4())
    token = sign_jwt(key, {"iss": "urn:buyer:review", "sub": agent, "jti": mandate_id, "nbf": now-1, "exp": now+86400,
        "vc": {"@context": ["https://www.w3.org/ns/credentials/v2"], "type": ["VerifiableCredential", "PurchasingMandate"],
               "credentialSubject": subject}}, kid="review")
    delivered = []
    gateway.outbox.register("review-test", delivered.append)
    with TestClient(create_app(gateway)) as client:
        url = f"/v1/agents/{agent}/autonomy"
        if vc_only:
            # Register the credential through a successful lookup, then tighten its grant.
            gateway.find_offers(need, f"agent:{agent}", mandate=token)
            assert client.post(url, headers=OPERATOR_HEADERS,
                json={"level": "approval_required", "reason": "Pilot starts"}).status_code == 200
        pending = place(gateway, need, agent=agent, mandate=token)
        assert pending.status == "PENDING_APPROVAL" and pending.mandate.autonomy == "approval_required"
        assert client.post(url, headers=OPERATOR_HEADERS,
            json={"level": "autonomous_within_limits", "reason": "Pilot reviewed"}).status_code == 200
        with gateway.db.connect() as conn:
            _, current, _ = gateway.mandates.authorize(conn, None, agent, "1", bound=("order", pending.id))
        expected = signed_level == "autonomous_within_limits"
        assert (current.autonomy == "autonomous_within_limits") == expected
        if invalid_authority == "expired":
            # Keep the order's approval window open while its buyer credential expires.
            clock = audit.utcnow()
            monkeypatch.setattr(audit, "utcnow", lambda: clock + timedelta(days=1))
            with gateway.db.connect(write=True) as conn:
                order = gateway.orders._order(conn, pending.id)
                order.expires_at = clock + timedelta(days=2)
                gateway.orders._save_order(conn, order)
            expected = False
        elif invalid_authority == "revoked":
            revocation = sign_jwt(key, {"iss": "urn:buyer:review", "iat": now,
                "jti_revoked": [mandate_id]}, kid="review")
            gateway.mandates.revoke(revocation, "human:buyer")
            expected = False
        response = client.post(f"/v1/approvals/{pending.id}/reject", headers=OPERATOR_HEADERS,
            json={"approver": "Ada"})
        assert response.status_code == 200, response.text
    events = [e for e in delivered if e["type"] == "autonomy.review_suggested"]
    assert len(events) == int(expected)
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE type='autonomy.review_suggested'").fetchone()[0] == int(expected)


@pytest.mark.parametrize("reject_constraint", [False, True])
def test_review_accepted_constraint_exception_is_not_refusal(gateway, need, upstream, reject_constraint):
    approval_first(gateway)
    policy = gateway.policy._editable(gateway.policy.get())
    policy["reject"]["on_hard_constraint_violation"] = reject_constraint
    policy["autonomy"]["promotion"]["min_approved_unchanged"] = 0
    update(gateway, **policy)
    order = place(gateway, need, product_id="2")
    assert order.status == ("REJECTED" if reject_constraint else "PENDING_APPROVAL")
    if not reject_constraint:
        assert gateway.orders.approve(order.id, "Ada").status == "CONFIRMED"
    with TestClient(create_app(gateway)) as client:
        result = record(client)
    assert result["rejected"] == int(reject_constraint)
    assert result["hard_constraint_refusals"] == int(reject_constraint)
    assert bool(result["last_hard_constraint_refusal_at"]) == reject_constraint
    assert result["autonomy_recommendation"]["eligible"] != reject_constraint


def test_review_track_record_ignores_foreign_lifetime_history(gateway, monkeypatch):
    clock = [audit.utcnow() - timedelta(days=400)]
    monkeypatch.setattr(audit, "utcnow", lambda: clock[0])
    hit = {"rule": "hard_constraint_violation"}
    # Historical quote-only ownership and agent-only failures must retain lifetime dates.
    with gateway.db.connect(write=True) as conn:
        conn.execute("INSERT INTO quotes VALUES('own-old','ACCEPTED',?,?)",
            (clock[0].isoformat(), json.dumps({"agent_id": AGENT})))
        audit.append(conn, "human:old", "CREATE_QUOTE_FAILED", "Refused", quote_id="own-old", data={"rule_hits": [hit]})
        audit.append(conn, f"agent:{AGENT}", "PLACE_ORDER", "Rejected", data={"outcome": "REJECTED"})
    last = clock[0].isoformat()
    clock[0] += timedelta(days=400)

    def measured_record():
        steps, statements = [], []
        with gateway.db.connect() as conn:
            conn.set_progress_handler(lambda: steps.append(1) or 0, 1)
            conn.set_trace_callback(statements.append)
            result = gateway.autonomy._record(conn, AGENT, gateway.policy.get(), 1)
        return result, len(steps), statements

    before, baseline, _ = measured_record()
    clock[0] -= timedelta(days=400)
    with gateway.db.connect(write=True) as conn:
        for i in range(300):
            qid = f"foreign-{i}"
            conn.execute("INSERT INTO quotes VALUES(?,'ACCEPTED',?,?)",
                (qid, clock[0].isoformat(), json.dumps({"agent_id": "other"})))
            audit.append(conn, "agent:other", "CREATE_QUOTE_FAILED", "Foreign refusal", quote_id=qid,
                data={"rule_hits": [hit], "marker": "foreign-review-history"})
        # Simulate reopening a pre-fix database: indexes are added without rewriting history.
        for index in ("audit_actor_ts", "orders_agent_created", "quotes_agent"):
            conn.execute(f"DROP INDEX IF EXISTS {index}")
    from app.db import Database
    Database(gateway.db.path)
    assert audit.verify(gateway.db)["valid"]
    with gateway.db.connect() as conn:
        assert json.loads(conn.execute("SELECT data FROM quotes WHERE id='own-old'").fetchone()[0]) == {"agent_id": AGENT}
    clock[0] += timedelta(days=400)
    loads, decoded_foreign = json.loads, []

    def observe_loads(value, *args, **kwargs):
        if isinstance(value, str) and "foreign-review-history" in value:
            decoded_foreign.append(value)
        return loads(value, *args, **kwargs)

    monkeypatch.setattr(json, "loads", observe_loads)
    for _ in range(2):
        result, steps, statements = measured_record()
        assert result == before
        assert result.orders_placed == result.hard_constraint_refusals == 0
        assert result.last_rejection_at.isoformat() == last
        assert result.last_hard_constraint_refusal_at.isoformat() == last
        assert not any("SELECT data FROM quotes WHERE id='foreign-" in s for s in statements)
        assert not decoded_foreign
        # Indexed ownership lookups should not do work proportional to unrelated rows.
        assert steps <= baseline + 1000, (baseline, steps)
