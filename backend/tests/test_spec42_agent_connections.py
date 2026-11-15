"""Spec 42: console-compatible connections and real in-process MCP authentication."""
import hashlib
import json
import logging
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import audit
from app.api import create_app
from app.db import Database
from app.models import PolicyUpdateRequest
from conftest import OPERATOR_HEADERS, agent_headers, demo_catalog
from test_verifiable_mandate import buyer as vc_issuer, issue as issue_mandate, AGENT


def issue(http, agent="connected-agent", *, idem="issue-key", **changes):
    contract = http.get("/v1/connect/trading-profile").json()["contract"]["version"]
    return http.post("/v1/connections", headers={**OPERATOR_HEADERS, "Idempotency-Key": idem},
        json={"agent_id": agent, "client": "claude_code", "customer_ids": ["1"], "contract_version": contract, **changes})


def auth(connection):
    return {"Authorization": "Bearer " + connection["key"]}


def rpc(client, headers, method="ping", params=None):
    return client.post("/mcp", headers={**headers, "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": method, **({"params": params} if params is not None else {})})


def test_ac_1_create_once_digest_and_approval_first_mandate(gateway):
    policy = gateway.policy.get().model_dump(mode="json")
    policy["approval"]["max_auto_daily_total_eur"] = 1234
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Operator"))
    with TestClient(create_app(gateway), base_url="http://localhost") as client:
        response = issue(client, label="Kitchen laptop")
        assert response.status_code == 201, response.text
        value = response.json()
        assert re.fullmatch(r"con_[0-9a-f]{8}", value["connection_id"])
        assert value["key"].startswith("vtg_") and len(value["key"]) >= 40
        assert value["key_prefix"] == value["key"][:8]
        assert value["last_used_at"] is None and value["revoked_at"] is None
        assert value["mandate"]["customer_ids"] == ["1"]
        assert value["mandate"]["autonomy"] == "approval_required"
        assert value["mandate"]["max_auto_daily_total_eur"] == 1234
        # Migrations are additive and repeatable, including an existing database.
        Database(gateway.db.path)
        with gateway.db.connect() as conn:
            stored = dict(conn.execute("SELECT * FROM connections").fetchone())
        assert stored["label"] == "Kitchen laptop" and json.loads(stored["customer_ids"]) == ["1"]
        assert stored["key_digest"] == hashlib.sha256(value["key"].encode()).hexdigest()
        assert value["key"] not in json.dumps(stored)
        event = next(e for e in gateway.audit() if e.action == "CONNECTION_CREATED")
        assert event.actor == "human:operator"
        assert event.data.items() >= {"connection_id": value["connection_id"], "agent_id": value["agent_id"],
                              "client": "claude_code", "key_prefix": value["key_prefix"]}.items()


@pytest.mark.parametrize("authority", ["policy", "signed"])
def test_ac_2_existing_agent_keeps_mandate_autonomy_history_and_demo(gateway, need, upstream, tmp_path, vc_issuer, authority):
    if authority == "signed":
        check_signed_connection(gateway, need, upstream, vc_issuer)
        return
    demo_catalog(gateway, tmp_path, upstream)
    before = gateway.policy.get().agents["hotel-chain-agent"].model_dump()
    history = [e.model_dump(mode="json") for e in gateway.audit()]
    with TestClient(create_app(gateway), base_url="http://localhost") as client:
        value = issue(client, "hotel-chain-agent").json()
        assert value["mandate"] == {**before, "autonomy": "approval_required"}
        assert gateway.policy.get().agents["hotel-chain-agent"].model_dump() == before
        assert all(e in [v.model_dump(mode="json") for v in gateway.audit()] for e in history)
        lookup = client.post("/v1/offers", headers=auth(value), json=need.model_dump(mode="json"))
        assert lookup.status_code == 200, lookup.text
        confirmed = client.post(f'/v1/connections/{value["connection_id"]}/trust/confirm',
                                headers=OPERATOR_HEADERS, json={"confirmed": True})
        assert confirmed.status_code == 200, confirmed.text
        best = lookup.json()["offers"][0]
        assert (best["product_id"], best["purchase_total"], best["units_needed"]) == ("464306", 1010, 1000)
        quote = client.post("/v1/quotes", headers=auth(value), json={"customer_id": "1",
            "deliver_by": str(need.deliver_by), "need_ref": lookup.json()["lookup_id"],
            "lines": [{"product_id": "464306", "quantity_sales_units": 1000}]})
        assert quote.status_code == 200, quote.text
        order = client.post("/v1/orders", headers={**auth(value), "Idempotency-Key": "connection-demo"},
                            json={"quote_id": quote.json()["id"]})
        assert order.status_code == 200, order.text
        assert (order.json()["total"], order.json()["status"], order.json()["agent_id"]) == (1010, "CONFIRMED", "hotel-chain-agent")


def check_signed_connection(gateway, need, upstream, vc_issuer, monkeypatch=None, authority="signed"):
    with gateway.db.connect(write=True) as conn:
        conn.execute("INSERT OR IGNORE INTO tenant_customers VALUES('3','hotel-aurora')")
    policy = gateway.policy._editable(gateway.policy.get())
    policy["customers"]["3"] = {}
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Fixture"))
    token = issue_mandate(vc_issuer, daily=700, order=100)
    if authority == "policy_and_signed":
        policy = gateway.policy._editable(gateway.policy.get())
        policy["agents"][AGENT] = {"customer_ids": ["1"], "max_auto_daily_total_eur": 300,
            "max_auto_total_eur": 50, "autonomy": "approval_required"}
        gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Fixture"))
    elif authority == "signed_with_grant":
        with gateway.db.connect(write=True) as conn:
            conn.execute("INSERT INTO agent_autonomy VALUES(?,?)", (AGENT, "approval_required"))
    with TestClient(create_app(gateway), base_url="http://localhost") as client:
        headers = {**agent_headers(AGENT), "Agent-Mandate": token}
        lookup = client.post("/v1/offers", headers=headers, json=need.model_dump(mode="json"))
        assert lookup.status_code == 200, lookup.text
        with gateway.db.connect() as conn:
            effective, _, _ = gateway.mandates.authorize(conn, token, AGENT, "1")
        before = effective.agents[AGENT].model_dump(mode="json")
        policy = gateway.policy.get().model_dump(mode="json")
        history = [e.model_dump(mode="json") for e in gateway.audit()]
        response = issue(client, AGENT)
        assert response.status_code == 201, response.text
        value = response.json()
        assert value["mandate"] == {**before, "autonomy": "approval_required"}
        assert gateway.policy.get().model_dump(mode="json") == policy
        assert all(e in [v.model_dump(mode="json") for v in gateway.audit()] for e in history)
        # Another key, even with a different requested scope, must preserve authority.
        assert issue(client, AGENT, idem="another", customer_ids=["3"]).json()["mandate"] == {**before, "autonomy": "approval_required"}
        with gateway.db.connect() as conn:
            after, _, _ = gateway.mandates.authorize(conn, token, AGENT, "1")
        assert after.agents[AGENT].model_dump(mode="json") == before
        quote = client.post("/v1/quotes", headers={**auth(value), "Agent-Mandate": token}, json={
            "customer_id": "1", "deliver_by": str(need.deliver_by), "need_ref": lookup.json()["lookup_id"],
            "lines": [{"product_id": "1", "quantity_sales_units": 1}]})
        assert quote.status_code == 200, quote.text
        if monkeypatch:
            import asyncio
            from app.auth import resolve_credential
            from test_spec63_connection_contract_driving_test import measured_run
            asyncio.run(measured_run(gateway, resolve_credential(gateway.settings.keys, value["key"], gateway)))
            future = audit.utcnow() + timedelta(days=2)
            monkeypatch.setattr("app.mandates.audit.utcnow", lambda: future)
            for headers in (auth(value), {**auth(value), "Agent-Mandate": token}):
                denied = client.post("/v1/orders", headers={**headers, "Idempotency-Key": "expired"},
                                     json={"quote_id": quote.json()["id"]})
                assert (denied.status_code, denied.json()["code"]) == (403, "MANDATE_INVALID")
            # Expired history still identifies an existing agent without granting fallback authority.
            assert issue(client, AGENT, idem="expired-credential").status_code == 201
        assert (AGENT in gateway.policy.get().agents) == (authority == "policy_and_signed")


@pytest.mark.parametrize("authority", ["signed", "signed_with_grant", "policy_and_signed"])
def test_review_signed_mandate_connection_preserves_authority(gateway, need, upstream, vc_issuer, monkeypatch, authority):
    check_signed_connection(gateway, need, upstream, vc_issuer, monkeypatch, authority)


def test_review_existing_connection_cannot_restore_removed_mandate(gateway):
    with TestClient(create_app(gateway)) as client:
        value = issue(client).json()
        assert issue(client, idem="second-key").json()["mandate"] == value["mandate"]
        policy = gateway.policy._editable(gateway.policy.get())
        del policy["agents"]["connected-agent"]
        gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Authority withdrawn"))
        refused = issue(client, idem="after-withdrawal")
        assert (refused.status_code, refused.json()["code"]) == (403, "AGENT_NOT_REGISTERED")
        assert "connected-agent" not in gateway.policy.get().agents


def test_ac_3_command_replay_redacts_key_and_conflicting_body_returns_409(gateway):
    with TestClient(create_app(gateway)) as client:
        first = issue(client).json()
        for _ in range(2):
            replay = issue(client)
            assert replay.status_code == 201
            assert replay.json() == {**first, "key": None}
        gateway.settings.public_url = "https://changed-public-origin.example"
        assert issue(client).json() == {**first, "key": None}
        conflict = issue(client, client="rest")
        assert (conflict.status_code, conflict.json()["code"]) == (409, "IDEMPOTENCY_CONFLICT")
        with gateway.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM connections").fetchone()[0] == 1
            stored = conn.execute("SELECT result FROM commands WHERE key='issue-key'").fetchone()[0]
        assert json.loads(stored)["value"]["key"] is None and first["key"] not in stored
        assert len([e for e in gateway.audit() if e.action == "CONNECTION_CREATED"]) == 1


def test_ac_4_validation_operator_guard_and_missing_credentials(gateway):
    with TestClient(create_app(gateway)) as client:
        for changes in ({"agent_id": "A!"}, {"agent_id": "a"}, {"agent_id": "a" * 49},
                        {"client": "unknown"}, {"customer_ids": ["unknown"]}, {"customer_ids": []}):
            assert issue(client, **changes).status_code == 422
        body = {"agent_id": "new-agent", "client": "rest", "customer_ids": ["1"]}
        assert client.post("/v1/connections", headers=OPERATOR_HEADERS, json=body).status_code == 422
        for method, url in (("POST", "/v1/connections"), ("GET", "/v1/connections"),
                            ("DELETE", "/v1/connections/con_missing")):
            response = client.request(method, url, headers=agent_headers("external"), json=body if method == "POST" else None)
            assert (response.status_code, response.json()["code"]) == (403, "OPERATOR_REQUIRED")
            response = client.request(method, url, json=body if method == "POST" else None)
            assert response.status_code == 401
        assert not gateway.audit()


def test_ac_5_connection_key_rest_lookup_quote_and_audit_actor(gateway, need, upstream):
    with TestClient(create_app(gateway)) as client:
        value = issue(client).json()
        lookup = client.post("/v1/offers", headers=auth(value), json=need.model_dump(mode="json"))
        assert lookup.status_code == 200, lookup.text
        assert lookup.json()["agent_id"] == "connected-agent"
        quote = client.post("/v1/quotes", headers=auth(value), json={"customer_id": "1", "deliver_by": str(need.deliver_by),
            "need_ref": lookup.json()["lookup_id"], "lines": [{"product_id": "1", "quantity_sales_units": 200}]})
        assert quote.status_code == 200, quote.text
        assert quote.json()["agent_id"] == "connected-agent"
        for action in ("LOOKUP", "CREATE_QUOTE"):
            assert any(e.action == action and e.actor == "agent:connected-agent" for e in gateway.audit())
        # Existing environment keys remain usable and resolve independently.
        assert client.get("/v1/me", headers=agent_headers("external")).json()["principal"]["id"] == "external"


def test_ac_6_list_newest_first_includes_revoked_without_key_or_digest(gateway, monkeypatch):
    now = audit.utcnow()
    # App/schema startup can exceed the two-second ordering gap on a loaded
    # integration runner. Both issuance timestamps must use the fixture clock.
    monkeypatch.setattr("app.connections.audit.utcnow", lambda: now)
    with TestClient(create_app(gateway)) as client:
        older = issue(client).json()
        monkeypatch.setattr("app.connections.audit.utcnow", lambda: now + timedelta(seconds=2))
        newer = issue(client, "newer-agent", idem="newer", client="cursor").json()
        client.delete("/v1/connections/" + older["connection_id"], headers=OPERATOR_HEADERS)
        rows = client.get("/v1/connections", headers=OPERATOR_HEADERS).json()
        assert [r["connection_id"] for r in rows] == [newer["connection_id"], older["connection_id"]]
        assert rows[1]["revoked_at"] is not None
        assert all(set(r) == {"connection_id", "agent_id", "client", "key_prefix", "created_at", "last_used_at", "revoked_at", "tenant_id", "type", "audience", "scopes", "created_by", "expires_at", "customer_ids"} for r in rows)
        assert older["key"] not in json.dumps(rows) and newer["key"] not in json.dumps(rows)


def test_ac_7_revoke_immediate_rest_mcp_denial_and_mandate_retained(gateway):
    with TestClient(create_app(gateway), base_url="http://localhost") as client:
        value = issue(client).json()
        mandate = gateway.policy.get().agents["connected-agent"].model_dump()
        assert client.get("/v1/me", headers=auth(value)).status_code == 200
        switched = rpc(client, auth(value))
        assert switched.status_code == 409 and switched.json()["code"] == "CONCURRENT_CONTEXT"
        url = "/v1/connections/" + value["connection_id"]
        revoked = client.delete(url, headers=OPERATOR_HEADERS)
        assert revoked.status_code == 200 and revoked.json()["revoked_at"]
        assert client.delete(url, headers=OPERATOR_HEADERS).json() == revoked.json()
        assert client.get("/v1/me", headers=auth(value)).status_code == 401
        assert rpc(client, auth(value)).status_code == 401
        assert gateway.policy.get().agents["connected-agent"].model_dump() == mandate
        entries = [e for e in gateway.audit() if e.action == "CONNECTION_REVOKED"]
        assert len(entries) == 1 and entries[0].actor == "human:operator"
        assert entries[0].data["connection_id"] == value["connection_id"]
        assert client.delete("/v1/connections/con_missing", headers=OPERATOR_HEADERS).status_code == 404


def test_ac_8_last_used_writes_at_most_once_per_minute(gateway, monkeypatch):
    now = audit.utcnow()
    with TestClient(create_app(gateway)) as client:
        value = issue(client).json()
        def used():
            return client.get("/v1/connections", headers=OPERATOR_HEADERS).json()[0]["last_used_at"]
        assert used() is None
        monkeypatch.setattr("app.connections.audit.utcnow", lambda: now)
        assert client.get("/v1/me", headers=auth(value)).status_code == 200
        first = used()
        assert first is not None
        monkeypatch.setattr("app.connections.audit.utcnow", lambda: now + timedelta(seconds=30))
        assert client.get("/v1/me", headers=auth(value)).status_code == 200
        assert used() == first
        monkeypatch.setattr("app.connections.audit.utcnow", lambda: now + timedelta(seconds=61))
        assert client.get("/v1/me", headers=auth(value)).status_code == 200
        assert used() != first


def test_ac_9_exact_mcp_path_ping_tools_and_two_dynamic_agent_principals(gateway, need, upstream):
    with TestClient(create_app(gateway), base_url="http://localhost") as client:
        first = issue(client).json()
        second = issue(client, "second-agent", idem="second").json()
        assert rpc(client, {}).status_code == 401
        assert rpc(client, {"Authorization": "Bearer invalid"}).status_code == 401
        assert client.post("/mcp/mcp", json={}).status_code == 404
        for value in (first, second, first):
            assert rpc(client, auth(value)).status_code == 200
            tools = rpc(client, auth(value), "tools/list")
            assert tools.status_code == 200, tools.text
            assert "find_offers" in {t["name"] for t in tools.json()["result"]["tools"]}
            response = rpc(client, auth(value), "tools/call", {"name": "find_offers", "arguments": {"need": need.model_dump(mode="json")}})
            assert response.status_code == 200, response.text
            result = response.json()["result"]
            assert not result.get("isError"), result
            assert result["structuredContent"]["agent_id"] == value["agent_id"]
        lookups = sorted((e for e in gateway.audit() if e.action == "LOOKUP"), key=lambda e: e.id)
        assert [e.actor for e in lookups] == ["agent:connected-agent", "agent:second-agent", "agent:connected-agent"]


def test_ac_10_public_origin_and_mcp_disabled_urls_and_contract(gateway):
    for origin in ("http://localhost:8123", "http://127.0.0.1", "https://[::1]:8123"):
        with TestClient(create_app(gateway), base_url=origin) as client:
            value = issue(client, idem=origin).json()
            assert value["mcp_url"] == origin + "/mcp"
            assert value["rest_base_url"] == origin + "/v1"
            response = client.post(value["mcp_url"], headers={**auth(value), "Origin": origin,
                "Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
            assert response.status_code == 200, response.text
    with TestClient(create_app(gateway), base_url="https://gateway.example") as client:
        value = issue(client, idem="external-origin").json()
        assert value["mcp_url"] is None
        assert value["rest_base_url"] == "https://gateway.example/v1"
    gateway.settings.public_url = "https://public.example"
    with TestClient(create_app(gateway), base_url="http://localhost") as client:
        value = issue(client, idem="public").json()
        assert value["mcp_url"] == "https://public.example/mcp"
        assert value["rest_base_url"] == "https://public.example/v1"
        assert rpc(client, auth(value), params=None).status_code == 200
        assert client.post(value["mcp_url"], headers={**auth(value), "Origin": "https://public.example",
            "Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 200
        schema = client.get("/openapi.json").json()
        assert schema["paths"]["/v1/connections"]["post"]["responses"]["201"]
        assert set(schema["components"]["schemas"]["Connection"]["properties"]["client"]["enum"]) == {
            "claude_desktop", "claude_code", "cursor", "vscode", "chatgpt", "mcp_other", "rest",
            "curl", "python", "typescript", "openai_agents_sdk", "anthropic_api", "langchain", "a2a"}
    with TestClient(create_app(gateway, serve_mcp=False)) as client:
        value = issue(client, idem="rest-only").json()
        assert value["mcp_url"] is None and value["rest_base_url"] == "https://public.example/v1"
        assert rpc(client, auth(value)).status_code == 404


def test_review_external_fallback_steers_to_rest(gateway):
    with TestClient(create_app(gateway), base_url="https://gateway.example") as client:
        value = issue(client).json()
        assert value["mcp_url"] is None
        assert value["rest_base_url"] == "https://gateway.example/v1"
        assert client.get(value["rest_base_url"] + "/me", headers=auth(value)).status_code == 200
        mcp_connection = issue(client, idem="mcp-fallback").json()
        assert rpc(client, auth(mcp_connection)).status_code == 421


def test_ac_11_scan_audit_logs_replay_and_other_responses_for_secret(gateway, need, upstream, caplog):
    caplog.set_level(logging.DEBUG)
    with TestClient(create_app(gateway), base_url="http://localhost") as client:
        created = issue(client)
        assert created.status_code == 201
        value = created.json()
        responses = [issue(client), issue(client, client="rest"),
                     client.get("/v1/connections", headers=OPERATOR_HEADERS),
                     client.get("/v1/me", headers=auth(value)),
                     client.post("/v1/offers", headers=auth(value), json=need.model_dump(mode="json")),
                     rpc(client, auth(value), "tools/list"),
                     client.delete("/v1/connections/" + value["connection_id"], headers=OPERATOR_HEADERS),
                     client.get("/v1/me", headers=auth(value)), rpc(client, auth(value)),
                     client.get("/v1/audit", headers=OPERATOR_HEADERS)]
        assert all(value["key"] not in response.text for response in responses)
        assert value["key"] not in caplog.text
        assert value["key"] not in json.dumps([e.model_dump(mode="json") for e in gateway.audit()])
        with gateway.db.connect() as conn:
            dump = "\n".join(conn.iterdump())
        assert value["key"] not in dump
