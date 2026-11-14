"""Spec 44: one acceptance test per AC, plus the shared comparison truth table."""
import asyncio
import json
import sqlite3
from datetime import timedelta
from uuid import uuid4

import pytest

from app import audit
from app.auth import Principal
from app.db import DomainError
from app.governance import classify_change, expand
from app.signing import verify_jws
from app.webhooks import verify_webhook
from conftest import OPERATOR_HEADERS, agent_headers, demo_catalog
from test_spec43_accounts_connections import http, sign, connect, bearer, PASSWORD
from test_webhooks import receiver

AGENT = "external"
ROOT = "/v1/tenant/agents/external"


@pytest.fixture
def people(http, gateway):
    admin = sign(http)
    with gateway.db.connect(write=True) as conn:
        password = conn.execute("SELECT password_hash FROM users WHERE id='aurora-director'").fetchone()[0]
        for uid, role in (("member-one", "member"), ("review-admin", "tenant_admin"), ("member-two", "member")):
            conn.execute("INSERT INTO users(id,tenant_id,email,role,password_hash,created_at) VALUES(?,?,?,?,?,?)",
                         (uid, "hotel-aurora", uid+"@aurora.example", role, password, audit.utcnow().isoformat()))
    member, reviewer = sign(http, "member-one@aurora.example"), sign(http, "review-admin@aurora.example")
    pa = gateway.accounts.session(admin["Cookie"].partition("=")[2])
    pm = gateway.accounts.session(member["Cookie"].partition("=")[2])
    pr = gateway.accounts.session(reviewer["Cookie"].partition("=")[2])
    gateway.governance.transfer(AGENT, "member-one", "Assign procurement operator", pa)
    return {"admin": admin, "member": member, "reviewer": reviewer, "pa": pa, "pm": pm, "pr": pr}


def decide(http, method, url, headers, body):
    first = getattr(http, method)(url, headers=headers, json=body)
    if first.status_code == 200 and first.json().get("confirmation_required"):
        first = getattr(http, method)(url, headers=headers, json={**body, "confirmation_token": first.json()["confirmation_token"]})
    return first


def change(http, people, **policy):
    result = decide(http, "patch", ROOT+"/policy", people["member"], {"policy": policy, "reason": "Procurement policy review"})
    assert result.status_code == 200, result.text
    return result.json()


def quote(http, need, qty=10, agent=AGENT):
    headers = agent_headers(agent)
    lookup = http.post("/v1/offers", headers=headers, json=need.model_dump(mode="json"))
    assert lookup.status_code == 200, lookup.text
    q = http.post("/v1/quotes", headers=headers, json={"customer_id": need.customer_id, "deliver_by": str(need.deliver_by),
        "need_ref": lookup.json()["lookup_id"], "lines": [{"product_id": "1", "quantity_sales_units": qty}]})
    assert q.status_code == 200, q.text
    return lookup.json(), q.json()


def order(http, q, agent=AGENT):
    result = http.post("/v1/orders", headers={**agent_headers(agent), "Idempotency-Key": uuid4().hex}, json={"quote_id": q["id"]})
    assert result.status_code == 200, result.text
    return result.json()


def rpc(http, headers, name, arguments, path="/mcp", **params):
    return http.post(path, headers={**headers, "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2026-07-28", "MCP-Method": "tools/call", "MCP-Name": name},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments,
            "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {"elicitation": {"form": {}}}}, **params}})


def test_AC_1_member_register_ownership_and_connections(http, gateway, people):
    # A standalone stdio process reopens this DB without the HTTP key registry.
    from app.config import Settings
    from app.service import Gateway
    from app.mcp_server import create_server
    standalone = Gateway(Settings(db_path=gateway.settings.db_path))
    try:
        result = asyncio.run(create_server(standalone).call_tool("search_products", {"query": "milk", "limit": 2}))
        assert not result.is_error, result
    finally:
        standalone.close()
    own = http.get("/v1/tenant/agents", headers=people["member"]).json()
    assert {a["agent_id"] for a in own} == {AGENT}
    assert own[0]["owner_user_id"] == "member-one"
    assert {"budget_use", "orders", "approvals_asked", "approvals_granted", "refusals_by_code", "last_activity", "clients", "connections", "autonomy_level"} <= own[0].keys()
    all_agents = http.get("/v1/tenant/agents", headers=people["admin"]).json()
    assert {AGENT, "hotel-chain-agent", "web-console"} <= {a["agent_id"] for a in all_agents}
    assert all(a["owner_user_id"] for a in all_agents)
    created = http.post("/v1/tenant/agents", headers=people["member"], json={"agent_id": "member-agent", "name": "Member's buyer"})
    assert created.status_code == 201, created.text
    assert next(a for a in http.get("/v1/tenant/agents", headers=people["member"]).json() if a["agent_id"] == "member-agent")["autonomy_level"] == "approval_required"
    with gateway.db.connect() as conn:
        event = conn.execute("SELECT payload FROM events WHERE type='governance.agent_registered' AND aggregate_id='member-agent'").fetchone()
    assert json.loads(event[0])["owner_user_id"] == "member-one"
    new = connect(http, people["member"], agent="member-agent")
    assert new["mandate"]["autonomy"] == "approval_required"
    management = connect(http, people["member"], agent="member-agent", audience="tenant")
    listed = rpc(http, bearer(management), "agent_governance", {"operation": "register", "arguments": {}}, "/mcp/tenant")
    assert {a["agent_id"] for a in listed.json()["result"]["structuredContent"]["items"]} == {AGENT, "member-agent"}
    assert http.get("/v1/tenant/agents", headers=people["member"], params={"owner_user_id": "member-one", "status": "active"}).json()
    assert http.patch("/v1/tenant/agents/hotel-chain-agent", headers=people["member"], json={"name": "no"}).status_code == 404
    assert http.post("/v1/tenant/connections", headers=people["member"], json={"agent_id": "hotel-chain-agent", "client": "python", "customer_ids": ["1"]}).status_code == 404
    assert http.get("/v1/tenant/agents", headers=sign(http, "finance@aurora.example")).status_code == 403


def test_AC_2_effective_three_layers_source_version_author(http, gateway, people):
    platform = Principal("valio-admin", operator=True, kind="platform_admin", user_id="valio-admin", role="platform_admin")
    gateway.governance.change_guardrails({"daily_budget_eur": 3000}, "Platform daily ceiling", platform, layer="platform")
    tenant = gateway.governance.change_guardrails({"daily_budget_eur": 800}, "Tenant daily ceiling", people["pa"])
    result = http.get(ROOT+"/policy/effective", headers=people["member"]).json()
    daily = result["fields"]["daily_budget_eur"]
    assert (daily["value"], daily["source_layer"], daily["version_id"], daily["author"]) == (800, "tenant", tenant["id"], "aurora-director")
    assert {s["source_layer"] for s in daily["sources"]} == {"platform", "tenant", "agent"}
    assert result["version_id"].startswith("eff_")
    assert change(http, people, daily_budget_eur=600)["status"] == "applied"
    assert http.get(ROOT+"/policy/effective", headers=people["member"]).json()["fields"]["daily_budget_eur"]["source_layer"] == "agent"
    # Platform limits also restrict tenant guardrail edits.
    error = decide(http, "put", "/v1/tenant/guardrails", people["admin"], {"policy": {"daily_budget_eur": 4000}, "reason": "Too high"})
    assert error.status_code == 422 and error.json()["data"]["source_layer"] == "platform"


def test_AC_3_owner_tightens_immediately_immutable_version(http, gateway, people):
    before = http.get(ROOT+"/policy", headers=people["member"]).json()
    applied = change(http, people, per_order_ceiling_eur=100)
    assert applied["status"] == "applied"
    version = applied["version"]
    assert version["author"] == "member-one" and version["reason"]
    assert version["diff"]["per_order_ceiling_eur"]["direction"] == "tightening"
    versions = http.get(ROOT+"/policy/versions", headers=people["member"]).json()
    assert {before["id"], version["id"]} <= {v["id"] for v in versions}
    assert http.get(ROOT+"/policy/versions/"+version["id"], headers=people["member"]).json()["data"]["per_order_ceiling_eur"] == 100
    with gateway.db.connect(write=True) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE governance_versions SET reason='rewrite' WHERE id=?", (version["id"],))
    assert change(http, people, autonomy_level="approval_required")["status"] == "applied"
    assert any(e.action == "AUTONOMY_CHANGED" and "track_record" in e.data for e in gateway.audit())


def test_AC_4_loosening_review_and_next_quote_every_surface(http, gateway, people, need, upstream):
    from app.config import BACKEND
    from app.developer_docs import generated, tool_schemas
    from app.mcp_server import create_server
    schemas = asyncio.run(tool_schemas(create_server(gateway)))
    assert json.loads((BACKEND / "static/tools.json").read_text()) == schemas
    assert (BACKEND / "static/developers.html").read_text() == generated(schemas)
    change(http, people, per_order_ceiling_eur=100)
    pending = change(http, people, per_order_ceiling_eur=200)["change_request"]
    assert pending["status"] == "pending" and "simulation" in pending
    path = "/v1/tenant/policy/change-requests/"+pending["id"]+"/approve"
    assert decide(http, "post", path, people["member"], {"reason": "Self review"}).status_code == 403
    # Even a requester with the admin role cannot self-approve.
    gateway.accounts.change_user("member-one", {"role": "tenant_admin"}, people["pa"])
    assert decide(http, "post", path, people["member"], {"reason": "Still self review"}).json()["code"] == "SELF_APPROVAL_FORBIDDEN"
    approved = decide(http, "post", path, people["reviewer"], {"reason": "Independent review"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    _, q = quote(http, need)
    effective = http.get(ROOT+"/policy/effective", headers=people["member"]).json()
    assert q["policy_version_id"] == effective["version_id"]
    assert effective["fields"]["per_order_ceiling_eur"]["value"] == 200
    # Tenant MCP: real input_required and a retry bound to the human/target/body.
    key = connect(http, people["admin"], audience="tenant")
    args = {"operation": "pause", "arguments": {"agent_id": AGENT, "reason": "MCP human confirmation"}}
    first = rpc(http, bearer(key), "agent_governance", args, "/mcp/tenant")
    assert first.status_code == 200, first.text
    required = first.json()["result"]
    assert required["resultType"] == "input_required" and "confirmation" in required["inputRequests"]
    assert next(a for a in gateway.governance.register(people["pa"]) if a["agent_id"] == AGENT)["status"] == "active"
    done = rpc(http, bearer(key), "agent_governance", args, "/mcp/tenant", requestState=required["requestState"],
        inputResponses={"confirmation": {"action": "accept", "content": {"confirm": True}}})
    assert done.status_code == 200 and done.json().get("result", {}).get("structuredContent", {}).get("status") == "paused", done.text
    card = http.get("/a2a/tenant/agent-card.json").json()
    assert {"register", "effective_policy", "change_requests", "pause", "alerts"} <= {s["id"] for s in card["skills"]}
    a2a = http.post("/a2a/tenant", headers={**bearer(key), "A2A-Version": "1.0"}, json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {
        "messageId": "tenant-register", "role": "ROLE_USER", "parts": [{"data": {"operation": "register", "arguments": {}}}]}}})
    assert a2a.status_code == 200 and "external" in a2a.text, a2a.text
    def find_data(value, key):
        if isinstance(value, dict):
            if key in value:
                return value
            for child in value.values():
                found = find_data(child, key)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = find_data(child, key)
                if found:
                    return found
    command = {"operation": "resume", "arguments": {"agent_id": AGENT, "reason": "A2A human review"}}
    message = {"jsonrpc": "2.0", "id": 2, "method": "SendMessage", "params": {"message": {
        "messageId": "tenant-resume", "role": "ROLE_USER", "parts": [{"data": command}]}}}
    first = http.post("/a2a/tenant", headers={**bearer(key), "A2A-Version": "1.0"}, json=message)
    preview = find_data(first.json(), "confirmation_token")
    assert preview and preview["confirmation_required"], first.text
    assert next(a for a in gateway.governance.register(people["pa"]) if a["agent_id"] == AGENT)["status"] == "paused"
    command["confirmation_token"] = preview["confirmation_token"]
    second = http.post("/a2a/tenant", headers={**bearer(key), "A2A-Version": "1.0"}, json=message)
    assert find_data(second.json(), "status")["status"] == "active", second.text
    # Approved scope additions must reach credential binding as well as policy
    # display; an old YAML mandate must not silently keep the new customer out.
    with gateway.db.connect(write=True) as conn:
        conn.execute("INSERT INTO tenant_customers VALUES('3','hotel-aurora')")
    other_need = need.model_copy(update={"customer_id": "3"})
    assert http.post("/v1/offers", headers=agent_headers(AGENT), json=other_need.model_dump(mode="json")).status_code == 403
    addition = change(http, people, allowed_customers=["1", "3"])["change_request"]
    approved_scope = decide(http, "post", "/v1/tenant/policy/change-requests/"+addition["id"]+"/approve", people["reviewer"], {"reason": "Approve the new customer"})
    assert approved_scope.json()["status"] == "approved"
    new_key = connect(http, people["member"], agent=AGENT, customer_ids=["3"])
    for headers in (agent_headers(AGENT), bearer(new_key)):
        response = http.post("/v1/offers", headers=headers, json=other_need.model_dump(mode="json"))
        assert response.status_code == 200, response.text


def test_AC_5_outside_guardrail_with_limit_and_source(http, gateway, people, upstream, need):
    guard = gateway.governance.change_guardrails({"per_order_ceiling_eur": 500}, "Tenant ceiling", people["pa"])
    response = decide(http, "patch", ROOT+"/policy", people["member"], {"policy": {"per_order_ceiling_eur": 501}, "reason": "Beyond authority"})
    assert response.status_code == 422 and response.json()["code"] == "OUTSIDE_GUARDRAIL"
    assert response.json()["data"] == {"field": "per_order_ceiling_eur", "limit": 500, "source_layer": "tenant", "version_id": guard["id"], "author": "aurora-director"}
    # Every non-numeric field also enforces the most restrictive layer.
    gateway.governance.change_guardrails({"allowed_operations": ["lookup"], "blocked_categories": ["10400"],
        "required_claims": ["FREE_FROM_LACTOSE"], "excluded_allergens": ["AE"], "substitutes_allowed": False}, "Guardrail set", people["pa"])
    refused = http.post("/v1/offers", headers=agent_headers(AGENT), json=need.model_dump(mode="json"))
    assert refused.status_code == 200 and refused.json()["offers"] == []
    assert not upstream.price_calls and not upstream.availability_calls
    assert gateway.mandates.search("milk", [], [], 10, token=None, agent_id=AGENT) == []


def test_AC_6_rollback_uses_same_review_lifecycle(http, gateway, people):
    initial = http.get(ROOT+"/policy", headers=people["member"]).json()
    loose = change(http, people, per_order_ceiling_eur=200)["version"]
    tight = change(http, people, per_order_ceiling_eur=100)["version"]
    path = ROOT+"/policy/versions/"+loose["id"]+"/rollback"
    request = decide(http, "post", path, people["member"], {"reason": "Return to 200"}).json()
    assert request["status"] == "pending"
    req = request["change_request"]["id"]
    assert decide(http, "post", "/v1/tenant/policy/change-requests/"+req+"/approve", people["reviewer"], {"reason": "Reviewed rollback"}).json()["status"] == "approved"
    immediate = decide(http, "post", ROOT+"/policy/versions/"+tight["id"]+"/rollback", people["member"], {"reason": "Return to 100"}).json()
    assert immediate["status"] == "applied" and immediate["version"]["id"] != tight["id"]
    assert sum(e.action == "POLICY_ROLLED_BACK" for e in gateway.audit()) == 2
    inherited = decide(http, "post", ROOT+"/policy/versions/"+initial["id"]+"/rollback", people["member"], {"reason": "Inherit the parent ceiling again"}).json()
    assert inherited["status"] == "pending"
    rejected = decide(http, "post", "/v1/tenant/policy/change-requests/"+inherited["change_request"]["id"]+"/reject", people["reviewer"], {"reason": "Keep the tighter ceiling"})
    assert rejected.json()["status"] == "rejected"


def test_AC_7_lookup_quote_order_activity_policy_versions(http, gateway, people, need, upstream):
    lookup, q = quote(http, need)
    placed = order(http, q)
    assert lookup["policy_version_id"] == q["policy_version_id"] == placed["policy_version_id"]
    entries = http.get(ROOT+"/activity", headers=people["member"]).json()
    assert [e["id"] for e in entries] == sorted([e["id"] for e in entries], reverse=True)
    assert {"LOOKUP", "CREATE_QUOTE", "PLACE_ORDER"} <= {e["action"] for e in entries}
    assert all(e["policy_version_id"] == q["policy_version_id"] for e in entries if e["action"] in {"LOOKUP", "CREATE_QUOTE", "PLACE_ORDER"})
    with gateway.db.connect() as conn:
        snapshot = json.loads(conn.execute("SELECT data FROM governance_effective WHERE id=?", (q["policy_version_id"],)).fetchone()[0])
    assert snapshot["fields"]["allowed_customers"]["value"] == ["1"]


def test_AC_8_simulation_stored_history_without_upstream_or_mutations(http, gateway, people, need, upstream):
    _, q = quote(http, need)
    placed = order(http, q)
    assert placed["status"] == "CONFIRMED"
    before = len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls), len(gateway.audit())
    result = http.post(ROOT+"/policy/simulate", headers=people["member"], json={"policy": {"daily_budget_eur": 50}})
    assert result.status_code == 200, result.text
    sim = result.json()
    assert sim["needed_approval"] == 2 and sim["orders"] == sim["quotes"] == 1
    assert any(e["id"] == placed["id"] and e["outcome"] == "needed_approval" for e in sim["examples"])
    assert before == (len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls), len(gateway.audit()))
    monthly = http.post(ROOT+"/policy/simulate", headers=people["member"], json={"policy": {"monthly_budget_eur": 50}}).json()
    assert monthly["needed_approval"] == 2
    allergen = http.post(ROOT+"/policy/simulate", headers=people["member"], json={"policy": {"excluded_allergens": ["AM"]}}).json()
    assert allergen["refused"] == 2
    window = http.post(ROOT+"/policy/simulate", headers=people["member"], json={"policy": {"delivery_day_window": {"max_days": 0}}}).json()
    assert window["refused"] == 2


def test_AC_9_pause_REST_MCP_resume_tenant_stop_isolation(http, gateway, people, need, upstream):
    lookup, q = quote(http, need)
    assert decide(http, "post", ROOT+"/pause", people["member"], {"reason": "Investigate"}).json()["status"] == "paused"
    requests = [("/v1/offers", need.model_dump(mode="json")), ("/v1/quotes", {"customer_id": "1", "deliver_by": str(need.deliver_by), "need_ref": lookup["lookup_id"], "lines": [{"product_id": "1", "quantity_sales_units": 10}]}), ("/v1/orders", {"quote_id": q["id"]})]
    for path, body in requests:
        refused = http.post(path, headers={**agent_headers(AGENT), "Idempotency-Key": uuid4().hex}, json=body)
        assert refused.status_code == 423 and refused.json()["code"] == "AGENT_PAUSED", refused.text
        assert refused.json()["data"]["paused_by"] == "member-one" and refused.json()["data"]["paused_at"]
        assert refused.json()["next_action"] == "CONTACT_ADMIN"
    for tool, args in (("find_offers", {"need": need.model_dump(mode="json")}), ("create_quote", requests[1][1]), ("place_order", {"quote_id": q["id"], "idempotency_key": "paused"})):
        result = rpc(http, agent_headers(AGENT), tool, args)
        assert result.status_code == 423 and result.json()["code"] == "AGENT_PAUSED"
    from app.mcp_server import create_server
    stdio = create_server(gateway, AGENT)
    failure = asyncio.run(stdio.call_tool("find_offers", {"need": need.model_dump(mode="json")}))
    assert failure.is_error and failure.structured_content["code"] == "AGENT_PAUSED"
    assert decide(http, "post", ROOT+"/resume", people["member"], {"reason": "Safe again"}).status_code == 200
    assert order(http, q)["status"] == "CONFIRMED"
    hospital = connect(http, sign(http, "manager@kaari.example"), agent="hospital-kitchen-agent", customer_ids=["2"])
    assert decide(http, "post", "/v1/tenant/agents/pause", people["admin"], {"reason": "Tenant stop"}).json()["paused"]
    for agent in (AGENT, "hotel-chain-agent", "web-console"):
        assert http.get("/v1/me", headers=agent_headers(agent)).status_code == 423
    assert http.get("/v1/me", headers=bearer(hospital)).status_code == 200
    assert decide(http, "post", "/v1/tenant/agents/resume", people["admin"], {"reason": "Tenant clear"}).status_code == 200
    assert http.get("/v1/me", headers=agent_headers(AGENT)).status_code == 200
    assert decide(http, "post", "/v1/operator/tenants/hotel-aurora/pause", OPERATOR_HEADERS, {"reason": "Platform stop"}).status_code == 200
    assert http.get("/v1/me", headers=agent_headers(AGENT)).status_code == 423
    assert http.patch(ROOT, headers=people["admin"], json={"disabled": True}).status_code == 200
    assert decide(http, "post", ROOT+"/resume", people["member"], {"reason": "Cannot enable a disabled identity"}).json()["status"] == "disabled"


def test_AC_10_departing_owner_pause_transfer_history(http, gateway, people):
    before = len(http.get(ROOT+"/activity", headers=people["admin"]).json())
    disabled = http.patch("/v1/tenant/users/member-one", headers=people["admin"], json={"disabled": True})
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["agents_to_transfer"][0]["agent_id"] == AGENT
    assert http.get("/v1/me", headers=agent_headers(AGENT)).status_code == 423
    assert decide(http, "post", ROOT+"/resume", people["admin"], {"reason": "Cannot resume orphan"}).json()["code"] == "OWNER_UNAVAILABLE"
    moved = decide(http, "post", ROOT+"/transfer", people["admin"], {"owner_user_id": "member-two", "reason": "Replacement operator"})
    assert moved.status_code == 200 and moved.json()["owner_user_id"] == "member-two"
    after = http.get(ROOT+"/activity", headers=sign(http, "member-two@aurora.example")).json()
    assert len(after) > before and any(e["action"] == "AGENT_TRANSFERRED" for e in after)
    assert decide(http, "post", ROOT+"/resume", people["admin"], {"reason": "New owner ready"}).status_code == 200
    assert http.get("/v1/me", headers=agent_headers(AGENT)).status_code == 200


def test_AC_11_budget_alert_signed_tenant_webhook_and_other_alerts(http, gateway, people, need, upstream, receiver):
    subscription = http.post("/v1/tenant/webhook-subscriptions", headers=people["admin"], json={"url": "https://receiver.test/hook", "customer_ids": ["1"], "event_types": ["governance.alert"], "audience": "tenant"})
    assert subscription.status_code == 200, subscription.text
    change(http, people, daily_budget_eur=1300)
    _, q = quote(http, need, qty=80)
    assert order(http, q)["status"] == "CONFIRMED"
    alerts = http.get("/v1/tenant/alerts", headers=people["admin"]).json()
    budget = next(a for a in alerts if a["type"] == "budget.80")
    assert (budget["used_eur"], budget["limit_eur"]) == (1040, 1300)
    calls = [r for r in receiver["calls"] if json.loads(r.content)["payload"].get("id") == budget["id"]]
    assert len(calls) == 1
    assert verify_webhook(calls[0].content, calls[0].headers, gateway.signing.jwks())
    # A second order reaching exactly 100% raises that threshold once.
    _, q2 = quote(http, need, qty=20)
    assert order(http, q2)["status"] == "CONFIRMED"
    assert sum(a["type"] == "budget.100" for a in http.get("/v1/tenant/alerts", headers=people["admin"]).json()) == 1
    connection = connect(http, people["member"], agent=AGENT)
    assert http.get("/v1/me", headers=bearer(connection)).status_code == 200
    # Restrict hours and generate a burst of five refused lookups.
    change(http, people, ordering_hours=[])
    for _ in range(5):
        assert http.post("/v1/offers", headers=agent_headers(AGENT), json=need.model_dump(mode="json")).status_code == 403
    assert change(http, people, ordering_hours=[audit.utcnow().hour])["status"] == "pending"
    types = {a["type"] for a in http.get("/v1/tenant/alerts", headers=people["admin"]).json()}
    assert {"budget.80", "budget.100", "connection.new_client", "connection.new_address", "activity.outside_hours", "refusal.burst", "policy.change_pending"} <= types
    assert http.get("/v1/tenant/alerts", headers=sign(http, "manager@kaari.example")).json() == []


def test_AC_12_signed_governance_report_orders_approver_versions(http, gateway, people, need, upstream):
    change(http, people, approval_routing={"tenant_admin_above_eur": 100, "approver_user_ids": ["aurora-chef", "aurora-director"]})
    _, q = quote(http, need)
    placed = order(http, q)
    assert placed["status"] == "PENDING_APPROVAL"
    body = {"approver": "Finance director", "evidence_revision": placed["evidence_revision"]}
    path = "/v1/tenant/approvals/"+placed["id"]+"/approve"
    assert decide(http, "post", path, sign(http, "chef@aurora.example"), body).status_code == 403
    approved = decide(http, "post", path, people["admin"], body)
    assert approved.status_code == 200 and approved.json()["status"] == "CONFIRMED", approved.text
    _, another = quote(http, need)
    declined = order(http, another)
    assert decide(http, "post", "/v1/tenant/approvals/"+declined["id"]+"/reject", people["admin"], {"approver": "Finance director", "comment": "Duplicate request"}).json()["status"] == "REJECTED"
    now = audit.utcnow()
    params = {"from": (now-timedelta(days=1)).isoformat(), "to": (now+timedelta(days=1)).isoformat(), "signed": True}
    result = http.get("/v1/tenant/governance/report", headers=people["admin"], params=params)
    assert result.status_code == 200, result.text
    export = result.json()
    assert verify_jws(export["signature"], export["report"], http.get("/.well-known/jwks.json").json())
    entry = next(a for a in export["report"]["agents"] if a["agent_id"] == AGENT)
    assert entry["orders"][0]["id"] == placed["id"]
    assert entry["approvals"][0]["approver_user_id"] == "aurora-director"
    assert placed["policy_version_id"] in entry["policy_version_ids"] and entry["policy_versions"]
    assert any(e["action"] == "REJECT" for e in entry["refusals"])


def test_AC_13_cross_tenant_member_read_change_pause_404(http, gateway, people, need, upstream):
    foreign = "/v1/tenant/agents/hospital-kitchen-agent"
    for suffix in ("/policy/effective", "/activity", "/policy", "/policy/versions"):
        assert http.get(foreign+suffix, headers=people["member"]).status_code == 404
    for method, suffix, body in (("patch", "/policy", {"policy": {"daily_budget_eur": 1}, "reason": "Foreign"}),
        ("post", "/pause", {"reason": "Foreign"}), ("post", "/policy/simulate", {"policy": {}})):
        assert getattr(http, method)(foreign+suffix, headers=people["member"], json=body).status_code == 404
    key = connect(http, people["member"], agent=AGENT, audience="tenant")
    result = rpc(http, bearer(key), "agent_governance", {"operation": "pause", "arguments": {"agent_id": "hospital-kitchen-agent", "reason": "Foreign"}}, "/mcp/tenant")
    assert result.json()["result"]["structuredContent"]["status"] == 404, result.text
    assert rpc(http, agent_headers(AGENT), "agent_governance", {"operation": "register", "arguments": {}}, "/mcp/tenant").status_code == 403
    # An old governance snapshot must never restore an operator-revoked scope.
    change(http, people, per_order_ceiling_eur=1000)
    from app.models import PolicyUpdateRequest
    policy = gateway.policy._editable(gateway.policy.get())
    policy["agents"][AGENT]["customer_ids"] = ["2"]
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Test platform operator"))
    with pytest.raises(DomainError, match="CUSTOMER_NOT_IN_MANDATE"):
        gateway.find_offers(need, "agent:"+AGENT)
    assert not upstream.price_calls and not upstream.availability_calls


def test_AC_14_demo_customer_one_thousand_litres_unchanged(http, gateway, people, need, upstream, tmp_path):
    import yaml
    from app.config import BACKEND
    assert yaml.safe_load((BACKEND.parent / "contracts/openapi.yaml").read_text()) == http.get("/openapi.json").json()
    demo_catalog(gateway, tmp_path, upstream)
    headers = agent_headers("hotel-chain-agent")
    lookup = http.post("/v1/offers", headers=headers, json=need.model_dump(mode="json")).json()
    assert (lookup["offers"][0]["product_id"], lookup["offers"][0]["purchase_total"]) == ("464306", 1010)
    q = http.post("/v1/quotes", headers=headers, json={"customer_id": "1", "deliver_by": str(need.deliver_by), "need_ref": lookup["lookup_id"],
        "lines": [{"product_id": "464306", "quantity_sales_units": 1000}]}).json()
    placed = order(http, q, agent="hotel-chain-agent")
    assert (placed["status"], placed["total"]) == ("CONFIRMED", 1010)


@pytest.mark.parametrize("field,before,after,direction", [
    ("per_order_ceiling_eur", 100, 50, "tightening"), ("daily_budget_eur", 100, 200, "loosening"),
    ("monthly_budget_eur", None, 100, "tightening"), ("daily_budget_eur", 100, None, "loosening"),
    ("allowed_customers", ["1"], ["1", "2"], "loosening"), ("allowed_operations", ["lookup", "quote"], ["lookup"], "tightening"),
    ("allowed_categories", ["milk"], ["bread"], "mixed"), ("allowed_categories", None, [], "tightening"),
    ("blocked_categories", [], ["milk"], "tightening"), ("required_claims", ["L"], [], "loosening"),
    ("excluded_allergens", ["AE"], ["AE", "AM"], "tightening"), ("substitutes_allowed", True, False, "tightening"),
    ("substitutes_allowed", False, True, "loosening"), ("autonomy_level", "approval_required", "autonomous_within_limits", "loosening"),
    ("autonomy_level", "autonomous_within_limits", "approval_required", "tightening"),
    ("ordering_hours", [8, 9], [9], "tightening"), ("ordering_hours", [], None, "loosening"),
    ("delivery_day_window.min_days", 1, 2, "tightening"), ("delivery_day_window.max_days", 4, 5, "loosening"),
    ("approval_routing.approver_user_ids", ["a", "b"], ["a"], "tightening"),
    ("approval_routing.tenant_admin_above_eur", 100, 50, "tightening"),
    ("allowed_customers", ["1", "2"], ["2", "1"], "unchanged"),
])
def test_change_classification_table(field, before, after, direction):
    assert classify_change(expand({field: before}), expand({field: after}))[field] == direction
