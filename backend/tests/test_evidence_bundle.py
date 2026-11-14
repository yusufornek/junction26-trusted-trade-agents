"""Spec 29: portable evidence checked offline, with mocked optional RPC."""
import asyncio
import io
import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from zipfile import ZipFile

import httpx
import pytest
from fastapi.testclient import TestClient

from app import audit
from app.api import create_app
from app.auth import Principal
from app.evidence import build_bundle, zip_bundle
from app.mcp_server import create_server
from app.signing import SigningKeys, verify_quote
from scripts.verify_evidence import verify_bundle
from conftest import OPERATOR_HEADERS, agent_headers
from test_buyer_view import buyer_setup, place


@pytest.fixture
def purchase(gateway, need, upstream):
    return place(gateway, need)


def save(gateway, order, tmp_path):
    manifest, files = build_bundle(gateway, order.id, Principal("external"))
    path = tmp_path / "purchase.zip"
    path.write_bytes(zip_bundle(files))
    return path, manifest, files


def test_ac_1_confirmed_bundle_verifies_all_checks(gateway, purchase, tmp_path, buyer_setup):
    path, manifest, files = save(gateway, purchase, tmp_path)
    trusted = tmp_path / "trusted.json"
    trusted.write_text(json.dumps(gateway.signing.jwks()))
    result = subprocess.run([sys.executable, "scripts/verify_evidence.py", str(path), "--trusted-jwks", str(trusted)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    for check in ("signature_valid", "trusted_issuer_match", "audit_consistent", "ubl_schema_valid", "artifact_integrity", "binding_valid"):
        assert report[check] is True
    assert report["supplier_acceptance"] == "simulated" and report["network_checks"] == []
    assert {"quote.json", "receipt.json", "audit.json", "checkpoint.json", "order.xml", "order-response.xml", "jwks.json", "VERIFY.md", "manifest.json"} == set(files)
    assert manifest["order_id"] == purchase.id and manifest["customer_id"] == "1" and manifest["environment"] == "live"
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        response = client.get(f"/v1/orders/{purchase.id}/evidence")
        assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
        assert set(ZipFile(io.BytesIO(response.content)).namelist()) == set(files)
        assert client.get(f"/v1/orders/{purchase.id}/evidence?format=json").json()["order_id"] == purchase.id
        from hashlib import sha256
        for item in manifest["files"]:
            artifact = client.get(item["href"])
            assert artifact.status_code == 200 and sha256(artifact.content).hexdigest() == item["sha256"]
        assert client.get(f"/v1/orders/{purchase.id}/evidence", headers=agent_headers("observer")).status_code == 200
        assert client.get(f"/v1/orders/{purchase.id}/evidence", headers=OPERATOR_HEADERS).status_code == 200
    server = create_server(gateway, "external")
    response = asyncio.run(server.call_tool("get_order_evidence", {"order_id": purchase.id}))
    assert response.structured_content["order_id"] == purchase.id
    # Retired public keys remain independently obtainable beyond the current quote-key window.
    gateway.signing.rotate(now=audit.utcnow() - timedelta(days=9))
    assert len(gateway.signing.jwks()["keys"]) == 1
    with TestClient(create_app(gateway)) as client:
        history = client.get("/.well-known/jwks-history.json").json()
    assert len(history["keys"]) == 2
    assert verify_bundle(path, history)[1] == 0
    assert verify_bundle(save(gateway, purchase, tmp_path)[0], history)[1] == 0
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        # A later key rotation and checkpoint cannot change an earlier manifest's links.
        for item in manifest["files"]:
            assert sha256(client.get(item["href"]).content).hexdigest() == item["sha256"]


def test_ac_2_changed_quote_quantity_names_artifact_and_fails(gateway, purchase, tmp_path):
    path, _, files = save(gateway, purchase, tmp_path)
    quote = json.loads(files["quote.json"])
    quote["lines"][0]["units_needed"] += 1
    files["quote.json"] = json.dumps(quote).encode()
    path.write_bytes(zip_bundle(files))
    report, code = verify_bundle(path, gateway.signing.jwks())
    assert code != 0 and report["signature_valid"] is False
    assert any(e["artifact"] == "quote.json" and e["check"] == "signature_valid" for e in report["errors"])


def test_ac_3_changed_audit_entry_names_entry(gateway, purchase, tmp_path):
    path, _, files = save(gateway, purchase, tmp_path)
    links = json.loads(files["audit.json"])
    link = next(l for l in links if "entry" in l)
    link["entry"]["summary"] = "Changed audit evidence"
    files["audit.json"] = json.dumps(links).encode()
    path.write_bytes(zip_bundle(files))
    report, code = verify_bundle(path, gateway.signing.jwks())
    assert code != 0 and report["audit_consistent"] is False
    assert any(e["check"] == "audit_consistent" and f"entry {link['seq']}" in e["detail"] for e in report["errors"])


def test_ac_4_untrusted_jwks_fails_even_with_valid_bundled_signature(gateway, purchase, tmp_path):
    path, _, files = save(gateway, purchase, tmp_path)
    assert verify_quote(json.loads(files["quote.json"]), json.loads(files["jwks.json"])) is True
    report, code = verify_bundle(path, SigningKeys(tmp_path / "untrusted").jwks())
    assert report["signature_valid"] is False and report["trusted_issuer_match"] is False and code != 0


def test_ac_5_optional_rpc_matches_checkpoint_transaction(gateway, purchase, tmp_path, respx_mock):
    checkpoint = audit.head(gateway.db, gateway.signing)
    tx = "0x" + "a" * 64
    with gateway.db.connect(write=True) as conn:
        conn.execute("INSERT INTO audit_anchors(entries,head_hash,chain_id,tx_hash,status,at) VALUES(?,?,137,?,'sent',?)",
                     (checkpoint["entries"], checkpoint["head_hash"], tx, audit.utcnow().isoformat()))
    path, _, files = save(gateway, purchase, tmp_path)
    assert "anchor.json" in files
    report, code = verify_bundle(path, gateway.signing.jwks())
    assert code == 0 and report["anchor_status"] == "not_checked" and not report["network_checks"]
    anchor = json.loads(files["anchor.json"])
    rpc_url = "https://rpc.test"
    data = "0x" + f"VAIMO-AUDIT-1:{anchor['entries']}:{anchor['head_hash']}".encode().hex()
    def rpc(request):
        method = json.loads(request.content)["method"]
        result = {"eth_chainId": "0x89", "eth_getTransactionByHash": {"hash": tx, "input": data, "blockNumber": "0x10"},
                  "eth_getTransactionReceipt": {"transactionHash": tx, "blockNumber": "0x10", "status": "0x1"}}[method]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})
    route = respx_mock.post(rpc_url).mock(side_effect=rpc)
    report, code = verify_bundle(path, gateway.signing.jwks(), rpc_url=rpc_url)
    assert code == 0 and report["anchor_status"] == "matched" and route.call_count == 3
    assert report["network_checks"] == ["anchor_status"]
    respx_mock.get("https://trust.test/keys").respond(200, json=gateway.signing.jwks())
    report, code = verify_bundle(path, "https://trust.test/keys")
    assert code == 0 and report["network_checks"] == ["trusted_issuer_match"]
    def pending(request):
        body = json.loads(request.content)
        if body["method"] == "eth_getTransactionReceipt":
            return httpx.Response(200, json={"result": None})
        return rpc(request)
    respx_mock.post(rpc_url).mock(side_effect=pending)
    report, code = verify_bundle(path, gateway.signing.jwks(), rpc_url=rpc_url)
    assert code != 0 and report["anchor_status"] == "pending"
    respx_mock.post(rpc_url).respond(200, json={"result": "0x1"})
    assert verify_bundle(path, gateway.signing.jwks(), rpc_url=rpc_url)[1] != 0
    with gateway.db.connect(write=True) as conn:
        conn.execute("UPDATE audit_anchors SET head_hash=?", ("f" * 64,))
    path, _, files = save(gateway, purchase, tmp_path)
    assert "anchor.json" in files  # A conflicting historical checkpoint must never disappear.
    report, code = verify_bundle(path, gateway.signing.jwks())
    assert code != 0 and report["audit_consistent"] is False and report["anchor_status"] == "not_checked"


def test_ac_6_other_customer_data_is_hash_only(gateway, need, upstream, buyer_setup, tmp_path):
    other = place(gateway, need.model_copy(update={"customer_id": "2"}), agent="agent-b")
    own = place(gateway, need)
    path, _, files = save(gateway, own, tmp_path)
    with gateway.db.connect() as conn:
        private = {r[0] for r in conn.execute("SELECT id FROM audit WHERE order_id=? OR quote_id=?", (other.id, other.quote_id))}
    assert private
    links = json.loads(files["audit.json"])
    for link in links:
        if link["seq"] in private:
            assert set(link) == {"seq", "prev_hash", "hash"}
    for body in files.values():
        for secret in (other.id, other.quote_id, "agent-b"):
            assert secret.encode() not in body
    assert verify_bundle(path, gateway.signing.jwks())[1] == 0


def test_ac_7_another_agent_cannot_download_evidence(gateway, purchase, buyer_setup):
    with TestClient(create_app(gateway), headers=agent_headers("agent-b")) as client:
        for query in ("", "?format=json", "?artifact=quote.json"):
            response = client.get(f"/v1/orders/{purchase.id}/evidence" + query)
            assert response.status_code == 404 and response.json()["code"] == "ORDER_NOT_FOUND"
        assert client.get(f"/v1/orders/{purchase.id}/evidence", headers={"Authorization": ""}).status_code == 401
    server = create_server(gateway, "agent-b")
    response = asyncio.run(server.call_tool("get_order_evidence", {"order_id": purchase.id}))
    assert response.is_error and response.structured_content["code"] == "ORDER_NOT_FOUND"
