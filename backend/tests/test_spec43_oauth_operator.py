"""Spec 43 R-9/R-10/R-13: operator-consented OAuth on the real gateway MCP transport."""
import base64
import gzip
import json
import logging
import re
import socket
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import oauth
from app.api import create_app
from app.models import PolicyUpdateRequest
from conftest import OPERATOR_HEADERS, OPERATOR_TOKEN, agent_headers
from test_sandbox import sandbox_gateway, sandbox_need, SANDBOX_ADMIN, ADMIN_HEADERS
from test_spec42_agent_connections import rpc
from test_verifiable_mandate import buyer as vc_issuer, issue as issue_mandate, AGENT

ORIGIN = "https://gateway.test"
RESOURCE = ORIGIN + "/mcp"
CALLBACK = "https://chatgpt.com/connector_platform/oauth/callback"
CIMD = "https://client.test/oauth/metadata.json"
VERIFIER = "v" * 64


def metadata(**changes):
    return {"client_name": "ChatGPT", "application_type": "web", "redirect_uris": [CALLBACK],
            "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"], **changes}


def register(client, **changes):
    response = client.post("/oauth/register", json=metadata(**changes))
    assert response.status_code == 201, response.text
    return response.json()


def start(client, client_id, **changes):
    response = client.get("/oauth/authorize", params={"client_id": client_id, "redirect_uri": CALLBACK,
        "response_type": "code", "code_challenge_method": "S256", "code_challenge": oauth.challenge(VERIFIER),
        "state": "client-state", "resource": RESOURCE, "scope": "mcp offline_access", **changes})
    assert response.status_code == 200, response.text
    request_id = re.search(r'name="request_id" value="([^"]+)"', response.text).group(1)
    return request_id, response


def consent(client, request_id, *, headers=OPERATOR_HEADERS, **changes):
    return client.post(f"/v1/oauth/requests/{request_id}/decision", headers=headers,
        json={"decision": "approve", "agent_id": "oauth-agent", "customer_ids": ["1"], **changes})


def grant(client, client_id, *, headers=OPERATOR_HEADERS, **changes):
    request_id, _ = start(client, client_id)
    approved = consent(client, request_id, headers=headers, **changes)
    assert approved.status_code == 200, approved.text
    query = parse_qs(urlsplit(approved.json()["redirect_url"]).query)
    assert query["iss"] == [ORIGIN] and query["state"] == ["client-state"]
    return {"grant_type": "authorization_code", "client_id": client_id, "code": query["code"][0],
            "redirect_uri": CALLBACK, "code_verifier": VERIFIER, "resource": RESOURCE}, approved.json(), request_id


def exchange(client, body):
    response = client.post("/oauth/token", data=body)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert result["expires_in"] == 3600 and result["token_type"] == "Bearer"
    initialized = rpc(client, bearer(result), "initialize", {"protocolVersion": "2025-11-25",
        "capabilities": {}, "clientInfo": {"name": "oauth-test", "version": "1"}})
    assert initialized.status_code == 200, initialized.text
    return result


def bearer(token):
    return {"Authorization": "Bearer " + token["access_token"]}


def refresh(client, client_id, token, **changes):
    return client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": client_id,
        "resource": RESOURCE, "refresh_token": token["refresh_token"], **changes})


def call(client, token, name, arguments):
    response = rpc(client, bearer(token), "tools/call", {"name": name, "arguments": arguments})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result.get("isError"), result
    return result["structuredContent"]


@pytest.mark.parametrize("registration", ["cimd", "dcr", "pre_registered"])
def test_ac_6_discovery_consent_and_full_sandbox_purchase_as_agent(sandbox_gateway, monkeypatch, registration):
    service = sandbox_gateway
    service.settings.public_url = ORIGIN
    service.oauth.resolver = lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    with respx.mock(assert_all_called=False) as mock, TestClient(create_app(service), base_url=ORIGIN) as client:
        discovery = rpc(client, {})
        assert discovery.status_code == 401
        assert f'resource_metadata="{ORIGIN}/.well-known/oauth-protected-resource/mcp"' in discovery.headers["www-authenticate"]
        resource = client.get("/.well-known/oauth-protected-resource/mcp").json()
        assert resource == client.get("/.well-known/oauth-protected-resource").json()
        assert resource["resource"] == RESOURCE and resource["authorization_servers"] == [ORIGIN]
        assert "offline_access" not in resource["scopes_supported"]
        server = client.get("/.well-known/oauth-authorization-server").json()
        assert server["issuer"] == ORIGIN and server["code_challenge_methods_supported"] == ["S256"]
        assert server["authorization_response_iss_parameter_supported"] and server["client_id_metadata_document_supported"]
        if registration == "cimd":
            route = mock.get("https://93.184.216.34/oauth/metadata.json", headers={"host": "client.test"}).mock(
                return_value=httpx.Response(200, json={"client_id": CIMD, **metadata()}))
            client_id = CIMD
        elif registration == "pre_registered":
            registered = client.post("/v1/oauth/clients", headers=ADMIN_HEADERS, json={"client_id": "pre-client", **metadata()})
            assert registered.status_code == 201
            client_id = registered.json()["client_id"]
        else:
            client_id = register(client)["client_id"]
        body, decision, request_id = grant(client, client_id, headers=ADMIN_HEADERS)
        if registration == "cimd":
            assert route.called
            request = route.calls[0].request
            assert "authorization" not in request.headers
        connection_id = decision["connection_id"]
        view = client.get("/v1/oauth/requests/" + request_id, headers=ADMIN_HEADERS).json()
        assert view["client_name"] == "ChatGPT" and view["redirect_host"] == "chatgpt.com"
        assert "code" not in view and "state" not in view and view["status"] == "approved"
        connection = client.get("/v1/connections", headers=ADMIN_HEADERS).json()[0]
        assert connection["credential_kind"] == "oauth" and connection["client"] == "chatgpt"
        assert connection["connection_id"] == connection_id and "key" not in connection
        mandate = service.policy.get().agents["oauth-agent"]
        assert mandate.autonomy == "approval_required" and mandate.customer_ids == ["1"]
        token = exchange(client, body)
        for revision in ("2025-11-25", "2026-07-28"):
            params = {"_meta": {"io.modelcontextprotocol/protocolVersion": revision,
                "io.modelcontextprotocol/clientCapabilities": {}}} if revision == "2026-07-28" else None
            tools = rpc(client, {**bearer(token), "MCP-Protocol-Version": revision,
                **({"MCP-Method": "tools/list"} if revision == "2026-07-28" else {})}, "tools/list", params)
            assert tools.status_code == 200, (revision, tools.text)
            assert "find_offers" in {v["name"] for v in tools.json()["result"]["tools"]}
        need = sandbox_need(service).model_dump(mode="json")
        from test_spec63_connection_contract_driving_test import measured_run
        from app.auth import resolve_credential
        import asyncio
        asyncio.run(measured_run(service, resolve_credential(service.settings.keys, token["access_token"], service, RESOURCE)))
        lookup = call(client, token, "find_offers", {"need": need})
        assert lookup["agent_id"] == "oauth-agent"
        quote = call(client, token, "create_quote", {"customer_id": "1", "deliver_by": need["deliver_by"],
            "plan_ref": {"lookup_id": lookup["lookup_id"], "plan_index": 0}})
        order = call(client, token, "place_order", {"quote_id": quote["id"], "idempotency_key": "oauth-purchase"})
        assert order["status"] == "PENDING_APPROVAL" and order["agent_id"] == "oauth-agent"
        approval_url = f'/v1/approvals/{order["id"]}/approve'
        approval_body = {"approver": "Operator", "evidence_revision": order["evidence_revision"]}
        preview = client.post(approval_url, headers=ADMIN_HEADERS, json=approval_body)
        assert preview.status_code == 200 and preview.json()["confirmation_required"]
        assert call(client, token, "get_order_status", {"order_id": order["id"]})["status"] == "PENDING_APPROVAL"
        approved = client.post(approval_url, headers=ADMIN_HEADERS,
            json={**approval_body, "confirmation_token": preview.json()["confirmation_token"]})
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "CONFIRMED"
        assert approved.json()["context"]["connection_authority"] == order["context"]["connection_authority"]
        assert approved.json()["approval_side"] == "valio_operator"
        assert call(client, token, "get_order_status", {"order_id": order["id"]})["status"] == "CONFIRMED"
        for action in ["LOOKUP", "CREATE_QUOTE", "PLACE_ORDER"]:
            assert any(e.action == action and e.actor == "agent:oauth-agent" for e in service.audit())
        assert client.get("/v1/me", headers=bearer(token)).status_code == 401
        assert client.post("/a2a", headers=bearer(token), json={}).status_code == 401
        assert client.delete("/v1/connections/" + connection_id, headers=ADMIN_HEADERS).status_code == 200
        assert rpc(client, bearer(token)).status_code == 401
        assert refresh(client, client_id, token).status_code == 400
        assert service.policy.get().agents["oauth-agent"] == mandate


@pytest.mark.parametrize("failure", ["verifier", "redirect", "resource", "other_client", "reuse", "code_reuse", "expired_access", "expired_refresh", "foreign_audience"])
def test_ac_7_pkce_redirect_resource_rotation_reuse_and_expiry(gateway, monkeypatch, failure):
    gateway.settings.public_url = ORIGIN
    with TestClient(create_app(gateway), base_url=ORIGIN) as client:
        client_id = register(client)["client_id"]
        body, _, _ = grant(client, client_id)
        if failure in {"verifier", "redirect", "resource", "other_client"}:
            field, value = {"verifier": ("code_verifier", "x" * 64), "redirect": ("redirect_uri", CALLBACK + "/"),
                "resource": ("resource", ORIGIN + "/a2a"), "other_client": ("client_id", register(client)["client_id"])}[failure]
            refused = client.post("/oauth/token", data={**body, field: value})
            assert refused.status_code == 400 and refused.json()["error"] in {"invalid_grant", "invalid_target"}
            assert rpc(client, bearer(exchange(client, body))).status_code == 200
            return
        token = exchange(client, body)
        if failure == "foreign_audience":
            with gateway.db.connect(write=True) as conn:
                conn.execute("UPDATE oauth_families SET resource=?", (ORIGIN + "/a2a",))
            assert rpc(client, bearer(token)).status_code == 401
            return
        if failure.startswith("expired"):
            moment = oauth.now() + (timedelta(hours=2) if failure == "expired_access" else timedelta(days=31))
            monkeypatch.setattr(oauth, "now", lambda: moment)
            assert rpc(client, bearer(token)).status_code == 401
            if failure == "expired_access":
                assert refresh(client, client_id, token).status_code == 200
            else:
                assert refresh(client, client_id, token).status_code == 400
            return
        rotated_response = refresh(client, client_id, token)
        assert rotated_response.status_code == 200
        rotated = rotated_response.json()
        assert rotated["access_token"] != token["access_token"] and rotated["refresh_token"] != token["refresh_token"]
        assert rpc(client, bearer(token)).status_code == 401
        assert rpc(client, bearer(rotated)).status_code == 200
        if failure == "code_reuse":
            reused = client.post("/oauth/token", data=body)
        else:
            reused = refresh(client, client_id, token)
            assert reused.status_code == 200 and reused.json() == rotated
            moment = gateway.connection_trust.clock() + timedelta(seconds=31)
            monkeypatch.setattr(gateway.connection_trust, "clock", lambda: moment)
            reused = refresh(client, client_id, token)
        assert reused.status_code == 400 and reused.json()["error"] == "invalid_grant"
        assert rpc(client, bearer(rotated)).status_code == 401
        assert refresh(client, client_id, rotated).status_code == 400
        action = "OAUTH_TOKEN_FAMILY_REVOKED" if failure == "code_reuse" else "CONNECTION_REAUTH_REQUIRED"
        assert any(e.action == action for e in gateway.audit())


def test_ac_8_dcr_application_type_required_and_registered_flow(gateway):
    gateway.settings.public_url = ORIGIN
    with TestClient(create_app(gateway), base_url=ORIGIN) as client:
        value = metadata()
        del value["application_type"]
        response = client.post("/oauth/register", json=value)
        assert response.status_code == 400 and response.json()["error"] == "invalid_client_metadata"
        client_id = register(client)["client_id"]
        body, _, _ = grant(client, client_id)
        assert rpc(client, bearer(exchange(client, body)), "tools/list").status_code == 200
        assert client.post("/oauth/register", json=metadata(redirect_uris=["http://evil.test/callback"])).status_code == 400
        assert client.post("/oauth/register", json=metadata(application_type="native", redirect_uris=["http://localhost:9876/callback"])).status_code == 201


def test_ac_12_no_credentials_after_issuance_in_responses_audit_database_or_logs(gateway, caplog):
    caplog.set_level(logging.DEBUG)
    gateway.settings.public_url = ORIGIN
    with TestClient(create_app(gateway), base_url=ORIGIN) as client:
        registered = register(client, token_endpoint_auth_method="client_secret_post")
        body, decision, request_id = grant(client, registered["client_id"])
        body["client_secret"] = registered["client_secret"]
        token = exchange(client, body)
        responses = [client.get("/v1/connections", headers=OPERATOR_HEADERS),
            client.get("/v1/oauth/requests/" + request_id, headers=OPERATOR_HEADERS),
            consent(client, request_id), rpc(client, bearer(token), "tools/list"),
            client.get("/v1/me", headers=bearer(token)),
            client.post("/oauth/token", data=body),
            client.delete("/v1/connections/" + decision["connection_id"], headers=OPERATOR_HEADERS),
            client.get("/v1/audit", headers=OPERATOR_HEADERS)]
        secrets = [OPERATOR_TOKEN, registered["client_secret"], body["code"], token["access_token"], token["refresh_token"]]
        with gateway.db.connect() as conn:
            dump = "\n".join(conn.iterdump())
            digests = [r[0] for table in ["oauth_codes", "oauth_tokens"] for r in conn.execute(f"SELECT digest FROM {table}")]
        audit_json = json.dumps([e.model_dump(mode="json") for e in gateway.audit()])
        for secret in secrets:
            assert secret not in dump and secret not in caplog.text and secret not in audit_json
            assert all(secret not in response.text for response in responses)
        for value in digests:
            assert value not in audit_json and value not in caplog.text
            assert all(value not in response.text for response in responses)


def test_ac_13_existing_keys_operator_and_demo_path_keep_working(gateway, need, upstream, tmp_path):
    from test_spec42_agent_connections import test_ac_2_existing_agent_keeps_mandate_autonomy_history_and_demo
    # Reuse the actual regression assertion: 464306, 1,000 L, EUR 1,010 and confirmed order.
    test_ac_2_existing_agent_keeps_mandate_autonomy_history_and_demo(gateway, need, upstream, tmp_path, None, "policy")


def test_r10_consent_denial_operator_guards_and_single_use(gateway):
    gateway.settings.public_url = ORIGIN
    with TestClient(create_app(gateway), base_url=ORIGIN) as client:
        client_id = register(client)["client_id"]
        request_id, _ = start(client, client_id)
        url = "/v1/oauth/requests/" + request_id
        for headers, status in [({}, 401), (agent_headers("external"), 403)]:
            assert client.get(url, headers=headers).status_code == status
            assert consent(client, request_id, headers=headers).status_code == status
        denied = consent(client, request_id, decision="deny")
        assert denied.status_code == 200
        query = parse_qs(urlsplit(denied.json()["redirect_url"]).query)
        assert query == {"error": ["access_denied"], "state": ["client-state"], "iss": [ORIGIN]}
        assert consent(client, request_id).status_code == 409
        assert client.get("/v1/connections", headers=OPERATOR_HEADERS).json() == []


@pytest.mark.parametrize("decision", ["approve", "deny"])
def test_r10_html_signin_csrf_and_consent(gateway, caplog, decision):
    caplog.set_level(logging.DEBUG)
    gateway.settings.public_url = ORIGIN
    with TestClient(create_app(gateway), base_url=ORIGIN) as client:
        client_id = register(client, client_name="<script>evil</script>")["client_id"]
        request_id, login = start(client, client_id)
        assert "&lt;script&gt;" in login.text and "<script>" not in login.text
        assert 'name="existing_agent"' not in login.text
        page = client.post("/oauth/authorize", data={"request_id": request_id, "action": "login", "operator_token": OPERATOR_TOKEN})
        assert page.status_code == 200 and OPERATOR_TOKEN not in page.text
        assert "HttpOnly" in page.headers["set-cookie"] and "Secure" in page.headers["set-cookie"]
        assert 'name="existing_agent"' in page.text and 'name="customer_ids"' in page.text
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        session = client.cookies.get("oauth_consent_" + request_id)
        with gateway.db.connect() as conn:
            dump = "\n".join(conn.iterdump())
        assert session not in dump and csrf not in dump and OPERATOR_TOKEN not in dump
        values = {"request_id": request_id, "action": decision, "agent_id": "html-agent", "customer_ids": "1", "scopes": "mcp"}
        assert client.post("/oauth/authorize", data=values).status_code == 403
        response = client.post("/oauth/authorize", data={**values, "csrf": csrf}, follow_redirects=False)
        assert response.status_code == 303
        query = parse_qs(urlsplit(response.headers["location"]).query)
        assert query["iss"] == [ORIGIN] and query["state"] == ["client-state"]
        if decision == "deny":
            assert query["error"] == ["access_denied"]
        else:
            assert "code" in query
        assert OPERATOR_TOKEN not in caplog.text and csrf not in caplog.text
        assert session not in caplog.text
        assert client.post("/oauth/authorize", data={**values, "csrf": csrf}).status_code == 403


def test_r10_frontend_handoff_exposes_only_request_id(gateway):
    gateway.settings.public_url = ORIGIN
    gateway.settings.frontend_consent_url = "https://console.test/consent"
    with TestClient(create_app(gateway), base_url=ORIGIN) as client:
        client_id = register(client)["client_id"]
        response = client.get("/oauth/authorize", params={"client_id": client_id, "redirect_uri": CALLBACK,
            "response_type": "code", "code_challenge_method": "S256", "code_challenge": oauth.challenge(VERIFIER),
            "resource": RESOURCE, "state": "sensitive-state"}, follow_redirects=False)
        assert response.status_code == 303
        parsed = urlsplit(response.headers["location"])
        assert parsed.netloc == "console.test" and set(parse_qs(parsed.query)) == {"request_id"}


def test_r13_connection_customers_intersect_current_mandate_and_preserve_existing(gateway, need, upstream):
    # Exercise two customers of the same business; cross-tenant access is tested separately.
    from app.tenancy import HOTEL
    with gateway.db.connect(write=True) as conn:
        conn.execute("UPDATE tenant_customers SET tenant_id=? WHERE customer_id='2'", (HOTEL,))
    policy = gateway.policy._editable(gateway.policy.get())
    policy["customers"]["2"] = {}
    policy["agents"]["external"]["customer_ids"] = ["1", "2"]
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Fixture"))
    before = gateway.policy.get().agents["external"].model_dump()
    gateway.settings.public_url = ORIGIN
    with TestClient(create_app(gateway), base_url=ORIGIN) as client:
        client_id = register(client, client_name="Claude", redirect_uris=[CALLBACK])["client_id"]
        body, _, _ = grant(client, client_id, agent_id="external", customer_ids=["1"])
        assert gateway.policy.get().agents["external"].model_dump() == before
        token = exchange(client, body)
        result = rpc(client, bearer(token), "tools/call", {"name": "find_offers", "arguments": {
            "need": need.model_copy(update={"customer_id": "2"}).model_dump(mode="json")}}).json()["result"]
        assert result["isError"] and result["structuredContent"]["code"] == "CUSTOMER_NOT_IN_MANDATE"
        assert not upstream.price_calls and not upstream.availability_calls
        assert call(client, token, "find_offers", {"need": need.model_dump(mode="json")})["agent_id"] == "external"
        # Same agent, different connection: customer-scoped reads must also stay narrowed.
        other_need = need.model_copy(update={"customer_id": "2"}).model_dump(mode="json")
        other_lookup = client.post("/v1/offers", headers=agent_headers("external"), json=other_need).json()
        other_quote = client.post("/v1/quotes", headers=agent_headers("external"), json={
            "customer_id": "2", "deliver_by": other_need["deliver_by"], "need_ref": other_lookup["lookup_id"],
            "lines": [{"product_id": "1", "quantity_sales_units": 1}]}).json()
        other_order = client.post("/v1/orders", headers={**agent_headers("external"), "Idempotency-Key": "customer-two"},
            json={"quote_id": other_quote["id"]}).json()
        result = rpc(client, bearer(token), "tools/call", {"name": "get_order_status", "arguments": {
            "order_id": other_order["id"]}}).json()["result"]
        assert result["isError"] and result["structuredContent"]["code"] == "CUSTOMER_NOT_IN_MANDATE"
        policy["agents"]["external"]["customer_ids"] = ["2"]
        gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Narrowed mandate"))
        result = rpc(client, bearer(token), "tools/call", {"name": "find_offers", "arguments": {
            "need": need.model_dump(mode="json")}}).json()["result"]
        assert result["isError"] and result["structuredContent"]["code"] == "CUSTOMER_NOT_IN_MANDATE"
        del policy["agents"]["external"]
        gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Removed mandate"))
        assert gateway.oauth.resolve(token["access_token"], RESOURCE).customer_ids == ()


@pytest.mark.parametrize("case", ["mismatch", "redirect", "private", "too_large", "compressed", "timeout", "malformed", "missing_name", "private_dns"])
def test_r9_cimd_metadata_validation_and_bounded_fetch(gateway, case):
    gateway.settings.public_url = ORIGIN
    gateway.oauth.resolver = lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (
        "127.0.0.1" if case == "private_dns" else "93.184.216.34", 443))]
    value = {"client_id": CIMD, **metadata()}
    if case == "mismatch":
        value["client_id"] += "/other"
    if case == "missing_name":
        del value["client_name"]
    with respx.mock(assert_all_called=False) as mock, TestClient(create_app(gateway), base_url=ORIGIN) as client:
        route = mock.get("https://93.184.216.34/oauth/metadata.json")
        if case == "redirect":
            route.respond(302, headers={"location": "https://evil.test/metadata"})
        elif case == "too_large":
            route.respond(200, content=b" " * 70_000, headers={"content-type": "application/json"})
        elif case == "compressed":
            route.respond(200, content=gzip.compress(b" " * 70_000),
                headers={"content-type": "application/json", "content-encoding": "gzip"})
        elif case == "timeout":
            route.mock(side_effect=httpx.ReadTimeout("sensitive upstream text"))
        elif case == "malformed":
            route.respond(200, content=b"oops", headers={"content-type": "application/json"})
        else:
            route.respond(200, json=value)
        response = client.get("/oauth/authorize", params={"client_id": "https://127.0.0.1/meta" if case == "private" else CIMD,
            "redirect_uri": CALLBACK, "response_type": "code", "resource": RESOURCE,
            "code_challenge_method": "S256", "code_challenge": oauth.challenge(VERIFIER)})
        assert response.status_code == 400 and response.json() == {"error": "invalid_client"}


@pytest.mark.parametrize("changes", [{"code_challenge_method": "plain"}, {"code_challenge": "bad"},
    {"resource": ORIGIN + "/a2a"}, {"resource": ""}, {"redirect_uri": CALLBACK + "/"},
    {"scope": "mcp admin"}, {"response_type": "token"}])
def test_r9_reject_unsupported_authorization_parameters_without_redirect(gateway, changes):
    gateway.settings.public_url = ORIGIN
    with TestClient(create_app(gateway), base_url=ORIGIN) as client:
        client_id = register(client)["client_id"]
        response = client.get("/oauth/authorize", params={"client_id": client_id, "redirect_uri": CALLBACK,
            "response_type": "code", "code_challenge_method": "S256", "code_challenge": oauth.challenge(VERIFIER),
            "resource": RESOURCE, **changes}, follow_redirects=False)
        if "redirect_uri" in changes:
            assert response.status_code == 400 and "location" not in response.headers
        else:
            assert response.status_code == 303
            query = parse_qs(urlsplit(response.headers["location"]).query)
            assert "error" in query and query["iss"] == [ORIGIN] and "code" not in query


def test_r9_refresh_scope_narrowing_and_concurrent_reuse(gateway, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    gateway.settings.public_url = ORIGIN
    with TestClient(create_app(gateway), base_url=ORIGIN) as client:
        client_id = register(client)["client_id"]
        body, _, _ = grant(client, client_id)
        token = exchange(client, body)
        narrowed = refresh(client, client_id, token, scope="mcp").json()
        assert narrowed["scope"] == "mcp"
        assert refresh(client, client_id, narrowed, scope="mcp offline_access").json() == {"error": "invalid_scope"}
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: refresh(client, client_id, narrowed), range(2)))
        assert [r.status_code for r in responses] == [200, 200]
        issued = responses[0].json()
        assert issued == responses[1].json()
        assert rpc(client, bearer(issued)).status_code == 200
        moment = gateway.connection_trust.clock() + timedelta(seconds=31)
        monkeypatch.setattr(gateway.connection_trust, "clock", lambda: moment)
        assert refresh(client, client_id, narrowed).status_code == 400
        assert rpc(client, bearer(issued)).status_code == 401


@pytest.mark.parametrize("expired", ["request", "code"])
def test_r10_expired_consent_and_codes_cannot_issue_tokens(gateway, monkeypatch, expired):
    gateway.settings.public_url = ORIGIN
    with TestClient(create_app(gateway), base_url=ORIGIN) as client:
        client_id = register(client)["client_id"]
        if expired == "request":
            request_id, _ = start(client, client_id)
        else:
            body, _, _ = grant(client, client_id)
        future = oauth.now() + timedelta(minutes=11)
        monkeypatch.setattr(oauth, "now", lambda: future)
        if expired == "request":
            assert consent(client, request_id).status_code == 410
        else:
            refused = client.post("/oauth/token", data=body)
            assert refused.status_code == 400 and refused.json()["error"] == "invalid_grant"


@pytest.mark.parametrize("method", ["client_secret_basic", "client_secret_post"])
def test_r9_confidential_clients_authenticate_at_token_endpoint(gateway, method):
    gateway.settings.public_url = ORIGIN
    with TestClient(create_app(gateway), base_url=ORIGIN) as client:
        registered = register(client, token_endpoint_auth_method=method)
        body, _, _ = grant(client, registered["client_id"])
        assert client.post("/oauth/token", data=body).status_code == 401
        if method == "client_secret_basic":
            encoded = base64.b64encode((registered["client_id"] + ":" + registered["client_secret"]).encode()).decode()
            response = client.post("/oauth/token", data=body, headers={"Authorization": "Basic " + encoded})
        else:
            response = client.post("/oauth/token", data={**body, "client_secret": registered["client_secret"]})
        assert response.status_code == 200
        assert rpc(client, bearer(response.json())).status_code == 200


def test_r13_signed_only_agent_keeps_mandate_without_reviving_expired_authority(gateway, need, upstream, vc_issuer, monkeypatch):
    from app import audit
    gateway.settings.public_url = ORIGIN
    signed = issue_mandate(vc_issuer)
    with TestClient(create_app(gateway), base_url=ORIGIN) as client:
        result = client.post("/v1/offers", headers={**agent_headers(AGENT), "Agent-Mandate": signed},
                             json=need.model_dump(mode="json"))
        assert result.status_code == 200
        client_id = register(client)["client_id"]
        body, _, _ = grant(client, client_id, agent_id=AGENT)
        token = exchange(client, body)
        assert AGENT not in gateway.policy.get().agents
        assert gateway.oauth.resolve(token["access_token"], RESOURCE).customer_ids == ("1",)
        lookup = rpc(client, {**bearer(token), "Agent-Mandate": signed}, "tools/call", {
            "name": "find_offers", "arguments": {"need": need.model_dump(mode="json")}}).json()["result"]
        assert not lookup.get("isError") and lookup["structuredContent"]["agent_id"] == AGENT
        future = audit.utcnow() + timedelta(days=2)
        monkeypatch.setattr(audit, "utcnow", lambda: future)
        assert gateway.oauth.resolve(token["access_token"], RESOURCE).customer_ids == ()
