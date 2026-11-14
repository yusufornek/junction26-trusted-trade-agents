"""Spec 19: credentials, authority, ownership and secret-free provisioning."""
import asyncio
from hashlib import sha256
import json
import select
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from app.api import create_app
from app.auth import Credentials
from app.config import BACKEND, Settings
from app.mcp_server import create_server
from app.models import PolicyUpdateRequest
from conftest import AGENT_TOKENS, OPERATOR_HEADERS, OPERATOR_TOKEN, agent_headers, quote_request

pytestmark = [pytest.mark.usefixtures("registered_agents"),
              pytest.mark.parametrize("registered_agents", [("agent-a", "agent-b")], indirect=True)]


@pytest.fixture
def client(gateway):
    with TestClient(create_app(gateway)) as client:
        yield client


def objects(gateway, need, agent, *, qty=1, key=None):
    lookup = gateway.find_offers(need, f"agent:{agent}")
    quote = gateway.orders.create_quote(quote_request(need, qty=qty, need_ref=lookup.lookup_id), agent)
    order = gateway.orders.place_order(quote.id, key or quote.id, agent)
    return lookup, quote, order


@pytest.mark.parametrize("headers,status,code", [
    ({}, 401, "UNAUTHENTICATED"),
    ({"Authorization": "Bearer unknown"}, 401, "UNAUTHENTICATED"),
    (OPERATOR_HEADERS, 422, "VALIDATION_ERROR"),
    (agent_headers("agent-a"), 422, "VALIDATION_ERROR"),
])
def test_review_malformed_cancellation_authenticates_before_audit(client, gateway, headers, status, code):
    response = client.post("/v1/orders/missing/cancel", content="{", headers={**headers, "Content-Type": "application/json"})
    assert response.status_code == status
    assert response.json()["code"] == code
    if status == 401:
        assert response.headers["www-authenticate"] == "Bearer"
        assert not gateway.audit()
    elif headers == OPERATOR_HEADERS:
        assert gateway.audit()[-1].actor == "human:operator"
        assert gateway.audit()[-1].action == "CANCEL_FAILED"


@pytest.mark.parametrize("method,path,operator_only", [
    ("post", "/v1/offers", False),
    ("post", "/v1/quotes", False),
    ("post", "/v1/orders", False),
    ("post", "/v1/mandates/revocations", False),
    ("post", "/v1/orders/missing/cancel", False),
    ("post", "/v1/approvals/missing/approve", True),
    ("post", "/v1/approvals/missing/reject", True),
    ("put", "/v1/policy", True),
    ("post", "/v1/policy/reset", True),
])
@pytest.mark.parametrize("caller", ["missing", "unknown", "agent", "operator"])
def test_review_malformed_json_authentication(client, gateway, method, path, operator_only, caller):
    headers = {"missing": {}, "unknown": {"Authorization": "Bearer unknown"},
               "agent": agent_headers("agent-a"), "operator": OPERATOR_HEADERS}[caller]
    status, code = ((401, "UNAUTHENTICATED") if caller in {"missing", "unknown"} else
                    (403, "OPERATOR_REQUIRED") if operator_only and caller == "agent" else
                    (422, "VALIDATION_ERROR"))
    response = client.request(method, path, content="{", headers={**headers, "Content-Type": "application/json"})
    assert (response.status_code, response.json()["code"]) == (status, code)
    if status == 401:
        assert response.headers["www-authenticate"] == "Bearer"
    if status in {401, 403}:
        assert not gateway.audit()


def test_ac_1_credentials_and_startup(client, need, monkeypatch):
    # This exhaustive authorization matrix isolates auth from spec 24's separately tested source throttle.
    client.app.state.gateway.source_limiter.capacity = 10000
    for headers in ({}, {"Authorization": "Bearer unknown"}):
        response = client.post("/v1/offers", content=b'\xff', headers={**headers, "Content-Type": "application/json"})
        assert (response.status_code, response.json()["code"]) == (401, "UNAUTHENTICATED")
    for path, methods in client.app.openapi()["paths"].items():
        for method, operation in methods.items():
            if path in {"/health", "/.well-known/jwks.json", "/.well-known/agent-card.json",
                        "/.well-known/jwks-history.json", "/v1/meta",
                        "/mcp/server-card", "/.well-known/ai-catalog.json", "/developers", "/developers/tools.json", "/llms.txt", "/v1/connect/catalog", "/v1/auth/login",
                        "/v1/auth/invites/{token}/accept", "/v1/auth/password-resets/{token}/accept"}:
                continue
            assert {"BearerAuth": []} in operation["security"]
            url = path.replace("{id}", "missing").replace("{order_id}", "missing").replace("{lookup_id}", "missing")
            for headers in ({}, {"Authorization": "Bearer unknown"}, {"X-Agent-Id": "agent-a"},
                            {"Authorization": "Basic unknown"}):
                for payload in ({"json": {}}, {"content": "{", "headers": {**headers, "Content-Type": "application/json"}}):
                    response = client.request(method, url, **({"headers": headers} | payload))
                    assert response.status_code == 401, (method, path, response.text)
                    assert response.json()["code"] == "UNAUTHENTICATED"
                    assert response.headers["www-authenticate"] == "Bearer"
    for url in ("/health", "/docs", "/openapi.json", "/.well-known/jwks.json"):
        assert client.get(url).status_code == 200
    cors = client.options("/v1/offers", headers={"Origin": "http://localhost:5173",
        "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "Authorization"})
    assert cors.status_code == 200 and "authorization" in cors.headers["access-control-allow-headers"].lower()
    with pytest.raises(RuntimeError, match="gen_keys.py"):
        create_app(settings=Settings(keys=Credentials()))
    monkeypatch.setenv("OPERATOR_TOKEN", "")
    monkeypatch.setenv("AGENT_KEYS", "")
    with pytest.raises(RuntimeError, match="gen_keys.py"):
        create_app()


def test_ac_2_identity_and_budget(client, gateway, need, upstream):
    body = gateway.policy.get().model_dump(mode="json")
    body["agents"]["agent-a"]["max_auto_daily_total_eur"] = 20
    gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Fixture"))
    headers = {**agent_headers("agent-a"), "X-Agent-Id": "agent-b"}
    for index, expected in enumerate(("CONFIRMED", "PENDING_APPROVAL")):
        lookup = client.post("/v1/offers", json=need.model_dump(mode="json"), headers=headers).json()
        assert lookup["agent_id"] == "agent-a"
        quote = client.post("/v1/quotes", json=quote_request(need, qty=1, need_ref=lookup["lookup_id"]).model_dump(mode="json"), headers=headers).json()
        assert quote["agent_id"] == "agent-a"
        result = client.post("/v1/orders", json={"quote_id": quote["id"]},
                             headers={**headers, "Idempotency-Key": f"spoof-{index}"})
        assert result.status_code == 200, result.text
        order = result.json()
        assert order["agent_id"] == "agent-a" and order["status"] == expected
    assert "max_auto_daily_total_eur" in [h["rule"] for h in order["policy"]["hits"]]
    assert all(event.actor == "agent:agent-a" for event in gateway.audit() if event.action in {"LOOKUP", "CREATE_QUOTE", "PLACE_ORDER"})


def test_ac_3_operator_scope(client, gateway):
    policy = gateway.policy.get().model_dump(mode="json")
    calls = [("post", "/v1/approvals/missing/approve", {"approver": "Ada"}),
             ("post", "/v1/approvals/missing/reject", {"approver": "Ada"}),
             ("put", "/v1/policy", {"policy": policy, "updated_by": "Ada"}),
             ("post", "/v1/policy/reset", {"updated_by": "Ada"}),
             ("get", "/v1/approvals", None), ("get", "/v1/audit", None),
             ("get", "/v1/stats", None), ("get", "/v1/policy", None)]
    for method, path, body in calls:
        headers = agent_headers("agent-a")
        for payload in ({"json": body}, *({"content": content, "headers": {**headers, "Content-Type": "application/json"}} for content in ("{", b'\xff'))):
            response = client.request(method, path, **({"headers": headers} | payload))
            assert response.status_code == 403 and response.json()["code"] == "OPERATOR_REQUIRED"


@pytest.mark.parametrize("caller,status,code", [("missing", 401, "UNAUTHENTICATED"),
    ("unknown", 401, "UNAUTHENTICATED"), ("agent", 403, "OPERATOR_REQUIRED"), ("operator", 400, "HTTP_ERROR")])
def test_review_body_decoding_authentication(client, gateway, caller, status, code):
    headers = {"missing": {}, "unknown": {"Authorization": "Bearer unknown"},
               "agent": agent_headers("agent-a"), "operator": OPERATOR_HEADERS}[caller]
    response = client.put("/v1/policy", content=b'\xff', headers={**headers, "Content-Type": "application/json"})
    assert (response.status_code, response.json()["code"]) == (status, code)
    if status == 401:
        assert response.headers["www-authenticate"] == "Bearer"
    assert not gateway.audit()


def test_ac_4_object_ownership(client, gateway, need, upstream):
    lookup, quote, order = objects(gateway, need, "agent-b")
    calls = len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls)
    paths = [f"/v1/lookups/{lookup.lookup_id}", f"/v1/quotes/{quote.id}", f"/v1/orders/{order.id}",
             f"/v1/orders/{order.id}/timeline"]
    for path in paths:
        assert client.get(path, headers=agent_headers("agent-a")).status_code == 404
        assert client.get(path, headers=agent_headers("agent-b")).status_code == 200
        assert client.get(path, headers=OPERATOR_HEADERS).status_code == 200
    request = quote_request(need, qty=1, need_ref=lookup.lookup_id).model_dump(mode="json")
    plan_request = {"customer_id": "1", "deliver_by": str(need.deliver_by),
                    "plan_ref": {"lookup_id": lookup.lookup_id, "plan_index": 0}}
    for body in (request, plan_request):
        assert client.post("/v1/quotes", json=body, headers=agent_headers("agent-a")).status_code == 404
    for key in (order.idempotency_key, "stolen-quote"):
        assert client.post("/v1/orders", json={"quote_id": quote.id}, headers={
            **agent_headers("agent-a"), "Idempotency-Key": key}).status_code == 404
    denied = client.post(f"/v1/orders/{order.id}/cancel", json={"cancelled_by": "Fake"}, headers=agent_headers("agent-a"))
    assert denied.status_code == 404
    assert client.get("/v1/orders", headers=agent_headers("agent-a")).json() == []
    assert [o["id"] for o in client.get("/v1/orders", headers=agent_headers("agent-b")).json()] == [order.id]
    assert [o["id"] for o in client.get("/v1/orders", headers=OPERATOR_HEADERS).json()] == [order.id]
    assert calls == (len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls))
    # A customer's scope must not disclose another agent's lookup through a different error.
    policy = gateway.policy.get().model_dump(mode="json")
    policy["agents"]["agent-b"]["customer_ids"].append("2")
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Fixture"))
    other_need = need.model_copy(update={"customer_id": "2"})
    foreign = gateway.find_offers(other_need, "agent:agent-b")
    response = client.post("/v1/quotes", headers=agent_headers("agent-a"),
                           json=quote_request(other_need, qty=1, need_ref=foreign.lookup_id).model_dump(mode="json"))
    assert response.status_code == 404
    # Missing ownership on historical data is operator-only, never a wildcard.
    with gateway.db.connect(write=True) as conn:
        conn.execute("UPDATE lookups SET data=json_remove(data, '$.agent_id') WHERE id=?", (lookup.lookup_id,))
        conn.execute("UPDATE quotes SET data=json_remove(data, '$.agent_id') WHERE id=?", (quote.id,))
    for path in paths[:2]:
        assert client.get(path, headers=agent_headers("agent-b")).status_code == 404
        assert client.get(path, headers=OPERATOR_HEADERS).status_code == 200
    owner_cancel = client.post(f"/v1/orders/{order.id}/cancel", json={"cancelled_by": "Spoofed human"}, headers=agent_headers("agent-b"))
    assert owner_cancel.status_code == 200 and owner_cancel.json()["cancelled_by"] == "agent-b"


def test_ac_5_customer_scope_before_upstream(client, gateway, need, upstream):
    body = need.model_dump(mode="json")
    body["customer_id"] = "2"
    response = client.post("/v1/offers", json=body, headers=agent_headers("agent-a"))
    assert response.status_code == 403 and response.json()["code"] == "CUSTOMER_NOT_IN_MANDATE"
    assert not upstream.price_calls and not upstream.availability_calls and not upstream.decision_calls
    event = gateway.audit()[-1]
    assert event.action == "LOOKUP_FAILED" and event.actor == "agent:agent-a"
    assert event.data["error"] == "CUSTOMER_NOT_IN_MANDATE"
    assert event.data["live_calls"] == {"price": 0, "availability": 0}


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_review_denied_quote_replay_matches_fresh_failure_audit(client, gateway, need, upstream, transport):
    """Spec 02 R-10 / spec 19 R-4: scope refusals have identical audit data."""
    lookup = gateway.find_offers(need, "agent:agent-a")
    body = quote_request(need, qty=1, need_ref=lookup.lookup_id).model_dump(mode="json")

    def call(key):
        if transport == "rest":
            result = client.post("/v1/quotes", json=body,
                headers={**agent_headers("agent-a"), "Idempotency-Key": key})
            return result.status_code, result.json()
        result = asyncio.run(create_server(gateway, "agent-a").call_tool("create_quote", {**body, "idempotency_key": key}))
        return (403 if result.is_error else 200), result.structured_content

    assert call("quote-audit")[0] == 200
    policy = gateway.policy._editable(gateway.policy.get())
    policy["agents"]["agent-a"]["customer_ids"] = ["2"]
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Scope removed"))
    calls = len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls)
    audits = []
    for key in ("fresh-refusal", "quote-audit"):
        before = len(gateway.audit())
        status, result = call(key)
        assert (status, result["code"]) == (403, "CUSTOMER_NOT_IN_MANDATE")
        events = gateway.audit()[before:]
        assert [event.action for event in events] == ["CREATE_QUOTE_FAILED"]
        assert events[0].actor == "agent:agent-a"
        # Each request retains its own trace/correlation; compare domain evidence.
        assert events[0].data["trace_id"] and events[0].data["correlation_id"]
        audits.append({key: value for key, value in events[0].data.items()
                       if key not in {"trace_id", "correlation_id"}})
    assert audits[0] == audits[1]
    assert calls == (len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls))


def test_ac_6_mandate_required(client, gateway, need, upstream):
    lookup, quote, _ = objects(gateway, need, "agent-a")
    # Revoke after a quote exists to exercise placement without an ownership mismatch.
    policy = gateway.policy.get().model_dump(mode="json")
    del policy["agents"]["agent-a"]
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Fixture"))
    calls = len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls)
    for method, path, body, headers in [
        ("post", "/v1/offers", need.model_dump(mode="json"), agent_headers("unregistered")),
        ("post", "/v1/quotes", quote_request(need, need_ref=lookup.lookup_id).model_dump(mode="json"), agent_headers("unregistered")),
        ("post", "/v1/orders", {"quote_id": quote.id}, {**agent_headers("agent-a"), "Idempotency-Key": "new-unregistered"}),
        ("post", "/v1/orders", {"quote_id": quote.id}, {**agent_headers("unregistered"), "Idempotency-Key": "unregistered"}),
    ]:
        response = client.request(method, path, json=body, headers=headers)
        assert response.status_code == 403 and response.json()["code"] == "AGENT_NOT_REGISTERED"
    assert calls == (len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls))


def test_ac_7_honest_operator_actor(client, gateway, need, upstream):
    _, _, order = objects(gateway, need, "agent-a", qty=200)
    approved = client.post(f"/v1/approvals/{order.id}/approve", headers=OPERATOR_HEADERS,
                           json={"approver": "Ada", "evidence_revision": order.evidence_revision})
    assert approved.status_code == 200 and approved.json()["approver"] == "Ada"
    event = gateway.audit(order_id=order.id)[-1]
    assert event.action == "APPROVE" and event.actor == "human:operator" and event.data["stated_name"] == "Ada"
    invalid = client.post(f"/v1/orders/{order.id}/cancel", headers=agent_headers("agent-a"), json={"cancelled_by": " "})
    assert invalid.status_code == 422 and gateway.audit()[-1].actor == "agent:agent-a"
    failed = client.post(f"/v1/approvals/{order.id}/reject", headers=OPERATOR_HEADERS, json={"approver": "Lin"})
    assert failed.status_code == 409
    assert gateway.audit()[-1].actor == "human:operator" and gateway.audit()[-1].data["stated_name"] == "Lin"
    cancelled = client.post(f"/v1/orders/{order.id}/cancel", headers=OPERATOR_HEADERS, json={"cancelled_by": "Grace"})
    assert cancelled.status_code == 200 and cancelled.json()["cancelled_by"] == "Grace"
    assert gateway.audit()[-1].actor == "human:operator" and gateway.audit()[-1].data["stated_name"] == "Grace"
    _, _, pending = objects(gateway, need, "agent-a", qty=200)
    assert client.post(f"/v1/approvals/{pending.id}/reject", headers=OPERATOR_HEADERS, json={"approver": "Lin"}).status_code == 200
    assert gateway.audit()[-1].actor == "human:operator" and gateway.audit()[-1].data["stated_name"] == "Lin"
    policy = client.get("/v1/policy", headers=OPERATOR_HEADERS).json()
    for path, method, body in [("/v1/policy", "put", {"policy": policy, "updated_by": "Ada"}),
                               ("/v1/policy/reset", "post", {"updated_by": "Ada"})]:
        result = client.request(method, path, json=body, headers=OPERATOR_HEADERS)
        assert result.status_code == 200 and result.json()["updated_by"] == "Ada"
        assert gateway.audit()[-1].actor == "human:operator" and gateway.audit()[-1].data["stated_name"] == "Ada"


def test_ac_8_operator_buyer_identity(client, gateway, need, upstream):
    headers = {**OPERATOR_HEADERS, "X-Agent-Id": "agent-a"}
    lookup = client.post("/v1/offers", json=need.model_dump(mode="json"), headers=headers).json()
    quote = client.post("/v1/quotes", json=quote_request(need, qty=1, need_ref=lookup["lookup_id"]).model_dump(mode="json"), headers=headers).json()
    response = client.post("/v1/orders", json={"quote_id": quote["id"]}, headers={**headers, "Idempotency-Key": "console"})
    assert response.status_code == 200 and response.json()["agent_id"] == "web-console"
    assert lookup["agent_id"] == quote["agent_id"] == "web-console"
    assert all(event.actor == "agent:web-console" for event in gateway.audit())
    body = need.model_dump(mode="json")
    body["customer_id"] = "2"
    assert client.post("/v1/offers", json=body, headers=headers).json()["code"] == "CUSTOMER_NOT_IN_MANDATE"
    custom = Credentials(OPERATOR_TOKEN, operator_agent_id="agent-a")
    with TestClient(create_app(gateway, keys=custom)) as other:
        assert other.post("/v1/offers", json=need.model_dump(mode="json"), headers=OPERATOR_HEADERS).json()["agent_id"] == "agent-a"


def test_ac_9_no_secret_leaks(client, gateway, need, upstream, caplog):
    objects(gateway, need, "agent-a")
    outputs = [client.get(path, headers=OPERATOR_HEADERS).text for path in ("/v1/me", "/v1/policy", "/v1/audit", "/v1/orders", "/openapi.json")]
    outputs.extend([caplog.text, repr(gateway.settings), repr(gateway.settings.keys)])
    with gateway.db.connect() as conn:
        for table in ("lookups", "quotes", "orders", "audit"):
            outputs.extend(str(tuple(row)) for row in conn.execute(f"SELECT * FROM {table}"))
    text = "\n".join(outputs)
    for secret in (OPERATOR_TOKEN, *AGENT_TOKENS.values()):
        assert secret not in text
        assert sha256(secret.encode()).hexdigest() not in text
    assert "AGENT_KEYS" not in client.get("/v1/policy", headers=OPERATOR_HEADERS).text


@pytest.mark.parametrize("identity", ["agent-a", "agent-b", "operator"])
def test_review_principal_reference_contains_no_digest(client, gateway, identity):
    secret = OPERATOR_TOKEN if identity == "operator" else AGENT_TOKENS[identity]
    response = client.get("/v1/me", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 200
    principal = gateway.settings.keys.resolve(secret, gateway.policy.get())
    public = json.dumps(principal.public())
    digest = sha256(secret.encode()).hexdigest()
    assert digest not in response.text and digest not in public
    assert secret not in response.text and secret not in public
    assert principal.credential_ref and not principal.credential_ref.startswith("sha256:")
    assert principal.credential_ref == gateway.settings.keys.resolve(secret).credential_ref


def test_ac_10_idempotent_key_generation(tmp_path):
    env_file = tmp_path / "keys.env"
    env_file.write_text("KEEP_ME=untouched\nAGENT_KEYS=hotel-chain-agent:existing-agent-secret\nOPERATOR_TOKEN=\n")
    command = [sys.executable, "scripts/gen_keys.py", "--write", "--env-file", str(env_file)]
    first = subprocess.run(command, cwd=BACKEND, capture_output=True, text=True, check=True)
    content, modified = env_file.read_bytes(), env_file.stat().st_mtime_ns
    second = subprocess.run(command, cwd=BACKEND, capture_output=True, text=True, check=True)
    assert env_file.read_bytes() == content and env_file.stat().st_mtime_ns == modified
    assert "OPERATOR_TOKEN" in first.stdout and "web-console" in first.stdout
    assert second.stdout.strip() == "No names added."
    values = dict(line.split("=", 1) for line in content.decode().splitlines())
    assert values["KEEP_ME"] == "untouched" and "hotel-chain-agent:existing-agent-secret" in values["AGENT_KEYS"]
    keys = dict(pair.split(":", 1) for pair in values["AGENT_KEYS"].split(","))
    assert len(values["OPERATOR_TOKEN"]) == len(keys["web-console"]) == 43
    for secret in (*keys.values(), values["OPERATOR_TOKEN"]):
        assert secret not in first.stdout + first.stderr + second.stdout + second.stderr
    assert env_file.stat().st_mode & 0o077 == 0


def test_ac_11_mcp_transports(gateway, need, upstream):
    server = create_server(gateway, "agent-a")
    app = server.streamable_http_app(stateless_http=True, json_response=True)
    message = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}}}
    with TestClient(app, base_url="http://localhost:8001") as remote:
        for headers in ({}, OPERATOR_HEADERS, agent_headers("agent-b"), {"Authorization": "Bearer wrong"}):
            for method in ("POST", "GET", "DELETE"):
                assert remote.request(method, "/mcp", headers=headers, json=message).status_code == 401
        response = remote.post("/mcp", json=message, headers={**agent_headers("agent-a"), "Accept": "application/json, text/event-stream"})
        assert response.status_code == 200 and response.json()["result"]["serverInfo"]["name"] == server.name
    with pytest.raises(RuntimeError, match="AGENT_KEYS"):
        create_server(gateway, "no-key").streamable_http_app()
    # Stdio has no HTTP credential requirement. Its provisioned identity still owns the operations.
    async def tools():
        local = create_server(gateway, "agent-b")
        result = await local.call_tool("find_offers", {"need": need.model_dump(mode="json")})
        assert result.structured_content["agent_id"] == "agent-b"
        lookup, quote, order = objects(gateway, need, "agent-a")
        from mcp.server.mcpserver.exceptions import ToolError
        tool_failure = await local.call_tool("get_order_status", {"order_id": order.id})
        assert tool_failure.is_error
        assert tool_failure.structured_content["code"] == "ORDER_NOT_FOUND"
    asyncio.run(tools())
    _, _, owned = objects(gateway, need, "agent-b")
    _, _, other = objects(gateway, need, "agent-a")
    # Exercise the actual --agent-id CLI path over JSON-RPC pipes with an isolated fixture DB.
    runner = """
import sys
from pathlib import Path
from app.config import Settings
from app.service import Gateway
import app.mcp_server as module
path = Path(sys.argv[1])
module.Gateway = lambda: Gateway(Settings(db_path=path))
sys.argv = ['mcp_server', '--agent-id', 'agent-b']
module.main()
"""
    process = subprocess.Popen([sys.executable, "-c", runner, str(gateway.settings.db_path)], cwd=BACKEND,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
        assert select.select([process.stdout], [], [], 5)[0], "stdio did not initialize"
        assert json.loads(process.stdout.readline())["result"]["serverInfo"]["name"] == server.name
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        for request_id, order in enumerate((owned, other), start=2):
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                "params": {"name": "get_order_status", "arguments": {"order_id": order.id}}}) + "\n")
            process.stdin.flush()
            assert select.select([process.stdout], [], [], 5)[0], "stdio tool did not respond"
            reply = json.loads(process.stdout.readline())
            assert reply["id"] == request_id
            result = reply["result"]
            if order == owned:
                assert not result.get("isError"), result
                assert result["structuredContent"]["agent_id"] == "agent-b"
                assert result["structuredContent"]["id"] == owned.id
            else:
                assert result["isError"] and "ORDER_NOT_FOUND" in result["content"][0]["text"]
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdout.close()
    assert process.returncode == 0
