"""Spec 45 profile slice: AC-1–4, shared surfaces and AC-10 demo regression.

AC-5–9 exercise the anomaly/forecast implementation in the separate lane.
All state lives in pytest's temporary database; no live demo storage is used.
"""
import asyncio
import json
import sqlite3

import pytest
import yaml
from fastapi.testclient import TestClient

from app import audit
from app.accounts import hash_password
from app.api import create_app
from app.db import Database, DomainError
from app.foundation import PRINCIPAL
from app.guardrail_presets import load_preset
from app.guardrail_profiles import record_policy_loosening
from conftest import agent_headers, demo_catalog

PASSWORD = "profile-test-password-45"


@pytest.fixture
def http(gateway):
    with gateway.db.connect(write=True) as conn:
        conn.execute("UPDATE users SET password_hash=?", (hash_password(PASSWORD),))
    with TestClient(create_app(gateway), base_url="http://localhost") as client:
        yield client


def sign(http, email):
    response = http.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    headers = {"Cookie": "vtg_session=" + response.cookies["vtg_session"], "X-CSRF-Token": response.json()["csrf_token"]}
    http.cookies.clear()
    return headers


def business(kind="restaurant_group", **extra):
    return {"business_type": kind, "sites": 1, "buyers_and_approvers": 3,
            "monthly_purchase_volume_band": "5–25k", "meals_per_day_band": "100–500", **extra}


def signup(http, kind="restaurant_group", **extra):
    response = http.post("/v1/auth/signup", json={"name": "Test kitchen", "email": kind + "@example.test",
        "password": PASSWORD, "profile": business(kind, **extra)})
    assert response.status_code == 201, response.text
    return response.json(), sign(http, kind + "@example.test")


def invite(http, admin, email, role):
    response = http.post("/v1/tenant/users", headers=admin, json={"email": email, "role": role})
    assert response.status_code == 201, response.text
    user = response.json()
    assert http.post(f'/v1/auth/invites/{user["token"]}/accept', json={"password": PASSWORD}).status_code == 200
    return user["user_id"], sign(http, email)


def connection(http, admin, tenant, agent="profile-agent"):
    response = http.post("/v1/tenant/connections", headers=admin, json={"agent_id": agent, "type": "rest",
        "client": "python", "audience": "buyer", "customer_ids": tenant["customer_ids"]})
    assert response.status_code == 201, response.text
    return {"Authorization": "Bearer " + response.json()["key"]}


def quote(http, agent, customer, need, *, units=50):
    body = need.model_dump(mode="json")
    body.update(customer_id=customer, quantity={"value": units * 5, "unit": "L"})
    lookup = http.post("/v1/offers", headers=agent, json=body)
    assert lookup.status_code == 200, lookup.text
    response = http.post("/v1/quotes", headers=agent, json={"customer_id": customer,
        "deliver_by": str(need.deliver_by), "need_ref": lookup.json()["lookup_id"],
        "lines": [{"product_id": "1", "quantity_sales_units": units}]})
    assert response.status_code == 200, response.text
    return response.json()


def place(http, agent, quoted, key="profile-order"):
    return http.post("/v1/orders", headers={**agent, "Idempotency-Key": key}, json={"quote_id": quoted["id"]})


def approve(http, human, order, *, key=None):
    url = f'/v1/tenant/approvals/{order["id"]}/approve'
    headers = {**human, **({"Idempotency-Key": key} if key else {})}
    body = {"approver": "Test reviewer", "evidence_revision": order["evidence_revision"]}
    preview = http.post(url, headers=headers, json=body)
    if preview.status_code != 200:
        return preview
    assert preview.json()["confirmation_required"]
    return http.post(url, headers=headers, json={**body, "confirmation_token": preview.json()["confirmation_token"]})


def update(http, admin, profile):
    preview = http.put("/v1/tenant/profile", headers=admin, json=profile)
    assert preview.status_code == 200, preview.text
    assert preview.json()["confirmation_required"]
    return http.put("/v1/tenant/profile", headers=admin, json={**profile, "confirmation_token": preview.json()["confirmation_token"]})


def test_AC_1_hospital_signup_and_constraints_required_before_order(http, gateway, need, upstream):
    tenant, admin = signup(http, "hospital_or_school_kitchen")
    snapshot = tenant["guardrail_profile"]
    assert tenant["user"]["role"] == "tenant_admin"
    assert snapshot["guardrails"]["four_eyes"] == {"value": True, "source": "preset:hospital_or_school_kitchen"}
    assert snapshot["profile"]["sites"] == 1
    agent = connection(http, admin, tenant)
    quoted = quote(http, agent, tenant["customer_ids"][0], need, units=10)
    calls = len(upstream.availability_calls)
    response = place(http, agent, quoted)
    assert response.status_code == 409 and response.json()["code"] == "CONSTRAINTS_REQUIRED"
    assert set(response.json()["data"]["missing_fields"]) == {"required_claims", "exclude_allergens"}
    assert len(upstream.availability_calls) == calls
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders WHERE tenant_id=?", (tenant["tenant"]["id"],)).fetchone()[0] == 0
    assert update(http, admin, business("hospital_or_school_kitchen", required_claims=["FREE_FROM_LACTOSE"])).status_code == 200
    partial = place(http, agent, quoted, "partial-constraints")
    assert partial.status_code == 409 and partial.json()["data"]["missing_fields"] == ["exclude_allergens"]
    profile = business("hospital_or_school_kitchen", required_claims=["FREE_FROM_LACTOSE"], exclude_allergens=["AE"])
    saved = update(http, admin, profile)
    assert saved.status_code == 200 and saved.json()["profile_revision"] == 3
    placed = place(http, agent, quoted, "constraints-set")
    assert placed.status_code == 200 and placed.json()["status"] == "PENDING_APPROVAL"
    context = http.get("/v1/me", headers=agent).json()
    assert context["standing_constraints"][tenant["customer_ids"][0]]["exclude_allergens"] == ["AE"]
    assert context["guardrail_profile"]["profile"] == profile
    assert any(a.action == "PLACE_ORDER_FAILED" and a.data.get("error") == "CONSTRAINTS_REQUIRED" for a in gateway.audit())


@pytest.mark.parametrize("kind,ceiling,threshold,four_eyes,quantity_k,velocity_k,monthly", [
    ("single_restaurant", 500, 250, False, 3, 3, 25000),
    ("restaurant_group", 2500, 1000, True, 3, 3, 25000),
    ("hotel_chain", 5000, 2500, True, 3, 3, 25000),
    ("hospital_or_school_kitchen", 1500, 500, True, 2, 2, 25000),
    ("catering", 7500, 3000, True, 3, 2, 37500),
])
def test_AC_2_all_five_presets_have_sourced_values_and_scaled_budgets(http, gateway, kind, ceiling, threshold, four_eyes, quantity_k, velocity_k, monthly):
    choices = http.get("/v1/auth/signup/options")
    assert choices.status_code == 200
    assert len(choices.json()["business_types"]) == 5
    assert all(o["label"] for group in choices.json().values() for o in group)
    tenant, admin = signup(http, kind, sites=3)
    snapshot = tenant["guardrail_profile"]
    fields = snapshot["guardrails"]
    values = {k: v["value"] for k, v in fields.items()}
    assert all(v["source"] == "preset:" + kind for v in fields.values())
    assert values["per_order_ceiling_eur"] == ceiling
    assert values["approver_amount_threshold_eur"] == threshold
    assert values["four_eyes"] is four_eyes
    assert values["substitutes_policy"] == "approval_required"
    assert values["starting_autonomy"] == "approval_required"
    assert values["anomaly_sensitivity"] == {"quantity_k": quantity_k, "velocity_k": velocity_k, "price_increase_pct": 20}
    assert values["monthly_budget_eur"] == monthly
    assert values["daily_budget_eur"] == round(monthly / 30, 2)
    if kind in {"hotel_chain", "restaurant_group"}:
        assert values["per_site_budgets"]["sites"] == 3
        assert values["per_site_budgets"]["monthly_budget_eur"] * 3 <= monthly
    assert bool(snapshot["warnings"]) is (not four_eyes)
    for band, base in [("<5k", 5000), ("5–25k", 25000), ("25–100k", 100000), (">100k", 200000)]:
        assert load_preset(kind, band)["monthly_budget_eur"]["value"] == base * (1.5 if kind == "catering" else 1)
    # Loader results are independent, and initial versions survive a repeat migration.
    fields["anomaly_sensitivity"]["value"]["quantity_k"] = 999
    assert load_preset(kind, "5–25k")["anomaly_sensitivity"]["value"]["quantity_k"] == quantity_k
    Database(gateway.db.path)
    read = http.get("/v1/tenant/profile", headers=admin)
    assert read.status_code == 200 and read.json()["guardrails"]["anomaly_sensitivity"]["value"]["quantity_k"] == quantity_k
    tid = tenant["tenant"]["id"]
    assert gateway.policy.get().tenant_anomaly_settings[tid]["velocity_k"] == velocity_k
    with gateway.db.connect() as conn:
        stored = json.loads(conn.execute("SELECT data FROM tenant_profile_versions WHERE tenant_id=? AND version=1", (tid,)).fetchone()[0])
    assert stored["guardrails"] == read.json()["guardrails"]


def test_AC_3_approver_above_threshold_refused_independent_admin_approves(http, gateway, need, upstream):
    tenant, owner = signup(http)
    reviewer_id, reviewer = invite(http, owner, "reviewer@example.test", "approver")
    admin_id, admin = invite(http, owner, "independent-admin@example.test", "tenant_admin")
    agent = connection(http, owner, tenant)
    quoted = quote(http, agent, tenant["customer_ids"][0], need, units=200)
    order = place(http, agent, quoted).json()
    assert order["status"] == "PENDING_APPROVAL" and order["total"] == 2600
    calls = len(upstream.availability_calls)
    refused = approve(http, reviewer, order)
    assert refused.status_code == 403 and refused.json()["code"] == "APPROVAL_LIMIT"
    assert refused.json()["data"]["threshold_eur"] == 1000
    assert refused.json()["data"]["who_can_approve"] == [admin_id]
    assert len(upstream.availability_calls) == calls
    approved = approve(http, admin, order)
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "CONFIRMED"
    assert approved.json()["approver_user_id"] == admin_id
    assert not approved.json()["self_approved"]
    refused_audit = [a for a in gateway.audit() if a.order_id == order["id"] and a.data.get("error") == "APPROVAL_LIMIT"]
    assert len(refused_audit) == 1 and refused_audit[0].data["user_id"] == reviewer_id
    # Exactly at the threshold remains approver authority.
    upstream.price = 5
    boundary_quote = quote(http, agent, tenant["customer_ids"][0], need, units=200)
    boundary = place(http, agent, boundary_quote, "at-threshold").json()
    assert boundary["total"] == 1000
    allowed = approve(http, reviewer, boundary)
    assert allowed.status_code == 200 and allowed.json()["status"] == "CONFIRMED", allowed.text


def test_AC_4_four_eyes_creator_refused_independent_review_and_self_approval_audited(http, gateway, need, upstream):
    tenant, creator = signup(http)
    reviewer_id, reviewer = invite(http, creator, "reviewer@example.test", "approver")
    agent = connection(http, creator, tenant)
    quoted = quote(http, agent, tenant["customer_ids"][0], need)
    order = place(http, agent, quoted).json()
    inbox = http.get("/v1/tenant/approvals", headers=creator).json()
    assert inbox[0]["approval_requirements"]["eligible_approver_user_ids"] == [reviewer_id]
    assert inbox[0]["approval_requirements"]["four_eyes"]
    calls = len(upstream.availability_calls)
    refused = approve(http, creator, order)
    assert refused.status_code == 403 and refused.json()["code"] == "FOUR_EYES"
    assert refused.json()["data"]["who_can_approve"] == [reviewer_id]
    assert len(upstream.availability_calls) == calls
    allowed = approve(http, reviewer, order)
    assert allowed.status_code == 200 and allowed.json()["status"] == "CONFIRMED", allowed.text
    single, self_reviewer = signup(http, "single_restaurant")
    single_agent = connection(http, self_reviewer, single, "solo-agent")
    single_quote = quote(http, single_agent, single["customer_ids"][0], need, units=10)
    single_order = place(http, single_agent, single_quote, "self-approved").json()
    approved = approve(http, self_reviewer, single_order)
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "CONFIRMED" and approved.json()["self_approved"] is True
    entries = gateway.audit(single_order["id"])
    entry = next(a for a in entries if a.action == "APPROVE")
    assert entry.data["self_approved"] is True
    assert entry.data["approval_warnings"] == ["SELF_APPROVAL_ALLOWED"]
    assert any(a.data.get("error") == "FOUR_EYES" for a in gateway.audit(order["id"]))


def test_AC_10_legacy_hotel_demo_is_unchanged(http, gateway, need, upstream, tmp_path):
    demo_catalog(gateway, tmp_path, upstream)
    headers = agent_headers("hotel-chain-agent")
    lookup = http.post("/v1/offers", headers=headers, json=need.model_dump(mode="json")).json()
    assert (lookup["offers"][0]["product_id"], lookup["offers"][0]["purchase_total"]) == ("464306", 1010)
    quoted = http.post("/v1/quotes", headers=headers, json={"customer_id": "1", "deliver_by": str(need.deliver_by),
        "need_ref": lookup["lookup_id"], "lines": [{"product_id": "464306", "quantity_sales_units": 1000}]}).json()
    response = place(http, headers, quoted, "demo45")
    assert response.status_code == 200, response.text
    order = response.json()
    assert (order["status"], order["total"]) == ("CONFIRMED", 1010)
    assert not order["self_approved"]
    assert not any(hit["rule"] in {"QUANTITY_ANOMALY", "PRICE_ANOMALY", "VELOCITY_ANOMALY", "NEW_CATEGORY"} for hit in order["policy"]["hits"])


def test_R9_profile_surfaces_isolation_validation_and_edit_change_control(http, gateway):
    tenant, admin = signup(http, required_claims=["FREE_FROM_LACTOSE"], exclude_allergens=["AE"])
    tid = tenant["tenant"]["id"]
    _, viewer = invite(http, admin, "viewer@example.test", "viewer")
    assert http.put("/v1/tenant/profile", headers=viewer, json=business()).status_code == 403
    profile = http.get("/v1/tenant/profile", headers=viewer).json()
    assert profile["tenant_id"] == tid
    session = http.get("/v1/auth/session", headers=admin).json()
    assert session["tenant"]["guardrail_profile"] == profile
    assert http.get("/v1/tenant/policy", headers=admin).json()["guardrail_profile"] == profile
    me = http.get("/v1/me", headers=admin).json()
    assert me["guardrail_profile"] == profile
    other, other_admin = signup(http, "catering")
    other_profile = http.get("/v1/tenant/profile", headers=other_admin).json()
    assert other_profile["tenant_id"] == other["tenant"]["id"] != tid
    other_policy = http.get("/v1/tenant/policy", headers=other_admin).json()
    assert tid not in other_policy["tenant_anomaly_settings"]
    new = business("hotel_chain", sites=9, required_claims=["FREE_FROM_LACTOSE"], exclude_allergens=["AE"])
    saved = update(http, admin, new)
    assert saved.status_code == 200
    assert saved.json()["profile"]["sites"] == 9
    assert saved.json()["guardrails"] == profile["guardrails"]
    assert saved.json()["guardrail_version"] == 1
    assert saved.json()["profile_revision"] == 2
    refused = update(http, admin, business("hotel_chain", sites=9))
    assert refused.status_code == 409 and refused.json()["code"] == "GUARDRAIL_CHANGE_REQUIRED"
    with gateway.db.connect(write=True) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE tenant_profile_versions SET data='{}' WHERE tenant_id=?", (tid,))
    staff = sign(http, "staff@valio.example")
    visible = http.get("/v1/operator/tenants", headers=staff).json()
    assert next(t for t in visible if t["id"] == tid)["guardrail_profile"] == saved.json()
    duplicate = http.post("/v1/auth/signup", json={"name": "Duplicate", "email": "restaurant_group@example.test", "password": PASSWORD, "profile": business()})
    assert duplicate.status_code == 409
    invalid = http.post("/v1/auth/signup", json={"name": "Invalid", "email": "invalid@example.test", "password": PASSWORD,
        "profile": business(exclude_allergens=["not-a-code"])})
    assert invalid.status_code == 422
    invalid_size = http.post("/v1/auth/signup", json={"name": "Invalid", "email": "invalid@example.test", "password": PASSWORD, "profile": business(sites=0)})
    assert invalid_size.status_code == 422
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tenants WHERE name='Duplicate' OR name='Invalid'").fetchone()[0] == 0
        events = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM events WHERE type='tenant.profile_updated' AND tenant_id=?", (tid,))]
    assert events[-1]["guardrail_profile"] == saved.json()
    assert PASSWORD not in json.dumps([a.model_dump(mode="json") for a in gateway.audit()])
    agent = connection(http, admin, tenant)
    rpc = http.post("/mcp", headers={**agent, "Accept": "application/json, text/event-stream"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_agent_context", "arguments": {}}})
    assert rpc.status_code == 200, rpc.text
    assert rpc.json()["result"]["structuredContent"]["guardrail_profile"] == saved.json()
    # R-9 keeps the checked-in REST and MCP surfaces synchronized with the app.
    from app.config import BACKEND
    from app.developer_docs import generated, tool_schemas
    from app.mcp_server import create_server
    assert yaml.safe_load((BACKEND.parent / "contracts/openapi.yaml").read_text()) == http.get("/openapi.json").json()
    schemas = asyncio.run(tool_schemas(create_server(gateway)))
    assert json.loads((BACKEND / "static/tools.json").read_text()) == schemas
    assert (BACKEND / "static/developers.html").read_text() == generated(schemas)


def test_four_eyes_owner_last_autonomy_and_policy_approver_core_checks(http, gateway, need, upstream):
    tenant, creator = signup(http)
    reviewer_id, reviewer = invite(http, creator, "grant-author@example.test", "tenant_admin")
    another_id, another = invite(http, creator, "policy-author@example.test", "tenant_admin")
    agent = connection(http, creator, tenant)
    order = place(http, agent, quote(http, agent, tenant["customer_ids"][0], need)).json()
    p = gateway.accounts.session(reviewer["Cookie"].partition("=")[2])
    principal_token = PRINCIPAL.set(p)
    try:
        from app.models import AutonomyChange
        gateway.autonomy.change("profile-agent", AutonomyChange(level="autonomous_within_limits", reason="Explicit test grant"), p)
        with pytest.raises(DomainError, match="independent") as caught:
            gateway.orders.approve(order["id"], "Grant author")
        assert caught.value.code == "FOUR_EYES"
    finally:
        PRINCIPAL.reset(principal_token)
    with gateway.db.connect(write=True) as conn:
        record_policy_loosening(conn, "profile-agent", another_id, tenant["tenant"]["id"])
    refused = approve(http, another, order)
    assert refused.status_code == 403 and refused.json()["code"] == "FOUR_EYES"
    assert refused.json()["data"]["who_can_approve"] == []


def test_keyed_approval_replay_rechecks_new_four_eyes_gate(http, gateway, need, upstream):
    tenant, owner = signup(http, "single_restaurant")
    agent = connection(http, owner, tenant)
    order = place(http, agent, quote(http, agent, tenant["customer_ids"][0], need, units=10)).json()
    p = gateway.accounts.session(owner["Cookie"].partition("=")[2])
    token = PRINCIPAL.set(p)
    try:
        approved = gateway.orders.approve(order["id"], "Owner", command_key="approval-replay-45")
        assert approved.self_approved
        # Model the tenant layer tightening concurrently in the governance lane.
        with gateway.db.connect(write=True) as conn:
            row = conn.execute("SELECT data FROM tenant_guardrail_profiles WHERE tenant_id=?", (p.tenant_id,)).fetchone()
            snapshot = json.loads(row[0])
            snapshot["guardrails"]["four_eyes"]["value"] = True
            conn.execute("UPDATE tenant_guardrail_profiles SET data=? WHERE tenant_id=?", (json.dumps(snapshot), p.tenant_id))
        with pytest.raises(DomainError) as caught:
            gateway.orders.approve(order["id"], "Owner", command_key="approval-replay-45")
        assert caught.value.code == "FOUR_EYES"
    finally:
        PRINCIPAL.reset(token)


def test_profiled_order_core_approval_requires_authenticated_reviewer(http, gateway, need, upstream):
    # Spec 46 R-4 adds independent supplier refreshes in the background scheduler.
    # Isolate this request's zero-upstream-call assertion from those authorized scans.
    gateway.recurring.close()
    tenant, owner = signup(http, "single_restaurant")
    agent = connection(http, owner, tenant)
    order = place(http, agent, quote(http, agent, tenant["customer_ids"][0], need, units=10)).json()
    calls = len(upstream.availability_calls)
    token = PRINCIPAL.set(None)
    try:
        with pytest.raises(DomainError) as caught:
            gateway.orders.approve(order["id"], "Unverified display name")
        assert caught.value.code == "APPROVER_REQUIRED" and caught.value.status == 403
    finally:
        PRINCIPAL.reset(token)
    assert len(upstream.availability_calls) == calls


def test_AC_2_AC_7_profile_sensitivity_reaches_anomaly_checks(http, gateway, need, upstream, monkeypatch):
    from app import anomaly_service

    tenant, admin = signup(http, "catering")
    agent = connection(http, admin, tenant)
    seen = {}
    original_quantity = anomaly_service.quantity_check
    original_velocity = anomaly_service.velocity_check

    def quantity(points, observed, now, k, **kwargs):
        seen["quantity_k"] = k
        return original_quantity(points, observed, now, k, **kwargs)

    def velocity(times, observed, now, k, minimum):
        seen["velocity_k"] = k
        return original_velocity(times, observed, now, k, minimum)

    monkeypatch.setattr(anomaly_service, "quantity_check", quantity)
    monkeypatch.setattr(anomaly_service, "velocity_check", velocity)
    quote(http, agent, tenant["customer_ids"][0], need)
    assert seen == {"quantity_k": 3, "velocity_k": 2}
    assert gateway.policy.get().anomalies.sensitivity == "normal"
    assert gateway.policy.get().anomalies.sensitivity_k["normal"] == 3
