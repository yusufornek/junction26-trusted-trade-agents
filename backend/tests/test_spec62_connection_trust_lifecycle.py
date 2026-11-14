"""Six focused, offline acceptance checks for connection trust lifecycle."""
import asyncio
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp import Client

from app import audit
from app.auth import Principal
from app.connection_trust_lifecycle import CONTEXT, context
from app.db import DomainError
from app.foundation import PRINCIPAL
from app.mcp_server import create_server
from app.webhooks import SubscriptionCreate
from conftest import agent_headers, demo_catalog
from test_spec43_accounts_connections import http, sign, connect, bearer
from test_spec43_oauth_audiences import CHALLENGE, VERIFIER, REDIRECT, rpc, tool
from test_webhooks import receiver


def register(gateway, *, dynamic=False, name="Procurement runtime", client="trust-runtime"):
    return gateway.audience_oauth.register(client, {"client_name": name, "application_type": "web", "redirect_uris": [REDIRECT]}, dynamic=dynamic)["client_id"]


def begin(http, human, client):
    r = http.get("/oauth/authorize", headers=human, params={"client_id": client, "redirect_uri": REDIRECT,
        "resource": "http://localhost/mcp", "code_challenge": CHALLENGE, "code_challenge_method": "S256", "scope": "* offline_access"})
    assert r.status_code == 200, r.text
    rid = re.search(r'action="/oauth/requests/([^/]+)/decision"', r.text)[1]
    return rid, r.text


def consent(http, human, client, *, agent="hotel-chain-agent", customers=None, rid=None):
    rid = rid or begin(http, human, client)[0]
    r = http.post(f"/v1/oauth/requests/{rid}/decision", headers=human,
        json={"approve": True, "agent_id": agent, "customer_ids": customers or ["1"],
              "notification_consent": {"action_needed": True}})
    assert r.status_code == 200, r.text
    decision = r.json()
    code = parse_qs(urlsplit(decision["redirect_url"]).query)["code"][0]
    tokens = http.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": client,
        "resource": "http://localhost/mcp", "redirect_uri": REDIRECT, "code_verifier": VERIFIER, "code": code})
    assert tokens.status_code == 200, tokens.text
    return decision["connection_id"], tokens.json(), {"Authorization": "Bearer "+tokens.json()["access_token"]}


def quote(http, credential, need, product="1", units=200):
    lookup = tool(http, credential, "find_offers", {"need": need.model_dump(mode="json")})["structuredContent"]
    assert "lookup_id" in lookup, lookup
    return tool(http, credential, "create_quote", {"customer_id": "1", "deliver_by": str(need.deliver_by),
        "need_ref": lookup["lookup_id"], "lines": [{"product_id": product, "quantity_sales_units": units}]})["structuredContent"]


def test_AC_1_dynamic_publisher_impersonation_and_admin_order_grant(http, gateway, need, upstream):
    registered = http.post('/oauth/register', json={"client_name": "OpenAI procurement assistant", "application_type": "web",
        "token_endpoint_auth_method": "none", "redirect_uris": [REDIRECT]})
    assert registered.status_code == 201, registered.text
    client = registered.json()["client_id"]
    with gateway.db.connect(write=True) as conn:
        conn.execute("UPDATE users SET role='member' WHERE id='aurora-director'")
    member = sign(http)
    rid, html = begin(http, member, client)
    view = http.get('/v1/oauth/requests/'+rid, headers=member).json()
    assert view["client_tier"] == "unverified" and view["logo"] is None
    assert "not OpenAI" in view["verification_label"] and view["registered_at"] and "not OpenAI" in html
    cid, _, credential = consent(http, member, client, rid=rid)
    q = quote(http, credential, need)
    refused = tool(http, credential, "place_order", {"quote_id": q["id"], "idempotency_key": "unverified"})
    assert refused["isError"] and refused["structuredContent"]["status"] == 403
    assert http.post(f'/v1/connections/{cid}/trust/order-scope', headers=member).status_code == 403
    with gateway.db.connect(write=True) as conn:
        conn.execute("UPDATE users SET role='tenant_admin' WHERE id='aurora-director'")
    admin = sign(http)
    assert http.post(f'/v1/connections/{cid}/trust/order-scope', headers=admin).status_code == 200
    assert http.post(f'/v1/connections/{cid}/trust/confirm', headers=admin, json={"confirmed": True}).status_code == 200
    ordered = tool(http, credential, "place_order", {"quote_id": q["id"], "idempotency_key": "granted"})["structuredContent"]
    assert ordered["status"] == "PENDING_APPROVAL"  # Unverified identity never inherits earned autonomy.
    trust = gateway.connection_trust
    assert "not OpenAI" in trust.client_info(client, {"client_name": "ОреnAI procurement assistant"}, REDIRECT)["verification_label"]
    trust.register_client('https://openai.com/client.json', 'metadata')
    assert trust.client_info('https://openai.com/client.json', {"client_name": "Runtime"}, 'https://openai.com/callback')["client_tier"] == "verified"
    assert trust.client_info('https://openai.com/client.json', {"client_name": "Runtime"}, REDIRECT)["client_tier"] == "unverified"


def test_AC_2_interleaved_browser_authorizations_keep_complete_bindings(http, gateway):
    admin = sign(http)
    client = register(gateway)
    with gateway.db.connect(write=True) as conn:
        conn.execute("INSERT INTO tenant_customers VALUES('77','hotel-aurora')")
    first = begin(http, admin, client)[0]
    second = begin(http, admin, client)[0]
    b, _, _ = consent(http, admin, client, rid=second, agent="second-runtime", customers=["77"])
    a, _, _ = consent(http, admin, client, rid=first, customers=["1"])
    with gateway.db.connect() as conn:
        for rid, cid, agent, customers in [(first, a, "hotel-chain-agent", ["1"]), (second, b, "second-runtime", ["77"])]:
            bound = json.loads(conn.execute("SELECT data FROM connection_trust_requests WHERE id=?", (rid,)).fetchone()[0])
            c = conn.execute("SELECT * FROM connections WHERE connection_id=?", (cid,)).fetchone()
            assert (bound["request_id"], bound["agent_id"], bound["customer_ids"]) == (rid, agent, customers)
            assert c["agent_id"] == agent and json.loads(c["customer_ids"]) == customers
            assert bound["user_id"] == "aurora-director" and bound["client_id"] == client
            assert bound["challenge"] == CHALLENGE and bound["notification_consent"] == {"action_needed": True}
    assert http.post(f'/v1/oauth/requests/{first}/decision', headers=admin, json={"approve": True, "agent_id": "second-runtime"}).status_code == 410


def test_AC_3_parallel_refresh_grace_reauth_and_existing_purchase_resume(http, gateway, need, upstream, monkeypatch):
    admin, client = sign(http), register(gateway)
    cid, tokens, credential = consent(http, admin, client)
    q = quote(http, credential, need)
    order = tool(http, credential, "place_order", {"quote_id": q["id"], "idempotency_key": "stable-purchase"})["structuredContent"]
    form = {"grant_type": "refresh_token", "client_id": client, "resource": "http://localhost/mcp", "refresh_token": tokens["refresh_token"]}
    barrier = Barrier(2)
    def worker():
        barrier.wait(timeout=5)
        return gateway.audience_oauth.token(form)
    with ThreadPoolExecutor(2) as workers:
        one, two = list(workers.map(lambda _: worker(), range(2)))
    assert one == two and one["access_token"] != tokens["access_token"]
    with gateway.db.connect() as conn:
        encrypted = conn.execute("SELECT successor FROM connection_trust_refresh").fetchone()[0]
        assert one["access_token"].encode() not in encrypted
    now = audit.utcnow()
    lifecycle_now = gateway.connection_trust.clock()
    monkeypatch.setattr(audit, "utcnow", lambda: now + timedelta(seconds=31))
    monkeypatch.setattr(gateway.connection_trust, "clock", lambda: lifecycle_now + timedelta(seconds=31))
    with pytest.raises(Exception, match="invalid_grant|Refresh reuse"):
        gateway.audience_oauth.token(form)
    assert gateway.connection_trust.health(cid)["state"] == "REAUTH_REQUIRED"
    assert rpc(http, {"Authorization": "Bearer "+one["access_token"]}).status_code == 401
    resumed_cid, _, resumed = consent(http, admin, client)
    assert resumed_cid == cid
    replay = tool(http, resumed, "place_order", {"quote_id": q["id"], "idempotency_key": "stable-purchase"})["structuredContent"]
    assert replay["id"] == order["id"]
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert conn.execute("SELECT 1 FROM audit WHERE action='CONNECTION_REAUTH_REQUIRED'").fetchone()
    assert any(n["event"] == "CONNECTION_REAUTH_REQUIRED" for n in gateway.connection_trust.health(cid)["notifications"])
    # The operator-consented registry has the same lifecycle, including a
    # different-context replay inside the grace window and same-connection recovery.
    from app.oauth import ConsentDecision
    gateway.settings.public_url = "http://localhost"
    operator = gateway.accounts.bind(Principal("web-console", operator=True))
    metadata = {"client_name": "Operator runtime", "application_type": "web", "redirect_uris": [REDIRECT],
        "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"]}
    gateway.oauth.register(metadata, client_id="operator-runtime")
    def operator_grant():
        rid = gateway.oauth.authorize({"client_id": "operator-runtime", "redirect_uri": REDIRECT, "resource": gateway.oauth.resource,
            "code_challenge": CHALLENGE, "code_challenge_method": "S256", "response_type": "code", "scope": "mcp offline_access"})
        decision = gateway.oauth.decide(rid, ConsentDecision(decision="approve", agent_id="hotel-chain-agent", customer_ids=["1"]), operator)
        code = parse_qs(urlsplit(decision["redirect_url"]).query)["code"][0]
        return decision["connection_id"], gateway.oauth.token({"grant_type": "authorization_code", "client_id": "operator-runtime",
            "resource": gateway.oauth.resource, "redirect_uri": REDIRECT, "code_verifier": VERIFIER, "code": code}, None)
    old_cid, old = operator_grant()
    rotate = {"grant_type": "refresh_token", "client_id": "operator-runtime", "resource": gateway.oauth.resource, "refresh_token": old["refresh_token"]}
    successor = gateway.oauth.token(rotate, None)
    assert gateway.oauth.token(rotate, None) == successor
    changed = CONTEXT.set(context("second-network", "oauth", "copied-runtime"))
    try:
        with pytest.raises(Exception):
            gateway.oauth.token(rotate, None)
    finally:
        CONTEXT.reset(changed)
    assert gateway.connection_trust.health(old_cid)["state"] == "REAUTH_REQUIRED"
    assert operator_grant()[0] == old_cid


def test_AC_4_pause_closes_sdk_stream_cancels_subscription_and_refuses_queued_order(http, gateway, need, upstream, receiver):
    admin_headers = sign(http)
    admin = gateway.accounts.session(admin_headers["Cookie"].partition('=')[2])
    c = connect(http, admin_headers)
    p = gateway.accounts.bind(gateway.connections.resolve(c["key"]))
    sub = gateway.webhooks.create(SubscriptionCreate(url="https://receiver.test/hook", customer_ids=["1"],
        event_types=["order.confirmed"]), p)
    # A pending order represents a queued call holding an already resolved principal.
    q = quote(http, bearer(c), need)
    async def run():
        server = create_server(gateway, "hotel-chain-agent")
        token = PRINCIPAL.set(p)
        try:
            async with Client(server) as client:
                async with client.listen(resource_subscriptions=["luotsi://agent/context"]) as updates:
                    assert gateway.agent_surface.subscribers
                    def pause():
                        token = PRINCIPAL.set(admin)
                        try:
                            gateway.governance.pause(p.agent_id, True, "Owner withdrew authority", admin)
                        finally:
                            PRINCIPAL.reset(token)
                    await asyncio.to_thread(pause)
                    with pytest.raises(StopAsyncIteration):
                        await asyncio.wait_for(anext(updates), 5)
                    with pytest.raises(DomainError) as refused:
                        gateway.orders.place_order(q["id"], "queued-call", p.agent_id)
                    assert (refused.value.status, refused.value.code) == (423, "AGENT_PAUSED")
                assert not gateway.agent_surface.subscribers
                with gateway.db.connect() as conn:
                    saved = conn.execute("SELECT data FROM webhook_subscriptions WHERE id=?", (sub["id"],)).fetchone()[0]
                    assert not json.loads(saved)["enabled"]
                assert not receiver["calls"]
        finally:
            PRINCIPAL.reset(token)
    asyncio.run(run())
    assert http.post('/v1/orders', headers=bearer(c), json={"quote_id": q["id"]}).status_code == 423


def test_AC_5_parallel_credential_contexts_and_connection_local_autonomy(http, gateway, need, upstream, tmp_path):
    demo_catalog(gateway, tmp_path, upstream)
    admin_headers = sign(http)
    admin = gateway.accounts.session(admin_headers["Cookie"].partition('=')[2])
    c = connect(http, admin_headers)
    key = bearer(c)
    first = http.get('/v1/me', headers={**key, "User-Agent": "runtime-one"})
    assert first.status_code == 200
    copied = http.get('/v1/me', headers={**key, "User-Agent": "runtime-two"})
    assert copied.status_code == 409 and copied.json()["code"] == "CONCURRENT_CONTEXT"
    state = gateway.connection_trust.health(c["connection_id"])
    assert state["state"] == "READ_ONLY" and len(state["context_history"]) == 2
    p = gateway.accounts.bind(gateway.connections.resolve(c["key"]))
    with pytest.raises(DomainError) as refused:
        with gateway.db.connect(write=True) as conn:
            gateway.connection_trust.require_order(p, conn)
    assert refused.value.code == "CONCURRENT_CONTEXT"
    # A separate connection for the same autonomous agent has no continuity grant.
    second = connect(http, admin_headers)
    q = quote(http, bearer(second), need, "464306", 1000)
    order = tool(http, bearer(second), "place_order", {"quote_id": q["id"], "idempotency_key": "new-connection"})["structuredContent"]
    assert order["status"] == "PENDING_APPROVAL"
    confirmation = http.post(f'/v1/connections/{second["connection_id"]}/trust/confirm', headers=admin_headers, json={"confirmed": True})
    assert confirmation.status_code == 200, confirmation.text
    p2 = gateway.accounts.bind(gateway.connections.resolve(second["key"]))
    token = PRINCIPAL.set(p2)
    try:
        assert not gateway.connection_trust.approval_required(p2.agent_id)
    finally:
        PRINCIPAL.reset(token)
    assert any(n["event"] == "CONNECTION_CONTINUITY_CONFIRMED" for n in confirmation.json()["notifications"])
    fresh = quote(http, bearer(second), need, "464306", 1000)
    autonomous = tool(http, bearer(second), "place_order", {"quote_id": fresh["id"], "idempotency_key": "confirmed-continuity"})["structuredContent"]
    assert autonomous["status"] == "CONFIRMED"


def test_AC_6_seeded_demo_remains_464306_1010_confirmed(http, gateway, need, upstream, tmp_path):
    demo_catalog(gateway, tmp_path, upstream)
    credential = agent_headers("hotel-chain-agent")
    q = quote(http, credential, need, "464306", 1000)
    result = tool(http, credential, "place_order", {"quote_id": q["id"], "idempotency_key": "trust-demo"})["structuredContent"]
    assert (q["lines"][0]["product_id"], result["total"], result["status"]) == ("464306", 1010, "CONFIRMED")
