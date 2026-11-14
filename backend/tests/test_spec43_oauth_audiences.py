"""Spec 43 part 2: OAuth and all non-REST audiences through real transports."""
import base64
import hashlib
import json
import re
import shlex
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app import audit
from app.accounts import digest
from app.auth import Principal
from app.foundation import PRINCIPAL
from app.models import PolicyUpdateRequest
from app.webhooks import verify_webhook
from conftest import agent_headers, demo_catalog, OPERATOR_HEADERS
from test_spec43_accounts_connections import http, sign, connect, bearer, pending, PASSWORD
from test_webhooks import receiver

CLIENT_ID = "https://assistant.example/client.json"
REDIRECT = "https://assistant.example/callback"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


def rpc(http, credential, path="/mcp", method="tools/list", params=None, version="2026-07-28"):
    params = {**(params or {})}
    if version == "2026-07-28":
        params["_meta"] = {"io.modelcontextprotocol/protocolVersion": version,
                           "io.modelcontextprotocol/clientCapabilities": {"elicitation": {"form": {}}}}
    return http.post(path, headers={**credential, "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": version,
                                   "MCP-Method": method, **({"MCP-Name": params["name"]} if "name" in params else {})},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})


def tool(http, credential, name, arguments=None, path="/mcp", **retry):
    response = rpc(http, credential, path, "tools/call", {"name": name, "arguments": arguments or {}, **retry})
    assert response.status_code == 200, response.text
    value = response.json()
    assert "result" in value, value
    return value["result"]


def authorize(http, respx_mock, *, path="/mcp", human=None, client_id=CLIENT_ID, scopes="*", agent="hotel-chain-agent", approve=True, customers=None):
    if client_id.startswith("https:"):
        respx_mock.get(client_id).mock(return_value=httpx.Response(200, json={"client_id": client_id,
            "client_name": "Procurement Assistant", "redirect_uris": [REDIRECT]}))
    human = human or sign(http)
    response = http.get("/oauth/authorize", headers=human, params={"client_id": client_id, "redirect_uri": REDIRECT,
        "resource": "http://localhost"+path, "code_challenge": CHALLENGE, "code_challenge_method": "S256", "response_type": "code", "scope": scopes, "state": "state-43"}, follow_redirects=False)
    assert response.status_code == 200, response.text
    request_id = re.search(r'action="/oauth/requests/([^/]+)/decision"', response.text)[1]
    view = http.get("/v1/oauth/requests/"+request_id, headers=human)
    assert view.status_code == 200 and view.json()["redirect_host"] == "assistant.example"
    decision = http.post("/v1/oauth/requests/"+request_id+"/decision", headers=human, json={"approve": approve, "agent_id": agent,
        **({"customer_ids": customers} if customers else {})})
    assert decision.status_code == 200, decision.text
    values = parse_qs(urlsplit(decision.json()["redirect_url"]).query)
    assert values["iss"] == ["http://localhost"] and values["state"] == ["state-43"]
    return values, decision.json(), human


def exchange(http, values, *, path="/mcp", client_id=CLIENT_ID, **extra):
    return http.post("/oauth/token", data={"grant_type": "authorization_code", "code": values["code"][0],
        "client_id": client_id, "redirect_uri": REDIRECT, "resource": "http://localhost"+path, "code_verifier": VERIFIER, **extra})


def oauth(http, respx_mock, *, path="/mcp", **kwargs):
    values, connection, human = authorize(http, respx_mock, path=path, **kwargs)
    response = exchange(http, values, path=path, client_id=kwargs.get("client_id", CLIENT_ID))
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer "+response.json()["access_token"]}, response.json(), connection, human


def human_key(http, email="chef@aurora.example", audience="tenant", **extra):
    # Tenant admin issues keys bound to itself; lower roles use their own OAuth
    # consent so tests exercise the actual human grant boundary.
    return bearer(connect(http, sign(http, email), audience=audience, **extra))


@pytest.mark.parametrize("registration", ["metadata", "dynamic"])
def test_AC_6_discovery_metadata_pkce_consent_mcp_purchase(http, gateway, need, upstream, tmp_path, respx_mock, registration):
    demo_catalog(gateway, tmp_path, upstream)
    denied = rpc(http, {}, method="ping")
    assert denied.status_code == 401
    assert '/.well-known/oauth-protected-resource/mcp' in denied.headers["www-authenticate"]
    for path in ["", "/mcp", "/mcp/tenant", "/mcp/operator", "/a2a", "/a2a/tenant", "/a2a/operator"]:
        metadata = http.get("/.well-known/oauth-protected-resource"+path)
        assert metadata.status_code == 200, metadata.text
        assert metadata.json()["resource"] == "http://localhost"+(path or "/mcp")
    meta = http.get("/.well-known/oauth-authorization-server").json()
    assert meta["code_challenge_methods_supported"] == ["S256"] and meta["client_id_metadata_document_supported"]
    client_id = CLIENT_ID
    if registration == "dynamic":
        response = http.post('/oauth/register', json={"client_name": "Purchase Assistant", "application_type": "web", "redirect_uris": [REDIRECT]})
        assert response.status_code == 201
        client_id = response.json()["client_id"]
    credential, tokens, connection, admin = oauth(http, respx_mock, client_id=client_id)
    import asyncio
    verified = asyncio.run(gateway.oauth.verify_token(tokens["access_token"]))
    assert verified.client_id == client_id and verified.resource == "http://localhost/mcp" and verified.expires_at > audit.utcnow().timestamp()
    assert tokens["expires_in"] == 3600
    listed = http.get("/v1/tenant/connections", headers=admin).json()
    assert next(c for c in listed if c["connection_id"] == connection["connection_id"])["type"] == "oauth"
    assert all("key" not in c for c in listed)
    for version in ["2026-07-28", "2025-11-25"]:
        response = rpc(http, credential, version=version)
        assert response.status_code == 200, response.text
        assert "find_offers" in {t["name"] for t in response.json()["result"]["tools"]}
    lookup = tool(http, credential, "find_offers", {"need": need.model_dump(mode="json")})["structuredContent"]
    assert lookup["offers"][0]["product_id"] == "464306"
    quote = tool(http, credential, "create_quote", {"customer_id": "1", "deliver_by": str(need.deliver_by), "need_ref": lookup["lookup_id"],
        "lines": [{"product_id": "464306", "quantity_sales_units": 1000}]})["structuredContent"]
    order = tool(http, credential, "place_order", {"quote_id": quote["id"], "idempotency_key": "oauth-demo"})["structuredContent"]
    assert (order["total"], order["status"], order["agent_id"]) == (1010, "CONFIRMED", "hotel-chain-agent")
    assert http.delete("/v1/tenant/connections/"+connection["connection_id"], headers=admin).status_code == 200
    assert rpc(http, credential).status_code == 401


def test_AC_7_token_binding_rotation_reuse_and_expiry(http, gateway, respx_mock, monkeypatch):
    values, _, _ = authorize(http, respx_mock)
    for extra in [{"code_verifier": "x"*64}, {"redirect_uri": REDIRECT+"/wrong"}, {"resource": "http://localhost/a2a"}]:
        response = exchange(http, values, **extra)
        assert response.status_code == 400 and response.json()["error"] == "invalid_grant"
    result = exchange(http, values)
    assert result.status_code == 200, result.text
    tokens = result.json()
    assert exchange(http, values).status_code == 400
    credential = {"Authorization": "Bearer "+tokens["access_token"]}
    assert rpc(http, credential, "/mcp/tenant").status_code == 401
    assert http.post('/a2a', headers=credential, json={}).status_code == 401
    assert http.get("/v1/orders", headers=credential).status_code == 401
    refresh = {"grant_type": "refresh_token", "client_id": CLIENT_ID, "resource": "http://localhost/mcp", "refresh_token": tokens["refresh_token"]}
    rotated = http.post("/oauth/token", data=refresh)
    assert rotated.status_code == 200
    assert http.post("/oauth/token", data=refresh).status_code == 400
    for token in [tokens["access_token"], rotated.json()["access_token"]]:
        assert rpc(http, {"Authorization": "Bearer "+token}).status_code == 401
    credential, _, _, _ = oauth(http, respx_mock)
    now = audit.utcnow()
    monkeypatch.setattr(audit, "utcnow", lambda: now+timedelta(hours=2))
    assert rpc(http, credential).status_code == 401


def test_AC_8_dcr_and_safe_metadata_registration(http, gateway, respx_mock, monkeypatch):
    body = {"client_name": "Registered Assistant", "redirect_uris": [REDIRECT]}
    assert http.post("/oauth/register", json=body).status_code == 400
    response = http.post("/oauth/register", json={**body, "application_type": "web"})
    assert response.status_code == 201, response.text
    credential, _, _, _ = oauth(http, respx_mock, client_id=response.json()["client_id"])
    assert rpc(http, credential).status_code == 200
    gateway.oauth.register("pre-registered", {**body, "application_type": "web"})
    credential, _, _, _ = oauth(http, respx_mock, client_id="pre-registered")
    assert rpc(http, credential).status_code == 200
    for url in ["http://assistant.example/client.json", "https://127.0.0.1/client.json"]:
        response = http.get("/oauth/authorize", headers=sign(http), params={"client_id": url, "redirect_uri": REDIRECT,
            "resource": "http://localhost/mcp", "code_challenge": CHALLENGE, "code_challenge_method": "S256"})
        assert response.status_code == 400
    monkeypatch.setenv("FRONTEND_CONSENT_URL", "http://localhost:5173/consent")
    admin = sign(http)
    response = http.get("/oauth/authorize", headers=admin, params={"client_id": "pre-registered", "redirect_uri": REDIRECT,
        "resource": "http://localhost/mcp", "code_challenge": CHALLENGE, "code_challenge_method": "S256"}, follow_redirects=False)
    assert response.status_code == 302
    request_id = parse_qs(urlsplit(response.headers["location"]).query)["request_id"][0]
    assert http.get('/v1/oauth/requests/'+request_id, headers=sign(http, "manager@kaari.example")).status_code == 404
    other_key = connect(http, sign(http, "manager@kaari.example"), agent="hospital-kitchen-agent", customer_ids=["2"])
    assert http.get('/oauth/requests/'+request_id, headers=bearer(other_key)).status_code == 403
    assert http.post('/oauth/requests/'+request_id+'/decision', headers=bearer(other_key), json={"approve": False}).status_code == 403
    assert http.post('/v1/oauth/requests/'+request_id+'/decision', headers={"Cookie": admin["Cookie"]}, json={"approve": False}).status_code == 403
    denied = http.post('/v1/oauth/requests/'+request_id+'/decision', headers=admin, json={"approve": False})
    assert denied.status_code == 200 and 'access_denied' in denied.json()["redirect_url"]
    assert http.get('/v1/oauth/requests/'+request_id, headers=admin).status_code == 410
    for payload in [{"client_id": "wrong"}, {"client_id": CLIENT_ID, **body, "padding": "x"*65536}]:
        respx_mock.get(CLIENT_ID).mock(return_value=httpx.Response(200, json=payload))
        response = http.get("/oauth/authorize", headers=sign(http), params={"client_id": CLIENT_ID, "redirect_uri": REDIRECT,
            "resource": "http://localhost/mcp", "code_challenge": CHALLENGE, "code_challenge_method": "S256"})
        assert response.status_code == 400


def test_AC_9_instructions_snippets_and_all_clients(http, gateway, need, upstream):
    admin = sign(http)
    connection = connect(http, admin)
    catalog = http.get("/v1/connect/catalog").json()
    for client in {c for t in catalog["types"] for c in t["clients"]}:
        response = http.get('/v1/tenant/connections/'+connection["connection_id"]+'/instructions', headers=admin, params={"client": client})
        assert response.status_code == 200 and response.json()["snippet"], response.text
    setup = http.get('/v1/tenant/connections/'+connection["connection_id"]+'/instructions?client=curl', headers=admin).json()
    args = shlex.split(setup["snippet"])
    lookup = http.post(urlsplit(args[3]).path, headers=bearer(connection), json=json.loads(args[args.index('--data')+1]))
    assert lookup.status_code == 200, lookup.text
    setup = http.get('/v1/tenant/connections/'+connection["connection_id"]+'/instructions?client=python', headers=admin).json()
    # Execute the delivered Python snippet with the real in-process client's transport.
    class Env:
        environ = {"VTG_CONNECTION_KEY": connection["key"]}
    class Httpx:
        @staticmethod
        def Client(base_url, headers):
            class Client:
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def post(self, path, **kw): return http.post(base_url+path, headers=headers, **kw)
            return Client()
    code = setup["snippet"].replace("import os, httpx", "")
    exec(code, {"os": Env, "httpx": Httpx, "print": lambda *a: None})
    setup = http.get('/v1/tenant/connections/'+connection["connection_id"]+'/instructions?client=mcp_other', headers=admin).json()
    config = json.loads(setup["snippet"])["mcpServers"][connection["connection_id"]]
    headers = {name: value.replace("${VTG_CONNECTION_KEY}", connection["key"]) for name, value in config["headers"].items()}
    value = tool(http, headers, "find_offers", {"need": need.model_dump(mode="json")}, path=urlsplit(config["url"]).path)
    assert value["structuredContent"]["lookup_id"]


def a2a(http, credential, command, arguments=None, audience="tenant", task_id=None):
    from a2a import types as t
    from google.protobuf.json_format import MessageToDict, ParseDict
    message = t.Message(message_id=uuid4().hex, role=t.Role.ROLE_USER, task_id=task_id or "")
    ParseDict({"data": {"version": 1, "command": command, "arguments": arguments or {}}}, message.parts.add())
    return http.post("/a2a/"+audience, headers={**credential, "A2A-Version": "1.0"}, json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": MessageToDict(t.SendMessageRequest(message=message))})


@pytest.mark.parametrize("scenario", ["approve", "evidence_changed", "expired"])
def test_AC_10_a2a_card_security_oauth_tenant_isolation_two_step(http, gateway, need, upstream, respx_mock, monkeypatch, receiver, scenario):
    _, _, order = pending(http, need)
    for path in ["/.well-known/agent-card.json", "/a2a/tenant/.well-known/agent-card.json", "/a2a/operator/.well-known/agent-card.json"]:
        card = http.get(path).json()
        assert set(card["securitySchemes"]) == {"BearerAuth", "OAuth2"}, card
    b, _, _, _ = oauth(http, respx_mock, path="/a2a/tenant", human=sign(http, "dietitian@kaari.example"))
    response = a2a(http, b, "approve_order", {"order_id": order["id"]})
    assert "error" in response.json() and "ORDER_NOT_FOUND" in response.text, response.text
    a, _, _, _ = oauth(http, respx_mock, path="/a2a/tenant", human=sign(http, "chef@aurora.example"))
    first = a2a(http, a, "approve_order", {"order_id": order["id"]})
    assert "result" in first.json(), first.text
    assert "INPUT_REQUIRED" in first.text
    preview = first.json()["result"]["task"]["artifacts"][0]["parts"][0]["data"]
    assert preview["confirmation_required"] and preview["summary"]["order"]["id"] == order["id"]
    if scenario == "evidence_changed":
        with gateway.db.connect(write=True) as conn:
            changed = gateway.orders._order(conn, order["id"])
            changed.evidence_revision += 1
            gateway.orders._save_order(conn, changed)
    elif scenario == "expired":
        now = audit.utcnow()
        monkeypatch.setattr(audit, "utcnow", lambda: now+timedelta(minutes=6))
    arguments = {"order_id": order["id"], "confirmation_token": preview["confirmation_token"]}
    confirmed = a2a(http, a, "approve_order", arguments, task_id=first.json()["result"]["task"]["id"])
    if scenario != "approve":
        assert ("EVIDENCE_CHANGED" if scenario == "evidence_changed" else "CONFIRMATION_INVALID") in confirmed.text, confirmed.text
        assert http.get('/v1/orders/'+order['id'], headers=OPERATOR_HEADERS).json()["status"] == "PENDING_APPROVAL"
        return
    assert "CONFIRMED" in confirmed.text, confirmed.text
    assert http.get('/v1/orders/'+order['id'], headers=OPERATOR_HEADERS).json()["approver_user_id"] == "aurora-chef"
    assert "CONFIRMATION_INVALID" in a2a(http, a, "approve_order", arguments).text
    # Expanding the webhook event registry must preserve buyer A2A push: buyer
    # subscriptions never request the new operator-only platform event types.
    from a2a import types as t
    from test_a2a_service import command
    from google.protobuf.json_format import MessageToDict
    buyer_oauth, _, _, _ = oauth(http, respx_mock, path="/a2a")
    response = http.post('/a2a', headers={**buyer_oauth, 'A2A-Version': '1.0'}, json={'jsonrpc': '2.0', 'id': 1, 'method': 'SendMessage',
        'params': MessageToDict(command('lookup', 'spec43-a2a-buyer-oauth', need=need.model_dump(mode='json')))})
    assert response.status_code == 200 and 'result' in response.json(), response.text
    buyer = gateway.accounts.bind(gateway.settings.keys.resolve(agent_headers("external")["Authorization"].split()[1], gateway.policy.get()))
    token = PRINCIPAL.set(buyer)
    try:
        task = gateway.a2a.send(command("lookup", "spec43-buyer-push", need=need.model_dump(mode="json")), buyer)
        config = gateway.a2a.create_push(t.TaskPushNotificationConfig(task_id=task.id, url="https://receiver.test/hook"), buyer)
        assert config.task_id == task.id
    finally:
        PRINCIPAL.reset(token)


def test_AC_14_mcp_operator_inbox_audience_and_human_gates(http, gateway, need, upstream, respx_mock):
    _, _, order = pending(http, need)
    bkey = connect(http, sign(http, "manager@kaari.example"), agent="hospital-kitchen-agent", customer_ids=["2"])
    _, _, hospital_order = pending(http, need.model_copy(update={"customer_id": "2"}), bearer(bkey))
    credential, _, _, _ = oauth(http, respx_mock, path="/mcp/operator", human=sign(http, "staff@valio.example"))
    result = tool(http, credential, "list_approvals", path="/mcp/operator")
    assert order["id"] in json.dumps(result)
    assert hospital_order["id"] in json.dumps(result)
    buyer, _, _, _ = oauth(http, respx_mock)
    assert rpc(http, buyer, "/mcp/operator").status_code == 401
    tenant, _, _, _ = oauth(http, respx_mock, path="/mcp/tenant")
    assert rpc(http, tenant, "/mcp/operator").status_code == 401
    assert rpc(http, agent_headers("external"), "/mcp/operator").status_code == 403
    assert [event.tenant_id for event in gateway.audit() if event.action == 'CONSENT_APPROVED' and event.actor == 'valio-admin'] == ['platform']
    from app.mcp_server import create_server
    import asyncio
    platform = connect(http, sign(http, "staff@valio.example"), audience="operator")
    local = create_server(gateway, audience="operator")
    local.local_credential = platform["key"]
    value = asyncio.run(local.call_tool("list_approvals", {}))
    assert order["id"] in json.dumps(value.structured_content) and hospital_order["id"] in json.dumps(value.structured_content)
    local.local_credential = agent_headers("external")["Authorization"].split()[1]
    assert asyncio.run(local.call_tool("list_approvals", {})).is_error
    assert http.delete('/v1/operator/connections/'+platform['connection_id'], headers=sign(http, 'staff@valio.example')).status_code == 200
    local.local_credential = platform["key"]
    assert asyncio.run(local.call_tool("list_approvals", {})).structured_content["status"] == 401


@pytest.mark.parametrize("scenario", ["approve", "reject", "cancel", "evidence_changed", "expired", "declined", "policy"])
def test_AC_15_mcp_tenant_multiround_decision_evidence_and_viewer(http, gateway, need, upstream, respx_mock, monkeypatch, scenario):
    _, _, order = pending(http, need)
    bkey = connect(http, sign(http, "manager@kaari.example"), agent="hospital-kitchen-agent", customer_ids=["2"])
    _, _, hospital_order = pending(http, need.model_copy(update={"customer_id": "2"}), bearer(bkey))
    credential, _, _, _ = oauth(http, respx_mock, path="/mcp/tenant", human=sign(http, "chef@aurora.example"))
    result = tool(http, credential, "list_orders", path="/mcp/tenant")
    assert order["id"] in json.dumps(result)
    assert hospital_order["id"] not in json.dumps(result)
    args = {"order_id": order["id"]}
    decision = "reject_order" if scenario == "reject" else "approve_order"
    first = tool(http, credential, decision, args, path="/mcp/tenant")
    assert first["resultType"] == "input_required" and order["id"] in first["inputRequests"]["decision"]["params"]["message"]
    assert http.get('/v1/orders/'+order['id'], headers=OPERATOR_HEADERS).json()["status"] == "PENDING_APPROVAL"
    retry = {"requestState": first["requestState"], "inputResponses": {"decision": {"action": "accept", "content": {"confirm": True}}}}
    if scenario == "evidence_changed":
        with gateway.db.connect(write=True) as conn:
            changed = gateway.orders._order(conn, order["id"])
            changed.evidence_revision += 1
            gateway.orders._save_order(conn, changed)
    elif scenario == "expired":
        now = audit.utcnow()
        monkeypatch.setattr(audit, "utcnow", lambda: now+timedelta(minutes=6))
    elif scenario == "declined":
        retry["inputResponses"]["decision"] = {"action": "decline"}
    final = tool(http, credential, decision, args, path="/mcp/tenant", **retry)
    if scenario in {"evidence_changed", "expired", "declined"}:
        assert final["isError"]
        assert final["structuredContent"]["code"] == {"evidence_changed": "EVIDENCE_CHANGED", "expired": "CONFIRMATION_INVALID", "declined": "CONFIRMATION_DECLINED"}[scenario]
        assert http.get('/v1/orders/'+order['id'], headers=OPERATOR_HEADERS).json()["status"] == "PENDING_APPROVAL"
        return
    assert final["structuredContent"]["status"] == ("REJECTED" if scenario == "reject" else "CONFIRMED"), final
    assert final["structuredContent"]["approver_user_id"] == "aurora-chef"
    assert tool(http, credential, decision, args, path="/mcp/tenant", **retry)["isError"]
    viewer, _, _, _ = oauth(http, respx_mock, path="/mcp/tenant", human=sign(http, "finance@aurora.example"))
    denied = tool(http, viewer, "approve_order", args, path="/mcp/tenant")
    assert denied["isError"] and denied["structuredContent"]["status"] == 403
    if scenario == "cancel":
        first = tool(http, credential, "cancel_order", args, path="/mcp/tenant")
        assert first["resultType"] == "input_required"
        final = tool(http, credential, "cancel_order", args, path="/mcp/tenant", requestState=first["requestState"],
            inputResponses={"decision": {"action": "accept", "content": {"confirm": True}}})
        assert final["structuredContent"]["status"] == "CANCELLED", final
    if scenario == "policy":
        admin, _, _, _ = oauth(http, respx_mock, path="/mcp/tenant")
        changes = {"approval": {"max_auto_total_eur": 1}}
        first = tool(http, admin, "set_policy", changes, path="/mcp/tenant")
        assert first["resultType"] == "input_required", first
        final = tool(http, admin, "set_policy", changes, path="/mcp/tenant", requestState=first["requestState"],
            inputResponses={"decision": {"action": "accept", "content": {"confirm": True}}})
        assert final["structuredContent"]["approval"]["max_auto_total_eur"] == 1, final


@pytest.mark.parametrize("kind", ["a2a", "webhook", "tool_pack"])
def test_AC_16_non_rest_audiences(http, gateway, need, upstream, respx_mock, receiver, kind):
    _, _, order = pending(http, need)
    bkey = connect(http, sign(http, "manager@kaari.example"), agent="hospital-kitchen-agent", customer_ids=["2"])
    _, _, hospital_order = pending(http, need.model_copy(update={"customer_id": "2"}), bearer(bkey))
    admin = sign(http)
    tenant = bearer(connect(http, admin, audience="tenant", type=kind, **({"webhook_url": "https://receiver.test/hook"} if kind == "webhook" else {})))
    platform = sign(http, "staff@valio.example")
    operator = bearer(connect(http, platform, audience="operator", type=kind, **({"webhook_url": "https://receiver.test/hook"} if kind == "webhook" else {})))
    if kind == "a2a":
        tenant_data = a2a(http, tenant, "list_orders").text
        operator_data = a2a(http, operator, "list_orders", audience="operator").text
        assert order["id"] in tenant_data and hospital_order["id"] not in tenant_data
        assert order["id"] in operator_data and hospital_order["id"] in operator_data
        assert "HUMAN_REQUIRED" in a2a(http, agent_headers("external"), "list_orders").text
        assert "HUMAN_REQUIRED" in a2a(http, agent_headers("external"), "list_orders", audience="operator").text
        assert "WRONG_AUDIENCE" in a2a(http, tenant, "list_orders", audience="operator").text
    elif kind == "webhook":
        assert len(http.get("/v1/tenant/webhook-subscriptions", headers=tenant).json()) == 1
        assert len(http.get("/v1/operator/webhook-subscriptions", headers=operator).json()) == 2
        assert http.get("/v1/tenant/webhook-subscriptions", headers=agent_headers("external")).status_code == 403
        assert http.get("/v1/operator/webhook-subscriptions", headers=agent_headers("external")).status_code == 403
        assert http.get("/v1/operator/webhook-subscriptions", headers=tenant).status_code == 401
    else:
        for format in ["openai", "anthropic", "gemini"]:
            response = http.get('/v1/connect/tool-pack', params={"audience": "tenant", "format": format}, headers=tenant)
            assert response.status_code == 200 and response.json()["declarations"], response.text
            assert "list_orders" in response.json()["tool_schemas"] and "list_tenants" not in response.json()["tool_schemas"]
        assert http.get('/v1/connect/tool-pack?audience=operator', headers=operator).status_code == 200
        assert http.get('/v1/connect/tool-pack?audience=tenant', headers=agent_headers("external")).status_code == 403
        assert http.get('/v1/connect/tool-pack?audience=operator', headers=agent_headers("external")).status_code == 403
        assert http.get('/v1/connect/tool-pack?audience=operator', headers=tenant).status_code == 401
        data = http.post('/v1/connect/tools/list_orders?audience=tenant', headers=tenant, json={})
        assert data.status_code == 200 and order["id"] in data.text and hospital_order["id"] not in data.text
        data = http.post('/v1/connect/tools/list_orders?audience=operator', headers=operator, json={})
        assert data.status_code == 200 and order["id"] in data.text and hospital_order["id"] in data.text
        assert http.post('/v1/connect/tools/list_orders?audience=tenant', headers=agent_headers("external"), json={}).status_code == 403
        for audience, credential, expected in [("tenant", tenant, {order["id"]}), ("operator", operator, {order["id"], hospital_order["id"]})]:
            pack = http.get('/v1/connect/tool-pack', params={"audience": audience}, headers=credential).json()
            from types import SimpleNamespace
            class Client:
                def __init__(self, base_url, headers): self.base_url, self.headers = base_url, headers
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def post(self, path, **kwargs): return http.post(self.base_url+path, headers=self.headers, **kwargs)
            namespace = {"os": SimpleNamespace(environ={"VTG_CONNECTION_KEY": credential["Authorization"].split()[1]}),
                         "httpx": SimpleNamespace(Client=Client)}
            exec(pack["executor"].replace("import os, httpx", ""), namespace)
            assert {o["id"] for o in namespace["execute"]("list_orders", {})} == expected


@pytest.mark.parametrize("scenario", ["delivery", "retry"])
def test_AC_17_operator_and_tenant_webhook_delivery(http, gateway, need, upstream, receiver, monkeypatch, scenario):
    if scenario == "retry":
        from test_webhooks import test_review_retry_test_rejects_constant_delay
        test_review_retry_test_rejects_constant_delay(gateway, need, upstream, receiver, monkeypatch)
        return
    a = sign(http)
    tenant = http.post("/v1/tenant/webhook-subscriptions", headers=a, json={"url": "https://receiver.test/hook", "customer_ids": ["1"], "event_types": ["order.approval_required"]})
    assert tenant.status_code == 200, tenant.text
    op = http.post("/v1/operator/webhook-subscriptions", headers=sign(http, "staff@valio.example"), json={"url": "https://receiver.test/hook", "customer_ids": ["1"], "event_types": ["order.approval_required", "platform.upstream_rate_limited"]})
    assert op.status_code == 200, op.text
    pending(http, need)
    key = connect(http, sign(http, "manager@kaari.example"), agent="hospital-kitchen-agent", customer_ids=["2"])
    pending(http, need.model_copy(update={"customer_id": "2"}), bearer(key))
    for user_id in ['aurora-director', 'kaari-manager']:
        token = PRINCIPAL.set(gateway.accounts.user_principal(gateway.accounts.user(user_id)))
        try:
            with gateway.db.connect(write=True) as conn:
                audit.append(conn, "system", "UPSTREAM_RATE_LIMITED", "Fixture upstream block.", data={"retry_after_seconds": 30})
        finally:
            PRINCIPAL.reset(token)
    gateway.webhooks.dispatch()
    bodies = [json.loads(r.content) for r in receiver["calls"]]
    assert len(bodies) == 5, bodies
    assert sum(b.get("customer_id") == "1" for b in bodies) == 2
    assert sum(b.get("customer_id") == "2" for b in bodies) == 1
    assert sum(b["type"] == "platform.upstream_rate_limited" for b in bodies) == 2
    with gateway.db.connect() as conn:
        assert [r[0] for r in conn.execute("SELECT tenant_id FROM events WHERE type='platform.upstream_rate_limited'")] == ['platform', 'platform']
    assert all(verify_webhook(r.content, r.headers, gateway.signing.jwks()) for r in receiver["calls"])


def test_AC_6_AC_7_operator_and_human_oauth_grants_coexist(http, gateway, respx_mock):
    """Both consent implementations share URLs, never their incompatible SQL rows."""
    gateway.settings.public_url = "http://localhost"
    registration = http.post("/oauth/register", json={
        "client_name": "Operator connector", "application_type": "web",
        "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
    })
    assert registration.status_code == 201, registration.text
    client_id = registration.json()["client_id"]
    # An unrelated human session must not change the legacy explicit mcp flow.
    started = http.get("/oauth/authorize", headers=sign(http), params={
        "client_id": client_id, "redirect_uri": REDIRECT, "resource": "http://localhost/mcp",
        "response_type": "code", "code_challenge": CHALLENGE,
        "code_challenge_method": "S256", "scope": "mcp offline_access",
    })
    assert started.status_code == 200, started.text
    operator_request = re.search(r'name="request_id" value="([^"]+)"', started.text)[1]
    approved = http.post("/v1/oauth/requests/"+operator_request+"/decision",
        headers=OPERATOR_HEADERS, json={"decision": "approve", "agent_id": "hotel-chain-agent", "customer_ids": ["1"]})
    assert approved.status_code == 200, approved.text
    values = parse_qs(urlsplit(approved.json()["redirect_url"]).query)
    operator_tokens = exchange(http, values, client_id=client_id)
    assert operator_tokens.status_code == 200, operator_tokens.text
    operator_headers = {"Authorization": "Bearer "+operator_tokens.json()["access_token"]}
    human_headers, human_tokens, human_connection, admin = oauth(http, respx_mock, path="/mcp/tenant")
    assert not rpc(http, operator_headers).json()["result"].get("isError")
    assert not rpc(http, human_headers, path="/mcp/tenant").json()["result"].get("isError")
    with gateway.db.connect() as conn:
        operator_columns = {row[1] for row in conn.execute("PRAGMA table_info(oauth_tokens)")}
        human_columns = {row[1] for row in conn.execute("PRAGMA table_info(audience_oauth_tokens)")}
        assert "family_id" in operator_columns and "family" in human_columns
        assert conn.execute("SELECT count(*) FROM oauth_tokens").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM audience_oauth_tokens").fetchone()[0] == 2
    revoked = http.delete("/v1/tenant/connections/"+human_connection["connection_id"], headers=admin)
    assert revoked.status_code == 200, revoked.text
    assert rpc(http, human_headers, path="/mcp/tenant").status_code == 401
    assert rpc(http, operator_headers).status_code == 200
