"""Spec 43 part 1: real REST sessions, tenancy and connection lifecycle."""
import hashlib
import json
import shlex
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import audit
from app.accounts import DEMO_USERS, hash_password
from app.api import create_app
from app.auth import Credentials
from app.db import Database
from app.foundation import PRINCIPAL
from app.models import PolicyUpdateRequest
from app.tenancy import HOTEL, HOSPITAL
from conftest import OPERATOR_HEADERS, TEST_KEYS, agent_headers, demo_catalog
from test_verifiable_mandate import buyer as vc_issuer, ISSUER

PASSWORD = "test-account-password-43"


@pytest.fixture
def http(gateway):
    encoded = hash_password(PASSWORD)
    with gateway.db.connect(write=True) as conn:
        conn.execute("UPDATE users SET password_hash=?", (encoded,))
    with TestClient(create_app(gateway), base_url="http://localhost") as client:
        yield client


def sign(http, email="director@aurora.example", password=PASSWORD):
    response = http.post("/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    headers = {"Cookie": "vtg_session=" + response.cookies["vtg_session"], "X-CSRF-Token": response.json()["csrf_token"]}
    http.cookies.clear()
    return headers


def connect(http, headers, *, agent="hotel-chain-agent", audience="buyer", **extra):
    response = http.post("/v1/tenant/connections", headers=headers, json={"agent_id": agent, "type": "rest", "client": "python",
        "audience": audience, "customer_ids": ["1"], **extra})
    assert response.status_code == 201, response.text
    return response.json()


def bearer(connection):
    return {"Authorization": "Bearer " + connection["key"]}


def pending(http, need, headers=None):
    headers = headers or agent_headers("external")
    lookup = http.post("/v1/offers", headers=headers, json=need.model_dump(mode="json"))
    assert lookup.status_code == 200, lookup.text
    quote = http.post("/v1/quotes", headers=headers, json={"customer_id": need.customer_id, "deliver_by": str(need.deliver_by),
        "need_ref": lookup.json()["lookup_id"], "lines": [{"product_id": "1", "quantity_sales_units": 200}]})
    assert quote.status_code == 200, quote.text
    order = http.post("/v1/orders", headers={**headers, "Idempotency-Key": "pending-" + need.customer_id}, json={"quote_id": quote.json()["id"]})
    assert order.status_code == 200, order.text
    assert order.json()["status"] == "PENDING_APPROVAL"
    return lookup.json(), quote.json(), order.json()


def test_AC_1_tenant_isolation_shared_storage_and_customer_gate(http, gateway, need, upstream):
    lookup, quote, order = pending(http, need)
    b = sign(http, "manager@kaari.example")
    key = connect(http, b, agent="hospital-kitchen-agent", customer_ids=["2"])
    b_agent = bearer(key)
    issued_by_platform = http.post("/v1/connections", headers={**OPERATOR_HEADERS, "Idempotency-Key": "operator-b-key"},
        json={"agent_id": "hospital-kitchen-agent", "client": "rest", "customer_ids": ["2"], "tenant_id": HOSPITAL})
    assert issued_by_platform.status_code == 201, issued_by_platform.text
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT tenant_id FROM commands WHERE key='operator-b-key'").fetchone()[0] == HOSPITAL
    for route in [f'/v1/lookups/{lookup["lookup_id"]}', f'/v1/quotes/{quote["id"]}', f'/v1/orders/{order["id"]}']:
        assert http.get(route, headers=b_agent).status_code == 404
        assert http.get(route, headers=OPERATOR_HEADERS).status_code == 200
    assert http.post("/v1/orders", headers={**b_agent, "Idempotency-Key": "foreign"}, json={"quote_id": quote["id"]}).status_code == 404
    calls = len(upstream.price_calls) + len(upstream.availability_calls)
    response = http.post("/v1/offers", headers=b_agent, json=need.model_dump(mode="json"))
    assert response.status_code == 403 and response.json()["code"] == "CUSTOMER_NOT_IN_MANDATE"
    assert len(upstream.price_calls) + len(upstream.availability_calls) == calls
    # The shared helper scopes joins, lists, updates and UPSERTs, rather than relying on endpoint checks.
    p = gateway.accounts.session(b["Cookie"].partition("=")[2])
    token = PRINCIPAL.set(p)
    try:
        with gateway.db.connect(write=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
            assert conn.execute("SELECT o.id FROM orders o JOIN quotes q ON q.id=o.quote_id").fetchall() == []
            assert conn.execute("UPDATE orders SET status='REJECTED' WHERE id=?", (order["id"],)).rowcount == 0
            conn.execute("SAVEPOINT tenant_scope")
            assert conn.execute("SELECT COUNT(*) FROM lookups").fetchone()[0] == 0
            conn.execute("ROLLBACK TO tenant_scope")
            assert conn.execute("SELECT COUNT(*) FROM lookups").fetchone()[0] == 0
            conn.execute("RELEASE tenant_scope")
    finally:
        PRINCIPAL.reset(token)
    assert http.get(f'/v1/orders/{order["id"]}', headers=OPERATOR_HEADERS).json()["status"] == "PENDING_APPROVAL"
    Database(gateway.db.path)  # repeatable migration preserves live rows (fixture database only)


def test_AC_1_signed_revocation_cannot_cross_tenants(http, gateway, vc_issuer):
    from app.mandates import sign_jwt
    token = sign_jwt(vc_issuer, {"iss": ISSUER, "jti_revoked": ["future-mandate"], "iat": audit.utcnow().timestamp()}, kid="buyer-1")
    hospital = sign(http, "manager@kaari.example")
    key = connect(http, hospital, agent="hospital-kitchen-agent", customer_ids=["2"])
    refused = http.post("/v1/mandates/revocations", headers=bearer(key), json={"jwt": token})
    assert refused.status_code == 403 and refused.json()["code"] == "CUSTOMER_NOT_IN_MANDATE"
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mandate_revocations").fetchone()[0] == 0
    assert http.post("/v1/mandates/revocations", headers=sign(http), json={"jwt": token}).status_code == 200


def test_AC_2_login_cookie_csrf_logout_and_throttling(http):
    response = http.post("/v1/auth/login", json={"email": "director@aurora.example", "password": PASSWORD})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert "secure" not in cookie
    assert http.get("/v1/auth/session").json()["role"] == "tenant_admin"
    assert http.post("/v1/tenant/agents", json={"agent_id": "csrf-agent", "name": "CSRF"}).status_code == 403
    assert http.post("/v1/auth/logout", headers={"X-CSRF-Token": response.json()["csrf_token"]}).status_code == 200
    assert http.get("/v1/auth/session").status_code == 401
    for _ in range(5):
        assert http.post("/v1/auth/login", json={"email": "missing@example.test", "password": "wrong"}).status_code == 401
    assert http.post("/v1/auth/login", json={"email": "missing@example.test", "password": "wrong"}).status_code == 429


def test_AC_3_rest_connection_create_prefix_read_revoke_and_catalog(http):
    admin = sign(http)
    key = connect(http, admin)
    assert key["key"].startswith("vtg_live_" + key["connection_id"] + "_")
    assert key["tenant_id"] == HOTEL
    assert http.get("/v1/me", headers=bearer(key)).json()["principal"]["connection_id"] == key["connection_id"]
    listed = http.get("/v1/tenant/connections", headers=admin).json()
    assert key["key"] not in json.dumps(listed) and "key_digest" not in json.dumps(listed)
    for audience in ["buyer", "tenant", "operator"]:
        catalog = http.get("/v1/connect/catalog", params={"audience": audience}).json()
        assert {x["type"] for x in catalog["types"]} == {"mcp", "rest", "a2a", "webhook", "tool_pack"}
        for entry in catalog["types"]:
            for client in entry["clients"]:
                result = http.get(f'/v1/tenant/connections/{key["connection_id"]}/instructions', params={"client": client}, headers=admin)
                assert result.status_code == 200, result.text
                assert result.json()["snippet"] and "VTG_CONNECTION_KEY" in result.json()["snippet"]
                assert key["key"] not in result.text
    assert http.delete(f'/v1/tenant/connections/{key["connection_id"]}', headers=admin).status_code == 200
    assert http.get("/v1/me", headers=bearer(key)).status_code == 401


def test_AC_4_rotation_overlap_and_expiry(http, gateway, monkeypatch):
    admin = sign(http)
    key = connect(http, admin)
    now = audit.utcnow()
    response = http.post(f'/v1/tenant/connections/{key["connection_id"]}/rotate', headers=admin, json={"overlap_hours": 1})
    assert response.status_code == 200, response.text
    new = response.json()
    assert new["key"] != key["key"]
    assert http.get("/v1/me", headers=bearer(key)).status_code == 200
    assert http.get("/v1/me", headers=bearer(new)).status_code == 200
    monkeypatch.setattr(audit, "utcnow", lambda: now + timedelta(hours=2))
    assert http.get("/v1/me", headers=bearer(key)).status_code == 401
    assert http.get("/v1/me", headers=bearer(new)).status_code == 200


def test_AC_5_same_tenant_customer_intersection(http, gateway, need, upstream):
    with gateway.db.connect(write=True) as conn:
        conn.execute("INSERT INTO tenant_customers VALUES('3',?)", (HOTEL,))
    policy = gateway.policy.get().model_dump(mode="json")
    policy["agents"]["hotel-chain-agent"]["customer_ids"] = ["1", "3"]
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Fixture"))
    key = connect(http, sign(http))
    request = need.model_copy(update={"customer_id": "3"})
    assert http.post("/v1/offers", headers=bearer(key), json=request.model_dump(mode="json")).status_code == 403
    assert not upstream.price_calls and not upstream.availability_calls
    assert http.get("/v1/me", headers=bearer(key)).json()["customer_ids"] == ["1"]


@pytest.mark.parametrize("reviewer", ["tenant", "operator"])
def test_AC_11_human_approval_scope_two_steps_and_identity(http, need, upstream, reviewer):
    _, _, order = pending(http, need)
    foreign = sign(http, "dietitian@kaari.example")
    body = {"approver": "Chef", "evidence_revision": order["evidence_revision"]}
    url = f'/v1/{"tenant/" if reviewer == "tenant" else ""}approvals/{order["id"]}/approve'
    assert http.post(url, headers=foreign, json=body).status_code == 404
    viewer = sign(http, "finance@aurora.example")
    assert http.post(url, headers=viewer, json=body).status_code == 403
    chef = sign(http, "chef@aurora.example") if reviewer == "tenant" else OPERATOR_HEADERS
    preview = http.post(url, headers=chef, json=body)
    assert preview.status_code == 200, preview.text
    assert preview.json()["confirmation_required"]
    assert http.get(f'/v1/tenant/orders/{order["id"]}', headers=chef).json()["status"] == "PENDING_APPROVAL"
    confirmation = preview.json()["confirmation_token"]
    result = http.post(url, headers=chef, json={**body, "confirmation_token": confirmation})
    assert result.status_code == 200, result.text
    assert result.json()["approver_user_id"] == ("aurora-chef" if reviewer == "tenant" else "legacy-operator")
    assert result.json()["approver_role"] == ("approver" if reviewer == "tenant" else "platform_admin")
    assert result.json()["status"] == "CONFIRMED"
    assert http.post(url, headers=chef, json={**body, "confirmation_token": confirmation}).status_code == 409


def test_AC_12_no_secret_digest_session_or_password_in_public_outputs(http, gateway, caplog):
    admin = sign(http)
    key = connect(http, admin)
    responses = [http.get("/v1/auth/session", headers=admin), http.get("/v1/me", headers=bearer(key)),
                 http.get("/v1/tenant/connections", headers=admin), http.get("/v1/tenant/users", headers=admin),
                 http.get("/v1/tenant/audit", headers=admin)]
    with gateway.db.connect() as conn:
        secrets = [r[0] for r in conn.execute("SELECT key_digest FROM connections")] + [r[0] for r in conn.execute("SELECT digest FROM sessions")] + [r[0] for r in conn.execute("SELECT password_hash FROM users WHERE password_hash IS NOT NULL")]
    public = " ".join(r.text for r in responses) + json.dumps([a.model_dump(mode="json") for a in gateway.audit()]) + caplog.text
    for secret in [PASSWORD, key["key"], admin["Cookie"].partition("=")[2], *secrets]:
        assert secret not in public
    assert all(r.status_code == 200 for r in responses)


def test_AC_13_legacy_keys_operator_and_unchanged_demo(http, gateway, need, upstream, tmp_path):
    assert http.get("/v1/me", headers=OPERATOR_HEADERS).json()["principal"]["role"] == "platform_admin"
    me = http.get("/v1/me", headers=agent_headers("hotel-chain-agent")).json()
    assert me["principal"]["tenant_id"] == HOTEL and me["customer_ids"] == ["1"]
    demo_catalog(gateway, tmp_path, upstream)
    headers = agent_headers("hotel-chain-agent")
    lookup = http.post("/v1/offers", headers=headers, json=need.model_dump(mode="json")).json()
    assert (lookup["offers"][0]["product_id"], lookup["offers"][0]["purchase_total"]) == ("464306", 1010)
    quote = http.post("/v1/quotes", headers=headers, json={"customer_id": "1", "deliver_by": str(need.deliver_by), "need_ref": lookup["lookup_id"],
        "lines": [{"product_id": "464306", "quantity_sales_units": 1000}]}).json()
    order = http.post("/v1/orders", headers={**headers, "Idempotency-Key": "demo43"}, json={"quote_id": quote["id"]}).json()
    assert (order["status"], order["total"]) == ("CONFIRMED", 1010)


@pytest.mark.parametrize("audience,email,path,status", [
    ("tenant", "director@aurora.example", "/v1/tenant/orders", 200),
    ("tenant", "director@aurora.example", "/v1/operator/orders", 401),
    ("operator", "staff@valio.example", "/v1/operator/orders", 200),
    ("operator", "staff@valio.example", "/v1/tenant/orders", 401),
    ("buyer", "director@aurora.example", "/v1/tenant/orders", 403),
    ("buyer", "director@aurora.example", "/v1/operator/orders", 403),
])
def test_AC_16_REST_audiences(http, need, upstream, audience, email, path, status):
    _, _, order = pending(http, need)
    headers = sign(http, email)
    key = connect(http, headers, audience=audience)
    response = http.get(path, headers=bearer(key))
    assert response.status_code == status, response.text
    if status == 200:
        assert order["id"] in {o["id"] for o in response.json()}
    if audience == "tenant":
        b = sign(http, "manager@kaari.example")
        b_key = connect(http, b, agent="hospital-kitchen-agent", audience="tenant", customer_ids=["2"])
        assert http.get("/v1/tenant/orders", headers=bearer(b_key)).json() == []


def test_AC_18_invite_accept_once_and_sign_in(http, caplog):
    admin = sign(http)
    invite = http.post("/v1/tenant/users", headers=admin, json={"email": "new-chef@example.test", "role": "approver"})
    assert invite.status_code == 201, invite.text
    token = invite.json()["token"]
    endpoint = f"/v1/auth/invites/{token}/accept"
    assert http.post(endpoint, json={"password": PASSWORD}).json()["role"] == "approver"
    reused = http.post(endpoint, json={"password": PASSWORD})
    assert reused.status_code == 410
    assert token not in reused.text and token not in caplog.text
    new = sign(http, "new-chef@example.test")
    assert http.get("/v1/auth/session", headers=new).json()["role"] == "approver"


def test_AC_19_live_role_changes_and_cross_tenant_user_404(http):
    admin = sign(http)
    chef = sign(http, "chef@aurora.example")
    assert http.patch("/v1/tenant/users/aurora-chef", headers=admin, json={"role": "viewer"}).status_code == 200
    assert http.get("/v1/auth/session", headers=chef).json()["role"] == "viewer"
    assert http.get("/v1/tenant/users", headers=chef).status_code == 403
    for method in ("get", "patch"):
        kwargs = {"json": {"role": "viewer"}} if method == "patch" else {}
        assert getattr(http, method)("/v1/tenant/users/kaari-manager", headers=admin, **kwargs).status_code == 404


def test_AC_20_last_admin_and_disable_revokes_sessions_and_keys(http):
    admin = sign(http)
    assert http.post("/v1/tenant/users", headers=admin, json={"email": "pending-admin@example.test", "role": "tenant_admin"}).status_code == 201
    for changes in ({"role": "viewer"}, {"disabled": True}):
        response = http.patch("/v1/tenant/users/aurora-director", headers=admin, json=changes)
        assert response.status_code == 409 and response.json()["code"] == "LAST_TENANT_ADMIN"
    assert http.delete("/v1/tenant/users/aurora-director", headers=admin).status_code == 409
    chef = sign(http, "chef@aurora.example")
    assert http.patch("/v1/tenant/users/aurora-chef", headers=admin, json={"role": "tenant_admin"}).status_code == 200
    key = connect(http, chef, audience="tenant")
    buyer_key = connect(http, chef, audience="buyer")
    assert http.patch("/v1/tenant/users/aurora-chef", headers=admin, json={"disabled": True}).status_code == 200
    assert http.get("/v1/auth/session", headers=chef).status_code == 401
    assert http.get("/v1/tenant/orders", headers=bearer(key)).status_code == 401
    assert http.get("/v1/me", headers=bearer(buyer_key)).status_code == 401


def test_AC_21_platform_creates_tenant_assigns_customer_and_first_admin(http):
    staff = sign(http, "staff@valio.example")
    result = http.post("/v1/operator/tenants", headers=staff, json={"name": "Bistro", "customer_ids": ["3"]})
    assert result.status_code == 201, result.text
    tid = result.json()["id"]
    invited = http.post(f"/v1/operator/tenants/{tid}/users", headers=staff, json={"email": "owner@bistro.example", "role": "tenant_admin"})
    assert invited.status_code == 201, invited.text
    assert http.post(f'/v1/auth/invites/{invited.json()["token"]}/accept', json={"password": PASSWORD}).status_code == 200
    owner = sign(http, "owner@bistro.example")
    session = http.get("/v1/auth/session", headers=owner).json()
    assert session["tenant_id"] == tid and session["customer_ids"] == ["3"]


@pytest.mark.parametrize("method", ["get", "post"])
def test_AC_1_OpenAPI_GET_POST_route_isolation(http, gateway, need, upstream, method):
    """Walk the live schema: foreign targets fail, shared/caller collections stay safe.

    Public discovery, catalog facts and caller session/context have no foreign
    business target. Authentication links have their own single-use tests.
    """
    lookup, quote, order = pending(http, need)
    a = connect(http, sign(http))
    b = connect(http, sign(http, "manager@kaari.example"), agent="hospital-kitchen-agent", customer_ids=["2"])
    headers = {**bearer(b), "Idempotency-Key": "foreign-openapi-target"}
    schema = http.get("/openapi.json").json()
    foreign = {"order_id": order["id"], "quote_id": quote["id"], "need_ref": lookup["lookup_id"],
               "lookup_id": lookup["lookup_id"], "agent_id": "hotel-chain-agent", "connection_id": a["connection_id"],
               "user_id": "aurora-director", "tenant_id": HOTEL, "customer_id": "1"}

    def example(definition, name="", depth=0):
        if name in foreign:
            return foreign[name]
        if depth > 12:
            return None
        if "$ref" in definition:
            return example(schema["components"]["schemas"][definition["$ref"].split("/")[-1]], name, depth+1)
        if "anyOf" in definition:
            return example(next(x for x in definition["anyOf"] if x.get("type") != "null"), name, depth+1)
        if "enum" in definition:
            return definition["enum"][0]
        if "default" in definition and definition["default"] is not None:
            return definition["default"]
        if "const" in definition:
            return definition["const"]
        kind = definition.get("type")
        if kind == "object" or "properties" in definition:
            properties = definition.get("properties", {})
            return {k: example(v, k, depth+1) for k, v in properties.items()
                    if k in definition.get("required", []) or k in foreign}
        if kind == "array":
            return [example(definition.get("items", {}), name, depth+1) for _ in range(max(1, definition.get("minItems", 0)))]
        if kind in {"integer", "number"}:
            return max(1, definition.get("minimum", 1))
        if kind == "boolean":
            return True
        if definition.get("format") == "date":
            return str(need.deliver_by)
        if definition.get("format") == "date-time":
            return (audit.utcnow()+timedelta(days=1)).isoformat()
        if name == "product_id":
            return "1"
        return "foreign-target"

    walked, targeted = [], []
    for template, methods in schema["paths"].items():
        if method not in methods:
            continue
        # Login and bearer/CSRF logout act on the caller rather than another tenant.
        if template.startswith("/v1/auth/"):
            walked.append(template)
            continue
        operation = methods[method]
        url = template
        params = operation.get("parameters", [])
        has_target = False
        for parameter in params:
            if parameter["in"] == "path":
                name = parameter["name"]
                value = foreign.get(name, "foreign-target")
                if name == "id":
                    value = quote["id"] if "/quotes/" in template else order["id"] if "/orders/" in template else "1" if "/products/" in template else "foreign-target"
                url = url.replace("{"+name+"}", value)
                has_target |= "/products/" not in template
        kwargs = {}
        if method == "post":
            body_schema = operation.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
            body = example(body_schema)
            if template == "/v1/offers":
                body = need.model_dump(mode="json")
            elif template == "/v1/quotes":
                body = {"customer_id": "1", "deliver_by": str(need.deliver_by), "need_ref": lookup["lookup_id"],
                        "lines": [{"product_id": "1", "quantity_sales_units": 200}]}
            kwargs["json"] = body
            has_target |= template in {"/v1/offers", "/v1/quotes", "/v1/orders"}
        result = getattr(http, method)(url, headers=headers, **kwargs)
        walked.append(template)
        if has_target:
            assert result.status_code in {403, 404}, (method, template, result.status_code, result.text)
            targeted.append(template)
        else:
            assert result.status_code < 500, (method, template, result.text)
            # Globally public schemas/docs include example strings, not stored objects.
            assert all(value not in result.text for value in [order["id"], quote["id"], lookup["lookup_id"]]), (method, template)
    assert walked and targeted
    assert len(walked) == sum(method in methods for methods in schema["paths"].values())


def test_instructions_execute_REST_Python_and_MCP_against_gateway(http, upstream, monkeypatch):
    admin = sign(http)
    key = connect(http, admin)
    endpoint = f'/v1/tenant/connections/{key["connection_id"]}/instructions'
    curl = http.get(endpoint, params={"client": "curl"}, headers=admin).json()
    parts = shlex.split(curl["snippet"])
    url = next(x for x in parts if x.startswith("http"))
    request_headers = {}
    for i, value in enumerate(parts):
        if value == "-H":
            k, v = parts[i+1].split(":", 1)
            request_headers[k] = v.strip().replace("$VTG_CONNECTION_KEY", key["key"])
    result = http.post(url, headers=request_headers, json=json.loads(parts[parts.index("--data")+1]))
    assert result.status_code == 200, result.text
    assert result.json()["offers"]
    python = http.get(endpoint, params={"client": "python"}, headers=admin).json()
    import httpx
    calls = []
    class InProcessClient:
        def __init__(self, *, base_url, headers):
            self.base_url, self.headers = base_url, headers
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, url, **kwargs):
            result = http.post(self.base_url+url, headers=self.headers, **kwargs)
            calls.append(result)
            return result
    monkeypatch.setattr(httpx, "Client", InProcessClient)
    monkeypatch.setenv("VTG_CONNECTION_KEY", key["key"])
    exec(compile(python["snippet"], "<generated-connection-python>", "exec"), {})
    assert calls and calls[0].status_code == 200
    mcp = http.get(endpoint, params={"client": "cursor"}, headers=admin).json()
    config = next(iter(json.loads(mcp["snippet"])["mcpServers"].values()))
    headers = {k: v.replace("${VTG_CONNECTION_KEY}", key["key"]) for k, v in config["headers"].items()}
    result = http.post(config["url"], headers={**headers, "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "find_offers", "arguments": {"need": mcp["lookup_example"]}}})
    assert result.status_code == 200, result.text
    assert not result.json()["result"].get("isError", False)
    assert result.json()["result"]["structuredContent"]["offers"]


def test_confirmation_is_user_payload_expiry_and_evidence_bound(http, gateway, need, upstream, monkeypatch):
    _, _, order = pending(http, need)
    chef, admin = sign(http, "chef@aurora.example"), sign(http)
    url = f'/v1/tenant/approvals/{order["id"]}/approve'
    body = {"approver": "Chef", "evidence_revision": order["evidence_revision"]}
    first = http.post(url, headers=chef, json=body).json()
    token = first["confirmation_token"]
    assert http.post(url, headers=admin, json={**body, "confirmation_token": token}).status_code == 409
    assert http.post(url, headers=chef, json={**body, "comment": "changed", "confirmation_token": token}).status_code == 409
    with gateway.db.connect(write=True) as conn:
        raw = json.loads(conn.execute("SELECT data FROM orders WHERE id=?", (order["id"],)).fetchone()[0])
        raw["evidence_revision"] += 1
        conn.execute("UPDATE orders SET data=? WHERE id=?", (json.dumps(raw), order["id"]))
    changed = http.post(url, headers=chef, json={**body, "confirmation_token": token})
    assert changed.status_code == 409 and changed.json()["code"] == "EVIDENCE_CHANGED"
    body["evidence_revision"] += 1
    fresh = http.post(url, headers=chef, json=body).json()["confirmation_token"]
    now = audit.utcnow()
    monkeypatch.setattr(audit, "utcnow", lambda: now+timedelta(minutes=6))
    assert http.post(url, headers=chef, json={**body, "confirmation_token": fresh}).status_code == 409


def test_agent_mandate_policy_and_team_lifecycles(http, gateway):
    admin = sign(http)
    assert http.post("/v1/tenant/agents", headers=admin, json={"agent_id": "new-tenant-agent", "name": "Kitchen"}).status_code == 201
    assert http.patch("/v1/tenant/agents/new-tenant-agent", headers=admin, json={"name": "Renamed"}).json()["name"] == "Renamed"
    mandate = http.post("/v1/tenant/agents/new-tenant-agent/mandates", headers=admin, json={"customer_id": "1"})
    assert mandate.status_code == 201, mandate.text
    with gateway.db.connect() as conn:
        record, subject = gateway.mandates.verify(conn, mandate.json()["jwt"], "new-tenant-agent", "1")
    assert subject["autonomy"] == "approval_required"
    assert http.post("/v1/tenant/agents/new-tenant-agent/mandates", headers=admin, json={"customer_id": "2"}).status_code == 403
    assert http.delete('/v1/tenant/agents/new-tenant-agent/mandates/'+mandate.json()["id"], headers=admin).status_code == 200
    assert http.get("/v1/tenant/agents/new-tenant-agent/mandates", headers=admin).json()[0]["revoked"]
    overlay = {"approval": {"max_auto_total_eur": 100}}
    preview = http.put("/v1/tenant/policy", headers=admin, json=overlay).json()
    saved = http.put("/v1/tenant/policy", headers=admin, json={**overlay, "confirmation_token": preview["confirmation_token"]})
    assert saved.status_code == 200 and saved.json()["approval"]["max_auto_total_eur"] == 100
    assert gateway.policy.get().approval.max_auto_total_eur > 100
    assert http.put("/v1/tenant/policy", headers=admin, json={"approval": {"max_auto_total_eur": 100000}}).status_code == 403
    invitation = http.post("/v1/tenant/users", headers=admin, json={"email": "reset@example.test", "role": "viewer"}).json()
    resend = http.post('/v1/tenant/users/'+invitation["user_id"]+'/resend-invite', headers=admin).json()
    assert http.post('/v1/auth/invites/'+invitation["token"]+'/accept', json={"password": PASSWORD}).status_code == 410
    assert http.post('/v1/auth/invites/'+resend["token"]+'/accept', json={"password": PASSWORD}).status_code == 200
    reset = http.post('/v1/tenant/users/'+invitation["user_id"]+'/password-reset', headers=admin).json()
    assert http.post('/v1/auth/password-resets/'+reset["token"]+'/accept', json={"password": PASSWORD+"2"}).status_code == 200
    assert http.post('/v1/auth/password-resets/'+reset["token"]+'/accept', json={"password": PASSWORD}).status_code == 410


def test_scopes_expiry_and_independent_connection_rate_buckets(http, gateway, need, upstream, monkeypatch):
    admin = sign(http)
    first = connect(http, admin, scopes=["read"])
    second = connect(http, admin, scopes=["read"])
    assert http.post("/v1/offers", headers=bearer(first), json=need.model_dump(mode="json")).status_code == 403
    rpc = http.post("/mcp", headers={**bearer(first), "Accept": "application/json, text/event-stream"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "find_offers", "arguments": {"need": need.model_dump(mode="json")}}})
    assert rpc.status_code == 200 and rpc.json()["result"]["structuredContent"]["code"] == "INSUFFICIENT_SCOPE"
    human = connect(http, admin, audience="tenant")
    assert http.post("/mcp", headers={**bearer(human), "Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
    policy = gateway.policy.get().model_dump(mode="json")
    policy["rate_limits"]["read"] = {"capacity": 1, "refill_per_minute": 1}
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Fixture"))
    gateway.limiter.buckets.clear()
    monkeypatch.setattr(gateway.limiter, "clock", lambda: 100.0)
    assert http.get("/v1/orders", headers=bearer(first)).status_code == 200
    assert http.get("/v1/orders", headers=bearer(first)).status_code == 429
    assert http.get("/v1/orders", headers=bearer(second)).status_code == 200
    expired = connect(http, admin, expires_at=(audit.utcnow()+timedelta(minutes=1)).isoformat())
    now = audit.utcnow()
    monkeypatch.setattr(audit, "utcnow", lambda: now+timedelta(minutes=2))
    assert http.get("/v1/me", headers=bearer(expired)).status_code == 401
