"""Spec 10: signatures, rotation, offline verification and explicit commitments."""
import base64
from copy import deepcopy
from datetime import datetime, timedelta
import json
import struct
import subprocess
import sys

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi.testclient import TestClient
import pytest
import yaml

from app import audit
from app.api import create_app
from app.canonical import canonical_json
from app.config import BACKEND
from app.signing import quote_payload, receipt_payload, signature_digest
from conftest import agent_headers, quote_request


def independent_verify(signature, payload, jwks):
    header, detached, sig = signature.split(".")
    assert detached == ""
    decode = lambda v: base64.urlsafe_b64decode(v + "=" * (-len(v) % 4))
    protected = json.loads(decode(header))
    assert protected["alg"] == "EdDSA"
    key = next(k for k in jwks["keys"] if k["kid"] == protected["kid"])
    assert key["crv"] == "Ed25519" and key["kty"] == "OKP" and "d" not in key
    payload64 = base64.urlsafe_b64encode(canonical_json(payload)).rstrip(b"=")
    Ed25519PublicKey.from_public_bytes(decode(key["x"])).verify(decode(sig), header.encode() + b"." + payload64)


def make_quote(gateway, need, qty=1):
    lookup = gateway.find_offers(need)
    return gateway.orders.create_quote(quote_request(need, qty=qty, need_ref=lookup.lookup_id))


def test_ac_1_quote_jwk_and_each_signed_field(gateway, need, upstream):
    quote = make_quote(gateway, need)
    with TestClient(create_app(gateway)) as client:
        response = client.get("/.well-known/jwks.json")
        assert response.status_code == 200  # deliberately no auth
        jwks = response.json()
    payload = quote_payload(quote)
    assert set(payload) == {"id", "customer_id", "deliver_by", "lines", "total", "expires_at", "price_commitment"}
    assert set(payload["lines"][0]) == {"product_id", "units", "unit_price", "currency", "gtin", "provenance"}
    assert all(isinstance(payload["lines"][0][k], str) for k in ("units", "unit_price"))
    independent_verify(quote.signature, payload, jwks)
    for field in set(payload) - {"lines"}:
        changed = deepcopy(payload)
        changed[field] += "tamper"
        with pytest.raises(InvalidSignature):
            independent_verify(quote.signature, changed, jwks)
    for field in set(payload["lines"][0]) - {"provenance"}:
        changed = deepcopy(payload)
        changed["lines"][0][field] = "tamper"
        with pytest.raises(InvalidSignature):
            independent_verify(quote.signature, changed, jwks)
    for source in ("price", "availability"):
        for field in ("source", "fetched_at", "valid_to"):
            changed = deepcopy(payload)
            changed["lines"][0]["provenance"][source][field] = "tamper"
            with pytest.raises(InvalidSignature):
                independent_verify(quote.signature, changed, jwks)


@pytest.mark.parametrize("field", ["id", "customer_id", "deliver_by", "total", "expires_at", "units_needed",
                                  "unit_price", "currency", "gtin", "provenance", "price_commitment", "signature"])
def test_ac_2_stored_tampering_refused_and_audited(gateway, need, upstream, field):
    quote = make_quote(gateway, need)
    data = quote.model_dump(mode="json")
    if field in {"units_needed", "unit_price"}:
        data["lines"][0][field] += 1
    elif field == "currency":
        data["lines"][0][field] = "USD"
    elif field == "gtin":
        data["lines"][0][field] = "00000000000001"
    elif field == "provenance":
        data["lines"][0][field]["price"]["source"] = "forged"
    elif field == "signature":
        data[field] = None  # legacy unsigned quotes also fail closed
    elif field == "expires_at":
        data[field] = "malformed-time"
    elif field == "deliver_by":
        data[field] = str(need.deliver_by + timedelta(days=1))
    elif field == "total":
        data[field] += 1
    else:
        data[field] = "changed"
    with gateway.db.connect(write=True) as conn:
        conn.execute("UPDATE quotes SET data=? WHERE id=?", (json.dumps(data), quote.id))
    before = len(upstream.availability_calls)
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        response = client.post("/v1/orders", json={"quote_id": quote.id}, headers={"Idempotency-Key": "tamper"})
        assert response.status_code == 409 and response.json()["code"] == "QUOTE_SIGNATURE_INVALID"
    assert len(upstream.availability_calls) == before
    assert gateway.orders.list_orders() == []
    entries = gateway.audit(quote_id=quote.id)
    assert entries[-1].data["error"] == "QUOTE_SIGNATURE_INVALID"
    assert audit.verify(gateway.db)["valid"]


def test_ac_3_rotation_retains_old_kid_seven_days(gateway, need, upstream):
    old = make_quote(gateway, need)
    script = BACKEND / "scripts/rotate_key.py"
    result = subprocess.run([sys.executable, str(script), "--keys-dir", str(gateway.signing.directory)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    new = make_quote(gateway, need)
    jwks = gateway.signing.jwks()
    assert len(jwks["keys"]) == 2 and old.signature.split(".")[0] != new.signature.split(".")[0]
    for quote in (old, new):
        independent_verify(quote.signature, quote_payload(quote), jwks)
    registry = json.loads((gateway.signing.directory / "keys.json").read_text())
    retired = next(k for k in registry["keys"] if k["kid"] != registry["active"])
    remaining = datetime.fromisoformat(retired["publish_until"]) - audit.utcnow()
    assert timedelta(days=6, hours=23) < remaining <= timedelta(days=7)
    retired["publish_until"] = (audit.utcnow() - timedelta(seconds=1)).isoformat()
    (gateway.signing.directory / "keys.json").write_text(json.dumps(registry))
    assert len(gateway.signing.jwks()["keys"]) == 1
    for pem in gateway.signing.directory.glob("*.pem"):
        assert pem.stat().st_mode & 0o077 == 0


def test_ac_4_contract_readme_and_mcp_document_signatures(gateway):
    schema = create_app(gateway).openapi()
    assert not schema["paths"]["/.well-known/jwks.json"]["get"].get("security")
    assert "signature" in schema["components"]["schemas"]["Quote"]["properties"]
    assert "receipt" in schema["components"]["schemas"]["Order"]["properties"]
    committed = yaml.safe_load((BACKEND.parent / "contracts/openapi.yaml").read_text())
    assert committed == schema
    readme = (BACKEND / "README.md").read_text()
    for text in ("RFC 8785", "Ed25519", "/.well-known/jwks.json", "verify_quote.py", "provenance", "7 days"):
        assert text in readme
    assert "verify_quote.py" in (BACKEND / "app/mcp_server.py").read_text()


def test_ac_5_receipts_bind_status_total_and_all_decisions(gateway, need, upstream):
    quote = make_quote(gateway, need)
    order = gateway.orders.place_order(quote.id, "receipt")
    assert order.status == "CONFIRMED"
    assert order.quote_signature_digest == signature_digest(quote.signature)
    assert order.customer_id == quote.customer_id and order.decided_by_kind == "agent"
    payload = receipt_payload(order)
    independent_verify(order.receipt, payload, gateway.signing.jwks())
    for field in payload:
        changed = {**payload, field: str(payload[field]) + "changed"}
        with pytest.raises(InvalidSignature):
            independent_verify(order.receipt, changed, gateway.signing.jwks())
    cancelled = gateway.orders.cancel(order.id, actor="agent:external")
    assert cancelled.status == "CANCELLED"
    independent_verify(cancelled.receipt, receipt_payload(cancelled), gateway.signing.jwks())
    pending_quote = make_quote(gateway, need, qty=200)
    pending = gateway.orders.place_order(pending_quote.id, "reject")
    assert pending.status == "PENDING_APPROVAL"
    rejected = gateway.orders.reject(pending.id, "Ada", actor="human:operator")
    assert rejected.status == "REJECTED" and rejected.decided_by_kind == "operator"
    independent_verify(rejected.receipt, receipt_payload(rejected), gateway.signing.jwks())
    another = make_quote(gateway, need, qty=200)
    approved = gateway.orders.place_order(another.id, "approve")
    approved = gateway.orders.approve(approved.id, "Ada", actor="human:operator")
    assert approved.status == "CONFIRMED" and approved.decided_by_kind == "operator"
    independent_verify(approved.receipt, receipt_payload(approved), gateway.signing.jwks())


def test_ac_6_offline_cli_reports_signature_expiry_customer(gateway, need, upstream, tmp_path):
    quote = make_quote(gateway, need)
    saved, keys = tmp_path / "quote.json", tmp_path / "jwks.json"
    saved.write_text(quote.model_dump_json())
    keys.write_text(json.dumps(gateway.signing.jwks()))
    command = [sys.executable, str(BACKEND / "scripts/verify_quote.py"), str(saved), "--jwks", str(keys)]
    def run(*args):
        return subprocess.run(command + list(args), cwd=tmp_path, capture_output=True, text=True)
    assert run("--customer", "1", "--at", quote.created_at.isoformat()).returncode == 0
    wrong = run("--customer", "other")
    assert wrong.returncode == 1 and "customer check failed" in wrong.stderr
    expired = run("--at", quote.expires_at.isoformat())
    assert expired.returncode == 1 and "expiry check failed" in expired.stderr
    data = json.loads(saved.read_text())
    data["total"] += 1
    saved.write_text(json.dumps(data))
    invalid = run()
    assert invalid.returncode == 1 and "signature check failed" in invalid.stderr


def test_ac_7_commitments_are_signed_and_exposed(gateway, need, upstream):
    quote = make_quote(gateway, need)
    order = gateway.orders.place_order(quote.id, "commitments")
    assert quote.price_commitment == quote_payload(quote)["price_commitment"] == "gateway-locked"
    assert order.commitment == receipt_payload(order)["commitment"] == "gateway-simulated"
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        assert client.get(f"/v1/quotes/{quote.id}").json()["price_commitment"] == "gateway-locked"
        assert client.get(f"/v1/orders/{order.id}").json()["commitment"] == "gateway-simulated"


def test_rfc8785_vectors_and_rejected_non_json_values():
    value = {"numbers": [333333333.33333329, 1e30, 4.50, 2e-3, 1e-27],
             "string": "€$\x0f\nA'B\"\\\"/", "literals": [None, True, False]}
    assert canonical_json(value) == ('{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
                                     '"string":"€$\\u000f\\nA\'B\\"\\\\\\"/"}').encode()
    assert canonical_json({"דּ": 1, "😀": 2, "€": 3, "\r": 4, "ö": 5, "1": 6, "\u0080": 7}) == '{"\\r":4,"1":6,"\u0080":7,"ö":5,"€":3,"😀":2,"דּ":1}'.encode()
    vectors = {"0000000000000000": "0", "8000000000000000": "0", "0000000000000001": "5e-324",
               "7fefffffffffffff": "1.7976931348623157e+308", "4340000000000000": "9007199254740992",
               "4430000000000000": "295147905179352830000", "44b52d02c7e14af6": "1e+23"}
    for bits, expected in vectors.items():
        assert canonical_json(struct.unpack('>d', bytes.fromhex(bits))[0]).decode() == expected
    assert canonical_json([1e-6, 1e-7, 1e20, 1e21]) == b'[0.000001,1e-7,100000000000000000000,1e+21]'
    for invalid in (float("nan"), float("inf"), "\ud800", {1: "key"}, 2**53):
        with pytest.raises((ValueError, UnicodeError)):
            canonical_json(invalid)
