"""Regressions at the OAuth/audiences, governance and profile merge boundaries."""
import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from app import audit
from app.guardrail_profiles import conflicted_users
from app.mcp_server import create_server
from app.webhooks import verify_webhook
from test_spec43_accounts_connections import http, sign, connect, bearer
from test_spec44_agent_governance import people
from test_webhooks import receiver, subscribe


def test_tenant_protocols_keep_human_tools_and_governance(http, gateway):
    admin = sign(http)
    key = connect(http, admin, audience="tenant")
    tools = asyncio.run(create_server(gateway, audience="tenant").list_tools())
    assert {"approve_order", "list_orders", "agent_governance"} <= {t.name for t in tools}
    operator = asyncio.run(create_server(gateway, audience="operator").list_tools())
    assert "list_tenants" in {t.name for t in operator}
    assert "agent_governance" not in {t.name for t in operator}
    for path in ("/a2a/tenant/agent-card.json", "/.well-known/agent-card/tenant.json",
                 "/a2a/tenant/.well-known/agent-card.json"):
        card = http.get(path).json()
        assert {"OAuth2", "BearerAuth"} <= card["securitySchemes"].keys()
        assert {"register", "agent_governance", "list_orders", "approve_order"} <= {s["id"] for s in card["skills"]}
    for data in ({"operation": "register", "arguments": {}},
                 {"version": 1, "command": "list_orders", "arguments": {}}):
        response = http.post("/a2a/tenant", headers={**bearer(key), "A2A-Version": "1.0"}, json={
            "jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {
                "messageId": "merge-" + next(iter(data)), "role": "ROLE_USER", "parts": [{"data": data}]}}})
        assert response.status_code == 200 and "error" not in response.json(), response.text
    paths = [getattr(r, "path", None) for r in http.app.routes]
    assert paths.count("/a2a/tenant") == paths.count("/mcp/tenant") == 1


def test_revoked_tenant_connection_stops_customerless_governance_webhooks(http, gateway, receiver):
    admin = sign(http)
    key = connect(http, admin, audience="tenant")
    principal = gateway.accounts.session(admin["Cookie"].partition("=")[2])
    subscription = http.post("/v1/tenant/webhook-subscriptions", headers=bearer(key), json={
        "url": "https://receiver.test/hook", "customer_ids": ["1"], "audience": "tenant",
        "event_types": ["governance.agent_paused", "governance.agent_resumed"]})
    assert subscription.status_code == 200, subscription.text
    gateway.governance.pause("hotel-chain-agent", True, "Review activity", principal)
    gateway.webhooks.dispatch()
    assert len(receiver["calls"]) == 1
    delivered = receiver["calls"][0]
    assert json.loads(delivered.content)["type"] == "governance.agent_paused"
    assert verify_webhook(delivered.content, delivered.headers, gateway.signing.jwks())
    assert http.delete("/v1/tenant/connections/" + key["connection_id"], headers=admin).status_code == 200
    gateway.governance.pause("hotel-chain-agent", False, "Review complete", principal)
    gateway.webhooks.dispatch()
    assert len(receiver["calls"]) == 1


def test_policy_loosening_reviewer_is_a_four_eyes_conflict(http, gateway, people):
    order = SimpleNamespace(agent_id="external")
    with gateway.db.connect() as conn:
        assert people["pr"].user_id not in conflicted_users(conn, order, people["pa"].tenant_id)
    gateway.governance.change("external", {"per_order_ceiling_eur": 100}, "Tighten", people["pm"])
    pending = gateway.governance.change("external", {"per_order_ceiling_eur": 200}, "Raise", people["pm"])
    gateway.governance.decide(pending["change_request"]["id"], "approve", "Independent review", people["pr"])
    with gateway.db.connect() as conn:
        assert people["pr"].user_id in conflicted_users(conn, order, people["pa"].tenant_id)


def test_concurrent_commit_delivers_events_added_during_an_active_sweep(gateway, receiver, monkeypatch):
    principal = gateway.accounts.user_principal(gateway.accounts.user("aurora-director"))
    subscribe(gateway, audience="tenant", event_types=["governance.agent_paused"], principal=principal)
    entered, release, second_committed = threading.Event(), threading.Event(), threading.Event()
    local = threading.local()
    original_post, original_commit = gateway.webhooks._post, gateway.db.on_commit

    def post(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(5), "First delivery was not released"
        return original_post(*args, **kwargs)

    def committed():
        if getattr(local, "second", False):
            second_committed.set()
        original_commit()

    def emit(second=False):
        local.second = second
        with gateway.db.connect(write=True) as conn:
            audit.append(conn, "system", "GOVERNANCE_AGENT_PAUSED", "Concurrent notification fixture",
                         data={"agent_id": "external", "tenant_id": principal.tenant_id})

    monkeypatch.setattr(gateway.webhooks, "_post", post)
    monkeypatch.setattr(gateway.db, "on_commit", committed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(emit)
        try:
            assert entered.wait(5), "First delivery did not start"
            second = pool.submit(emit, True)
            assert second_committed.wait(5), "Second event did not commit"
        finally:
            release.set()
        first.result(timeout=5)
        second.result(timeout=5)
    assert len(receiver["calls"]) == 2
    assert len({r.headers["webhook-id"] for r in receiver["calls"]}) == 2
