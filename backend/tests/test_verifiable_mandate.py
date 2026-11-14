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
from app.mandates import sign_jwt, decode
from app.mcp_server import create_server
from mcp.server.mcpserver.exceptions import ToolError
from app.models import PolicyUpdateRequest
from app.valio import RequestCounter
from app.signing import b64
from conftest import OPERATOR_HEADERS, agent_headers, quote_request

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
    with pytest.raises(ToolError, match="MANDATE_INVALID"):
        asyncio.run(create_server(gateway, AGENT, mandate=bad).call_tool("search_products", {"query": "milk"}))
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
    token = issue(buyer)
    with TestClient(create_app(gateway), headers=headers(token)) as client:
        other = need.model_copy(update={"customer_id": "2"})
        result = client.post("/v1/offers", json=other.model_dump(mode="json"))
        assert result.status_code == 403 and result.json()["data"]["check"] == "customer"
        result = client.post("/v1/quotes", json=quote_request(other, qty=1, need_ref="missing").model_dump(mode="json"))
        assert result.status_code == 403 and result.json()["data"]["check"] == "customer"
    assert not upstream.price_calls


@pytest.mark.parametrize("policy_daily,expected,confirmed", [(2000, 2000, 1), (10000, 5000, 3)])
def test_ac_4_effective_limits_only_tighten(gateway, need, upstream, buyer, policy_daily, expected, confirmed):
    body = gateway.policy.get().model_dump(mode="json")
    body["agents"][AGENT] = {"customer_ids": ["1"], "max_auto_total_eur": 3000, "max_auto_daily_total_eur": policy_daily}
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
    order = gateway.orders.place_order(q.id, "pending", AGENT, mandate=token)
    assert order.status == "PENDING_APPROVAL"
    claims = {"iss": ISSUER, "jti_revoked": [decode(token)[1]["jti"]], "iat": int(audit.utcnow().timestamp())}
    with TestClient(create_app(gateway), headers=agent_headers(AGENT)) as client:
        wrong = sign_jwt(Ed25519PrivateKey.generate(), claims, kid="buyer-1", typ="JWT")
        assert client.post("/v1/mandates/revocations", json={"jwt": wrong}).status_code == 422
        signed = sign_jwt(buyer, claims, kid="buyer-1", typ="JWT")
        assert client.post("/v1/mandates/revocations", json={"jwt": signed}).status_code == 200
        for path, body, extra in (("/v1/quotes", quote_request(need, need_ref=q.need_ref).model_dump(mode="json"), headers(token)),
                                  ("/v1/orders", {"quote_id": q.id}, {"Idempotency-Key": "pending"}),
                                  (f"/v1/approvals/{order.id}/approve", {"approver": "Ada"}, OPERATOR_HEADERS)):
            result = client.post(path, json=body, headers=extra)
            assert result.status_code == 403 and result.json()["data"]["check"] == "revoked", result.text
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
