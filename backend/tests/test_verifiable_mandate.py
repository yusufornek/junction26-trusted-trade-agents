"""Spec 12: buyer authority and revocation across the shared procurement core."""
import asyncio
import json
import select
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app import audit
from app.api import create_app
from app.db import DomainError
from app.mandates import sign_jwt, decode
from app.mcp_server import create_server
from mcp.server.mcpserver.exceptions import ToolError
from app.models import PolicyUpdateRequest
from app.valio import RequestCounter
from app.signing import b64
from conftest import OPERATOR_HEADERS, agent_headers, quote_request, raw_product, rebuild_catalog

AGENT = "outsider"
ISSUER = "urn:buyer:test"


@pytest.fixture
def buyer(gateway):
    key = Ed25519PrivateKey.generate()
    jwk = {"kid": "buyer-1", "kty": "OKP", "crv": "Ed25519", "alg": "EdDSA", "use": "sig",
           "x": b64(key.public_key().public_bytes_raw())}
    body = gateway.policy.get().model_dump(mode="json")
    body["customers"]["1"] = {"mandate_issuers": {ISSUER: [jwk]}}
    gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Buyer onboarding"), actor="human:operator")
    return key


def issue(key, *, daily=5000, order=1500, categories=None, **updates):
    now = int(audit.utcnow().timestamp())
    subject = {"agent_id": AGENT, "customer_id": "1", "max_auto_total_eur": order,
               "autonomy": "autonomous_within_limits",
               "max_auto_daily_total_eur": daily}
    if categories is not None:
        subject["categories"] = categories
    claims = {"iss": ISSUER, "sub": AGENT, "jti": str(uuid4()), "nbf": now - 1, "exp": now + 86400,
              "vc": {"@context": ["https://www.w3.org/ns/credentials/v2"],
                     "type": ["VerifiableCredential", "PurchasingMandate"], "credentialSubject": subject}, **updates}
    return sign_jwt(key, claims, kid="buyer-1")


def headers(token):
    return {**agent_headers(AGENT), "Agent-Mandate": token}


def quote(gateway, need, token, qty=100):
    lookup = gateway.find_offers(need, f"agent:{AGENT}", mandate=token)
    return gateway.orders.create_quote(quote_request(need, qty=qty, need_ref=lookup.lookup_id), AGENT, mandate=token)


@pytest.mark.parametrize("command,transport", [("lookup", "core"), ("quote", "rest"),
    ("quote", "mcp"), ("order", "rest"), ("order", "mcp"), ("approval", "rest")])
@pytest.mark.parametrize("refusal", ["revoked", "customer_scope"])
def test_review_keyed_replay_refusals_are_audited(gateway, need, upstream, buyer, command, transport, refusal):
    """Spec 12 R-4: every replay guard uses the command's normal refusal audit.

    Lookup keys are core-only; approval is REST-only. Quote/order exercise both
    public transports, including headerless replays of stored buyer authority.
    """
    token = issue(buyer)
    body = gateway.policy._editable(gateway.policy.get())
    body["agents"][AGENT] = {"customer_ids": ["1"], "autonomy": "autonomous_within_limits",
                            "max_auto_daily_total_eur": 5000}
    gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Fixture"))
    lookup = gateway.find_offers(need, f"agent:{AGENT}", mandate=token)
    request = quote_request(need, qty=200, need_ref=lookup.lookup_id)
    payload, path, tool = request.model_dump(mode="json"), "/v1/quotes", "create_quote"
    if command in {"order", "approval"}:
        q = gateway.orders.create_quote(request, AGENT, mandate=token)
        payload, path, tool = {"quote_id": q.id}, "/v1/orders", "place_order"
        if command == "approval":
            pending = gateway.orders.place_order(q.id, "setup-order", AGENT)
            assert pending.status == "PENDING_APPROVAL"
            payload, path = {"approver": "Ada"}, f"/v1/approvals/{pending.id}/approve"

    with TestClient(create_app(gateway)) as client:
        def call(present=False):
            if transport == "core":
                try:
                    return 200, gateway.find_offers(need, f"agent:{AGENT}",
                        mandate=token if present else None, command_key="review-audit").model_dump(mode="json")
                except DomainError as error:
                    return error.status, {"code": error.code, "data": error.data}
            if transport == "mcp":
                result = asyncio.run(create_server(gateway, AGENT, mandate=token if present else None)
                    .call_tool(tool, {**payload, "idempotency_key": "review-audit"}))
                return (403 if result.is_error else 200), result.structured_content
            auth = OPERATOR_HEADERS if command == "approval" else headers(token) if present else agent_headers(AGENT)
            result = client.post(path, json=payload, headers={**auth, "Idempotency-Key": "review-audit"})
            return result.status_code, result.json()

        first = call(present=True)
        assert first[0] == 200, first
        assert call() == first
        if refusal == "revoked":
            claims = {"iss": ISSUER, "jti_revoked": [decode(token)[1]["jti"]], "iat": int(audit.utcnow().timestamp())}
            gateway.mandates.revoke(sign_jwt(buyer, claims, kid="buyer-1"), f"agent:{AGENT}")
        else:
            body["agents"][AGENT]["customer_ids"] = ["2"]
            gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Scope removed"))
        with gateway.db.connect() as conn:
            before = {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
                      for table in ("lookups", "quotes", "orders", "reservations", "commands", "events")}
        audit_before = len(gateway.audit())
        calls = len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls)
        status, denied = call()
        code = "MANDATE_INVALID" if refusal == "revoked" else "CUSTOMER_NOT_IN_MANDATE"
        assert (status, denied["code"]) == (403, code)
        if refusal == "revoked":
            assert denied["data"]["check"] == "revoked"
        events = gateway.audit()[audit_before:]
        action = {"lookup": "LOOKUP", "quote": "CREATE_QUOTE", "order": "PLACE_ORDER", "approval": "APPROVE"}[command]
        assert [event.action for event in events] == [action + "_FAILED"]
        event = events[0]
        assert event.actor == ("human:operator" if command == "approval" else f"agent:{AGENT}")
        assert event.data["error"] == code
        assert event.data["live_calls"] == {"price": 0, "availability": 0}
        assert event.data["inputs"]
        if command == "approval":
            assert event.order_id == pending.id and event.data["stated_name"] == "Ada"
        assert calls == (len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls))
        with gateway.db.connect() as conn:
            assert before == {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
                              for table in before}


@pytest.mark.parametrize("command", ["quote", "lookup"])
def test_review_bound_mandate_checked_on_headerless_command_replay(gateway, need, upstream, buyer, command):
    claims = decode(issue(buyer, sub="external"))[1]
    claims["vc"]["credentialSubject"]["agent_id"] = "external"
    token = sign_jwt(buyer, claims, kid="buyer-1")
    if command == "quote":
        # The VC is first presented at quote time, not on the bound lookup.
        lookup = gateway.find_offers(need)
        request = quote_request(need, need_ref=lookup.lookup_id, qty=1)
        def call(mandate):
            return gateway.orders.create_quote(request, mandate=mandate, command_key="bound-command")
    else:
        def call(mandate):
            return gateway.find_offers(need, mandate=mandate, command_key="bound-command")
    first = call(token)
    revocation = sign_jwt(buyer, {"iss": ISSUER, "jti_revoked": [claims["jti"]],
        "iat": int(audit.utcnow().timestamp())}, kid="buyer-1")
    gateway.mandates.revoke(revocation, "agent:external")
    before = len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls)
    with pytest.raises(DomainError, match="MANDATE_INVALID") as denied:
        call(None)
    assert denied.value.data["check"] == "revoked"
    assert before == (len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls))
    assert first.agent_id == "external"


def test_review_search_only_mandate_stats(gateway, buyer):
    token = issue(buyer)
    gateway.mandates.search("milk", [], [], 1, token=token, agent_id=AGENT)
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        response = client.get("/v1/stats")
    assert response.status_code == 200
    row = next(a for a in response.json()["agents_seen"] if a["actor"] == f"agent:{AGENT}")
    assert row["registered"] is True
    assert row["mandate_id"] == decode(token)[1]["jti"]
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM mandate_credentials").fetchone()[0] == 1


@pytest.mark.parametrize("operation", ["search", "lookup"])
def test_review_latest_successful_mandate_use(gateway, need, upstream, buyer, operation):
    first, second = issue(buyer), issue(buyer)
    def use(token):
        if operation == "search":
            gateway.mandates.search("milk", [], [], 1, token=token, agent_id=AGENT)
        else:
            gateway.find_offers(need, f"agent:{AGENT}", mandate=token)
    for token in (first, second, first):
        use(token)
    # Failed use must not displace the last successful authority.
    bad = issue(buyer, exp=int(audit.utcnow().timestamp()) - 1)
    with pytest.raises(DomainError) as error:
        use(bad)
    assert error.value.code == "MANDATE_INVALID"
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        row = next(a for a in client.get("/v1/stats").json()["agents_seen"] if a["actor"] == f"agent:{AGENT}")
    assert row["registered"] is True
    assert row["mandate_id"] == decode(first)[1]["jti"]
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM mandate_credentials").fetchone()[0] == 2


def test_review_legacy_mandate_credentials_survive_usage_migration(gateway, buyer, tmp_path):
    import hashlib
    import sqlite3
    from app.db import Database
    from app.mandates import Mandates
    from app.stats import read_stats
    database = tmp_path / "legacy-credentials.db"
    first, second = issue(buyer), issue(buyer)
    digests = [hashlib.sha256(t.encode()).hexdigest() for t in (first, second)]
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE mandate_credentials (digest TEXT PRIMARY KEY, jwt TEXT NOT NULL)")
        conn.executemany("INSERT INTO mandate_credentials VALUES(?,?)", zip(digests, (first, second)))
        conn.execute("CREATE TABLE object_mandates (kind TEXT NOT NULL, object_id TEXT NOT NULL, "
                     "digest TEXT NOT NULL REFERENCES mandate_credentials(digest), PRIMARY KEY(kind,object_id))")
        conn.execute("INSERT INTO object_mandates VALUES('quote','historical',?)", (digests[1],))
    db = Database(database)
    mandates = Mandates(db, gateway.policy, gateway.catalog)
    with db.connect(write=True) as conn:
        assert mandates.stored(conn, "quote", "historical") == second
        audit.append(conn, f"agent:{AGENT}", "LOOKUP_SEARCH", "Historical successful search")
    assert read_stats(db, mandates=mandates).agents_seen[0].mandate_id == decode(second)[1]["jti"]
    mandates.search("milk", [], [], 1, token=first, agent_id=AGENT)
    restarted = Database(database)
    assert read_stats(restarted, mandates=mandates).agents_seen[0].mandate_id == decode(first)[1]["jti"]
    with restarted.connect() as conn:
        assert mandates.stored(conn, "quote", "historical") == second
        assert conn.execute("SELECT count(*) FROM mandate_credentials").fetchone()[0] == 2


def test_ac_1_valid_buyer_authority_without_policy_row(gateway, need, upstream, buyer):
    token = issue(buyer)
    assert AGENT not in gateway.policy.get().agents
    with TestClient(create_app(gateway), headers=headers(token)) as client:
        lookup = client.post("/v1/offers", json=need.model_dump(mode="json")).json()
        result = client.post("/v1/quotes", json=quote_request(need, qty=100, need_ref=lookup["lookup_id"]).model_dump(mode="json"))
        assert result.status_code == 200, result.text
        q = result.json()
        result = client.post("/v1/orders", json={"quote_id": q["id"]}, headers={"Idempotency-Key": "vc"})
        assert result.status_code == 200, result.text
        order = result.json()
        assert order["status"] == "CONFIRMED"
        mandate_id = decode(token)[1]["jti"]
        assert order["mandate"]["id"] == q["mandate"]["id"] == lookup["mandate"]["id"] == mandate_id
        assert order["mandate"]["issuer"] == ISSUER
        assert order["mandate"]["limits"] == {"max_auto_total_eur": 1500, "max_auto_daily_total_eur": 5000}
        events = [e for e in gateway.audit() if e.action in ("LOOKUP", "CREATE_QUOTE", "PLACE_ORDER")]
        assert len(events) == 3 and all(e.data["mandate"]["id"] == mandate_id for e in events)
        stats = client.get("/v1/stats", headers=OPERATOR_HEADERS).json()
        assert stats["agents_seen"][0]["mandate_id"] == mandate_id
        assert "mandate_issuers" in json.dumps(gateway.audit()[0].data["diff"])
        pending_quote = quote(gateway, need, token, qty=200)
        pending = gateway.orders.place_order(pending_quote.id, "human-vc", AGENT, mandate=token)
        assert pending.status == "PENDING_APPROVAL"
        approved = client.post(f"/v1/approvals/{pending.id}/approve", headers=OPERATOR_HEADERS, json={"approver": "Ada"})
        assert approved.status_code == 200 and approved.json()["status"] == "CONFIRMED", approved.text
        assert gateway.audit()[-1].data["mandate"]["id"] == mandate_id


@pytest.mark.parametrize("check", ["exp", "nbf", "signature", "issuer", "sub", "format"])
def test_ac_2_invalid_checks_are_named_audited_on_every_step(gateway, need, upstream, buyer, check, monkeypatch):
    valid = issue(buyer)
    q = quote(gateway, need, valid)
    claims = decode(valid)[1]
    if check == "exp": claims["exp"] = int(audit.utcnow().timestamp())
    if check == "nbf": claims["nbf"] = int(audit.utcnow().timestamp()) + 600
    if check == "issuer": claims["iss"] = "unknown"
    if check == "sub": claims["sub"] = "other"
    bad = sign_jwt(Ed25519PrivateKey.generate() if check == "signature" else buyer, claims, kid="buyer-1")
    if check == "format": bad = "malformed"
    calls = len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls)
    with TestClient(create_app(gateway), headers=headers(bad)) as client:
        for path, body in (("/v1/offers", need.model_dump(mode="json")),
                           ("/v1/quotes", quote_request(need, qty=100, need_ref=q.need_ref).model_dump(mode="json")),
                           ("/v1/orders", {"quote_id": q.id})):
            result = client.post(path, json=body, headers={"Idempotency-Key": "bad"})
            assert result.status_code == 403, result.text
            assert result.json()["code"] == "MANDATE_INVALID" and result.json()["data"]["check"] == check
            event = gateway.audit()[-1]
            assert event.data["error"] == "MANDATE_INVALID" and event.data["check"] == check
    assert calls == (len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls))
    tool_failure = asyncio.run(create_server(gateway, AGENT, mandate=bad).call_tool("search_products", {"query": "milk"}))
    assert tool_failure.is_error
    assert tool_failure.structured_content["code"] == "MANDATE_INVALID"
    assert gateway.audit()[-1].data["check"] == check
    if check == "exp":
        # Expiry also binds human approval; it cannot disappear when the header is omitted.
        short = issue(buyer, order=500, exp=int(audit.utcnow().timestamp()) + 10)
        pending_quote = quote(gateway, need, short)
        pending = gateway.orders.place_order(pending_quote.id, "short-lived", AGENT, mandate=short)
        later = audit.utcnow() + timedelta(seconds=11)
        monkeypatch.setattr(audit, "utcnow", lambda: later)
        with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
            result = client.post(f"/v1/approvals/{pending.id}/approve", json={"approver": "Ada"})
            assert result.status_code == 403 and result.json()["data"]["check"] == "exp"


def test_ac_3_customer_binding(gateway, need, upstream, buyer):
    with gateway.db.connect(write=True) as conn:
        conn.execute("INSERT OR IGNORE INTO tenant_customers VALUES('3','hotel-aurora')")
    token = issue(buyer)
    with TestClient(create_app(gateway), headers=headers(token)) as client:
        other = need.model_copy(update={"customer_id": "3"})
        result = client.post("/v1/offers", json=other.model_dump(mode="json"))
        assert result.status_code == 403 and result.json()["data"]["check"] == "customer"
        result = client.post("/v1/quotes", json=quote_request(other, qty=1, need_ref="missing").model_dump(mode="json"))
        assert result.status_code == 403 and result.json()["data"]["check"] == "customer"
    assert not upstream.price_calls


@pytest.mark.parametrize("policy_daily,expected,confirmed", [(2000, 2000, 1), (10000, 5000, 3)])
def test_ac_4_effective_limits_only_tighten(gateway, need, upstream, buyer, policy_daily, expected, confirmed):
    body = gateway.policy.get().model_dump(mode="json")
    body["agents"][AGENT] = {"customer_ids": ["1"], "autonomy": "autonomous_within_limits", "max_auto_total_eur": 3000, "max_auto_daily_total_eur": policy_daily}
    gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Ada"))
    token = issue(buyer)
    for i in range(confirmed + 1):
        q = quote(gateway, need, token)
        order = gateway.orders.place_order(q.id, f"limits-{i}", AGENT, mandate=token)
        assert order.mandate.limits["max_auto_daily_total_eur"] == expected
        assert order.mandate.limits["max_auto_total_eur"] == 1500
        assert order.status == ("CONFIRMED" if i < confirmed else "PENDING_APPROVAL")
    assert any(h.rule == "max_auto_daily_total_eur" for h in order.policy.hits)
    body["agents"][AGENT]["max_auto_total_eur"] = 500
    gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Ada"))
    q = quote(gateway, need, token)
    assert gateway.orders.place_order(q.id, "per-order", AGENT).mandate.limits["max_auto_total_eur"] == 500


def test_ac_5_signed_revocation_blocks_quote_replay_and_pending_approval(gateway, need, upstream, buyer):
    token = issue(buyer, order=500)
    q = quote(gateway, need, token)
    keyed_body = quote_request(need, need_ref=q.need_ref).model_dump(mode="json")
    with TestClient(create_app(gateway)) as client:
        keyed_quote = client.post("/v1/quotes", json=keyed_body,
            headers={**headers(token), "Idempotency-Key": "signed-quote"})
        assert keyed_quote.status_code == 200
    order = gateway.orders.place_order(q.id, "pending", AGENT, mandate=token)
    assert order.status == "PENDING_APPROVAL"
    claims = {"iss": ISSUER, "jti_revoked": [decode(token)[1]["jti"]], "iat": int(audit.utcnow().timestamp())}
    with TestClient(create_app(gateway), headers=agent_headers(AGENT)) as client:
        wrong = sign_jwt(Ed25519PrivateKey.generate(), claims, kid="buyer-1", typ="JWT")
        assert client.post("/v1/mandates/revocations", json={"jwt": wrong}).status_code == 422
        signed = sign_jwt(buyer, claims, kid="buyer-1", typ="JWT")
        assert client.post("/v1/mandates/revocations", json={"jwt": signed}).status_code == 200
        for path, body, extra in (("/v1/quotes", keyed_body, {"Idempotency-Key": "signed-quote"}),
                                  ("/v1/quotes", quote_request(need, need_ref=q.need_ref).model_dump(mode="json"), headers(token)),
                                  ("/v1/orders", {"quote_id": q.id}, {"Idempotency-Key": "pending"}),
                                  (f"/v1/approvals/{order.id}/approve", {"approver": "Ada"}, OPERATOR_HEADERS)):
            before = len(gateway.audit())
            result = client.post(path, json=body, headers=extra)
            assert result.status_code == 403 and result.json()["data"]["check"] == "revoked", result.text
            action = "CREATE_QUOTE_FAILED" if path == "/v1/quotes" else "PLACE_ORDER_FAILED" if path == "/v1/orders" else "APPROVE_FAILED"
            events = gateway.audit()[before:]
            assert [event.action for event in events] == [action]
            assert events[0].data["error"] == "MANDATE_INVALID" and events[0].data["check"] == "revoked"
            assert events[0].data["live_calls"] == {"price": 0, "availability": 0}
        # Revocation must still allow a human to reject the pending order.
        assert client.post(f"/v1/approvals/{order.id}/reject", json={"approver": "Ada"}, headers=OPERATOR_HEADERS).status_code == 200
    assert gateway.audit()[-2].data["error"] == "MANDATE_INVALID"


def test_ac_6_category_restriction_on_every_line(gateway, need, upstream, buyer):
    token = issue(buyer, categories=["10400"])
    q = quote(gateway, need, token)
    pending_quote = quote(gateway, need, token, qty=200)
    pending = gateway.orders.place_order(pending_quote.id, "category-pending", AGENT, mandate=token)
    with gateway.db.connect(write=True) as conn:
        conn.execute("UPDATE products SET category='OTHER',data=json_set(data,'$.category','OTHER') WHERE id='1'")
    with TestClient(create_app(gateway), headers=headers(token)) as client:
        for path, body in (("/v1/quotes", quote_request(need, qty=1, need_ref=q.need_ref).model_dump(mode="json")),
                           ("/v1/orders", {"quote_id": q.id})):
            result = client.post(path, json=body, headers={"Idempotency-Key": "category"})
            assert result.status_code == 403 and result.json()["data"]["check"] == "category", result.text
        lookup = client.post("/v1/offers", json=need.model_dump(mode="json")).json()
        assert "1" not in [o["product_id"] for o in lookup["offers"]]
        result = client.post(f"/v1/approvals/{pending.id}/approve", headers=OPERATOR_HEADERS, json={"approver": "Ada"})
        assert result.status_code == 403 and result.json()["data"]["check"] == "category"



def test_ac_7_demo_issuer_and_revocation_with_temporary_keys(gateway, need, upstream, tmp_path):
    from scripts.issue_mandate import main
    out = tmp_path / "mandate.jwt"
    keydir = tmp_path / "buyer-keys"
    args = ["--keys-dir", str(keydir), "--out", str(out)]
    assert main(args + ["--customer", "1", "--agent", AGENT, "--order-limit", "1500", "--daily-limit", "5000", "--days", "1", "--category", "10400"]) == 0
    token = out.read_text().strip()
    header, claims, _, _ = decode(token)
    from cryptography.hazmat.primitives import serialization
    keyfile, = keydir.glob("*.pem")
    assert keyfile.stat().st_mode & 0o777 == 0o600
    key = serialization.load_pem_private_key(keyfile.read_bytes(), password=None)
    body = gateway.policy.get().model_dump(mode="json")
    body["customers"]["1"] = {"mandate_issuers": {claims["iss"]: [{"kid": header["kid"], "x": b64(key.public_key().public_bytes_raw())}]}}
    gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Ada"))
    q = quote(gateway, need, token)
    assert q.mandate.id == claims["jti"]
    assert main(args + ["--revoke", claims["jti"]]) == 0
    with TestClient(create_app(gateway), headers=agent_headers(AGENT)) as client:
        assert client.post("/v1/mandates/revocations", json={"jwt": out.read_text().strip()}).status_code == 200
        response = client.post("/v1/orders", json={"quote_id": q.id}, headers={"Idempotency-Key": "script"})
        assert response.status_code == 403 and response.json()["data"]["check"] == "revoked"
    assert len(list(keydir.glob("*.pem"))) == 1


def test_ac_8_stdio_cli_mandate_and_http_header(gateway, need, upstream, buyer, tmp_path):
    token = issue(buyer, order=500)
    q = quote(gateway, need, token)
    mandate_file = tmp_path / "mandate.jwt"
    mandate_file.write_text(token)
    # Run the real CLI argument parser and transport; inject only fixture paths and recorded stock.
    stock = gateway.valio.fetch("availability", "1", need.deliver_by, ["1"], RequestCounter(), fresh=True)
    runner = '''
import json, sys
from pathlib import Path
from app.config import Settings
from app.service import Gateway
from app import mcp_server
settings = Settings(db_path=Path(sys.argv[1]), policy_path=Path(sys.argv[2]))
gateway = Gateway(settings)
stock = json.loads(sys.argv[3])
gateway.valio.fetch = lambda *a, **k: stock
mcp_server.Gateway = lambda: gateway
sys.argv = ["mcp", "--agent-id", "outsider", "--mandate", sys.argv[4]]
mcp_server.main()
'''
    error_log = tmp_path / "mcp-stderr.log"
    stderr_file = error_log.open("w")
    process = subprocess.Popen([sys.executable, "-c", runner, str(gateway.settings.db_path), str(gateway.settings.policy_path), json.dumps(stock), str(mandate_file)],
                               cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr_file, text=True)
    def rpc(message):
        process.stdin.write(json.dumps(message) + "\n"); process.stdin.flush()
        ready, _, _ = select.select([process.stdout], [], [], 10)
        assert ready, "MCP stdio timeout"
        return json.loads(process.stdout.readline())
    try:
        rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"); process.stdin.flush()
        # No policy row or bound object can authorize this fresh catalog search.
        found = rpc({"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "search_products", "arguments": {"query": "milk"}}})
        assert not found["result"].get("isError"), (found, error_log.read_text())
        searches = [e for e in gateway.audit() if e.action == "LOOKUP_SEARCH"]
        assert searches, "CLI must load the mandate for a fresh search"
        assert searches[-1].data["mandate"]["id"] == decode(token)[1]["jti"]
        assert searches[-1].data["mandate"]["limits"]["max_auto_total_eur"] == 500
        result = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "place_order", "arguments": {"quote_id": q.id, "idempotency_key": "stdio-vc"}}})
        assert not result["result"].get("isError"), (result, error_log.read_text())
        order = result["result"]["structuredContent"]
        assert order["status"] == "PENDING_APPROVAL" and order["mandate"]["limits"]["max_auto_total_eur"] == 500
    finally:
        process.stdin.close()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired: process.kill(); process.wait()
        process.stdout.close(); stderr_file.close()
    assert process.returncode == 0
    # HTTP presentation must be read from this tool call's request, never another session.
    app = create_server(gateway, AGENT).streamable_http_app(stateless_http=True, json_response=True)
    with TestClient(app, base_url="http://localhost:8001", headers={**headers(token), "Accept": "application/json, text/event-stream"}) as client:
        result = client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "find_offers", "arguments": {"need": need.model_dump(mode="json")}}})
        assert result.status_code == 200, result.text
        assert result.json()["result"]["structuredContent"]["mandate"]["id"] == decode(token)[1]["jti"]
        client.headers.pop("Agent-Mandate")
        result = client.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "find_offers", "arguments": {"need": need.model_dump(mode="json")}}})
        assert result.json()["result"]["isError"]


@pytest.mark.parametrize("other_kid", ["buyer-1", "buyer-2"])
def test_review_revocation_respects_customer_signing_key(gateway, need, upstream, buyer, other_kid):
    other_key = Ed25519PrivateKey.generate()
    body = gateway.policy.get().model_dump(mode="json")
    body["customers"]["2"] = {"mandate_issuers": {ISSUER: [{"kid": other_kid, "x": b64(other_key.public_key().public_bytes_raw())}]}}
    gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Ada"))
    token1 = issue(buyer, jti="shared-id")
    claims = decode(token1)[1]
    claims["vc"]["credentialSubject"]["customer_id"] = "2"
    token2 = sign_jwt(other_key, claims, kid=other_kid)
    revoke = sign_jwt(buyer, {"iss": ISSUER, "jti_revoked": ["shared-id"], "iat": int(audit.utcnow().timestamp())}, kid="buyer-1", typ="JWT")
    gateway.mandates.revoke(revoke, "agent:outsider")
    with gateway.db.connect() as conn:
        assert gateway.mandates.verify(conn, token2, AGENT, "2")[0].id == "shared-id"
        from app.db import DomainError
        with pytest.raises(DomainError, match="revoked"):
            gateway.mandates.verify(conn, token1, AGENT, "1")


def test_review_ac_8_requires_cli_mandate_file(gateway, need, upstream, buyer, tmp_path, monkeypatch):
    original = subprocess.Popen
    def without_mandate(command, *args, **kwargs):
        command = list(command)
        command[2] = command[2].replace(', "--mandate", sys.argv[4]', '')
        return original(command, *args, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", without_mandate)
    with pytest.raises(AssertionError):
        test_ac_8_stdio_cli_mandate_and_http_header(gateway, need, upstream, buyer, tmp_path)


def test_review_order_bound_mandate_checked_on_headerless_replay(gateway, need, upstream, buyer):
    from test_signed_quotes import make_quote
    token = issue(buyer, sub="external")
    claims = decode(token)[1]
    claims["vc"]["credentialSubject"]["agent_id"] = "external"
    token = sign_jwt(buyer, claims, kid="buyer-1")
    q = make_quote(gateway, need)
    order = gateway.orders.place_order(q.id, "placement-only", mandate=token)
    revoke = sign_jwt(buyer, {"iss": ISSUER, "jti_revoked": [claims["jti"]], "iat": int(audit.utcnow().timestamp())}, kid="buyer-1", typ="JWT")
    gateway.mandates.revoke(revoke, "agent:external")
    before = len(upstream.availability_calls)
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        response = client.post("/v1/orders", json={"quote_id": q.id}, headers={"Idempotency-Key": "placement-only"})
    assert response.status_code == 403 and response.json()["data"]["check"] == "revoked"
    assert len(upstream.availability_calls) == before
    assert gateway.orders.get_order(order.id) == order
    assert gateway.audit()[-1].data["check"] == "revoked"


@pytest.mark.parametrize("operation", ["search", "lookup", "broadened"])
def test_review_category_filter_precedes_limits_and_counts(gateway, need, upstream, buyer, tmp_path, operation):
    rows = [raw_product(str(i).zfill(2), "Milk unauthorized") for i in range(40)]
    for row in rows: row["category"] = "OTHER"
    rows.append(raw_product("z", "Milk eligible"))
    vocabulary = raw_product("egg-vocabulary", "Egg declaration vocabulary", egg=True)
    vocabulary["category"] = "OTHER"
    rows.append(vocabulary)
    rebuild_catalog(gateway, tmp_path, rows)
    token = issue(buyer, categories=["10400"])
    if operation == "search":
        products = gateway.mandates.search("milk", [], [], 1, token=token, agent_id=AGENT)
        assert [p.id for p in products] == ["z"]
    else:
        if operation == "broadened":
            need = need.model_copy(update={"search_terms": [], "intent": "1000 litres milk for hotel"})
        result = gateway.find_offers(need, f"agent:{AGENT}", mandate=token)
        assert [o.product_id for o in result.offers] == ["z"]
        assert result.matched_count == result.screened_count == result.priced_count == 1
        assert "POOL_TRUNCATED" not in [w.code for w in result.warnings]
        assert (result.retrieval.mode == "intent_broadened") == (operation == "broadened")


@pytest.mark.parametrize("withdrawal", ["revoked", "expired", "issuer_removed"])
@pytest.mark.parametrize("policy_row", [False, True])
def test_review_stats_registration_uses_current_authority(gateway, need, upstream, buyer, monkeypatch, withdrawal, policy_row):
    token = issue(buyer)
    q = quote(gateway, need, token, qty=1)
    if policy_row:
        body = gateway.policy.get().model_dump(mode="json")
        body["agents"][AGENT] = {"customer_ids": ["1"], "autonomy": "autonomous_within_limits", "max_auto_daily_total_eur": 5000}
        gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Ada"))
    if withdrawal == "revoked":
        revoke = sign_jwt(buyer, {"iss": ISSUER, "jti_revoked": [decode(token)[1]["jti"]], "iat": int(audit.utcnow().timestamp())}, kid="buyer-1", typ="JWT")
        gateway.mandates.revoke(revoke, "agent:outsider")
    elif withdrawal == "expired":
        later = audit.utcnow() + timedelta(days=2)
        monkeypatch.setattr(audit, "utcnow", lambda: later)
    else:
        body = gateway.policy.get().model_dump(mode="json")
        body["customers"]["1"]["mandate_issuers"] = {}
        gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Ada"))
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        stats = client.get("/v1/stats").json()
    row = next(a for a in stats["agents_seen"] if a["actor"] == f"agent:{AGENT}")
    assert row["registered"] is policy_row
    assert row.get("mandate_id") is None
    assert gateway.orders.get_quote(q.id).mandate.id == decode(token)[1]["jti"]  # history remains readable


@pytest.mark.parametrize("field", ["max_auto_total_eur", "max_auto_daily_total_eur", "nbf", "exp"])
def test_review_oversized_jwt_integer_is_audited_refusal(gateway, need, upstream, buyer, field):
    claims = decode(issue(buyer))[1]
    target = claims if field in ("nbf", "exp") else claims["vc"]["credentialSubject"]
    target[field] = 10**400
    bad = sign_jwt(Ed25519PrivateKey.generate(), claims, kid="buyer-1")
    with TestClient(create_app(gateway), headers=headers(bad), raise_server_exceptions=False) as client:
        result = client.post("/v1/offers", json=need.model_dump(mode="json"))
    assert result.status_code == 403 and result.json()["code"] == "MANDATE_INVALID"
    assert gateway.audit()[-1].data["error"] == "MANDATE_INVALID"
    assert not upstream.price_calls and not upstream.availability_calls


def test_legacy_revocations_migrate_without_restoring_authority(gateway, buyer, tmp_path):
    import sqlite3
    from app.db import Database, DomainError
    database = tmp_path / "legacy-revocations.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE mandate_revocations (issuer TEXT NOT NULL, mandate_id TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY(issuer,mandate_id))")
        conn.execute("INSERT INTO mandate_revocations VALUES(?,?,?)", (ISSUER, "old-revoked", audit.utcnow().isoformat()))
    token = issue(buyer, jti="old-revoked")
    for _ in range(2):
        db = Database(database)
        with db.connect() as conn:
            row, = conn.execute("SELECT * FROM mandate_revocations")
            assert row["customer_id"] == "" and row["mandate_id"] == "old-revoked"
            with pytest.raises(DomainError, match="revoked"):
                gateway.mandates.verify(conn, token, AGENT, "1")


@pytest.mark.parametrize('transport', ['rest', 'mcp'])
def test_review_cached_quote_revalidates_revocation(gateway, need, upstream, buyer, transport):
    token = issue(buyer)
    lookup = gateway.find_offers(need, f'agent:{AGENT}', mandate=token)
    body = quote_request(need, qty=1, need_ref=lookup.lookup_id).model_dump(mode='json')
    with TestClient(create_app(gateway), headers=headers(token)) as client:
        first = client.post('/v1/quotes', json=body, headers={'Idempotency-Key': 'cached-authority'})
        assert first.status_code == 200, first.text
        revoke = sign_jwt(buyer, {'iss': ISSUER, 'jti_revoked': [decode(token)[1]['jti']],
                               'iat': int(audit.utcnow().timestamp())}, kid='buyer-1', typ='JWT')
        assert client.post('/v1/mandates/revocations', json={'jwt': revoke}).status_code == 200
        before = len(upstream.price_calls), len(upstream.availability_calls)
        if transport == 'rest':
            replay = client.post('/v1/quotes', json=body, headers={'Idempotency-Key': 'cached-authority'})
            assert replay.status_code == 403, replay.text
            payload = replay.json()
        else:
            replay = asyncio.run(create_server(gateway, AGENT, mandate=token).call_tool('create_quote', {**body, 'idempotency_key': 'cached-authority'}))
            assert replay.is_error
            payload = replay.structured_content
        assert payload['code'] == 'MANDATE_INVALID' and payload['data']['check'] == 'revoked'
        assert before == (len(upstream.price_calls), len(upstream.availability_calls))
    assert gateway.audit()[-1].data['error'] == 'MANDATE_INVALID'
