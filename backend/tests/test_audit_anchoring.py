"""Spec 21: JSON-RPC acceptance tests and optional local Anvil proof."""
import json
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone

import httpx
import pytest
from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from eth_utils import keccak
from fastapi.testclient import TestClient
from hexbytes import HexBytes

from app import audit
from app.anchoring import AnchorConfig, AnchorError, Anchoring, REQUIRED
from app.api import create_app
from conftest import agent_headers
from scripts import anchor_audit

TEST_KEY = "0x" + "19" * 32  # Synthetic test key; never fund this account.
RPC_URL = "https://anchor.test/rpc"


@pytest.fixture
def anchor(gateway):
    gateway.anchoring.config = AnchorConfig(RPC_URL, "80002", TEST_KEY, "https://amoy.polygonscan.com/tx/")
    return gateway.anchoring


class RPC:
    def __init__(self, gateway, anchor):
        self.gateway, self.anchor = gateway, anchor
        self.raw = self.transaction = self.tx_hash = None
        self.calls = []
        self.fail = self.mined = False

    def respond(self, request):
        body = json.loads(request.content)
        method = body["method"]
        self.calls.append(method)
        if self.fail:
            return httpx.Response(503, text=TEST_KEY)
        if method == "eth_chainId": result = "0x13882"
        elif method == "eth_getTransactionCount":
            assert body["params"] == [Account.from_key(TEST_KEY).address, "pending"]
            result = "0x7"
        elif method == "eth_maxPriorityFeePerGas": result = "0x3"
        elif method == "eth_feeHistory": result = {"baseFeePerGas": ["0xa", "0xb"]}
        elif method == "eth_estimateGas": result = "0x186a0"
        elif method == "eth_sendRawTransaction":
            self.raw = body["params"][0]
            self.transaction = TypedTransaction.from_bytes(HexBytes(self.raw)).as_dict()
            self.tx_hash = "0x" + keccak(HexBytes(self.raw)).hex()
            result = self.tx_hash
        elif method == "eth_getTransactionReceipt":
            assert self.anchor.list()[0]["status"] in ("sent", "confirmed")
            assert self.anchor.list()[0]["tx_hash"] == body["params"][0]
            result = {"transactionHash": self.tx_hash, "blockNumber": "0x2a", "status": "0x1"} if self.mined else None
        elif method == "eth_getTransactionByHash":
            result = {"hash": self.tx_hash, "blockNumber": "0x2a", "input": "0x" + bytes(self.transaction["data"]).hex(),
                      "from": Account.from_key(TEST_KEY).address, "to": Account.from_key(TEST_KEY).address, "value": "0x0"}
        elif method == "eth_getBlockByNumber": result = {"timestamp": "0x6553f100"}
        else: raise AssertionError(method)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


@pytest.fixture
def rpc(gateway, anchor, respx_mock):
    state = RPC(gateway, anchor)
    respx_mock.post(RPC_URL).mock(side_effect=state.respond)
    return state


def add_event(gateway, action="TEST"):
    with gateway.db.connect(write=True) as conn:
        audit.append(conn, "agent:external", action, "Synthetic audit evidence")


def test_ac_1_cli_signs_type2_self_transaction_and_records_receipt(gateway, anchor, rpc, monkeypatch, capsys):
    add_event(gateway)
    checkpoint = audit.verify(gateway.db)
    monkeypatch.setattr(anchor_audit, "load_env", lambda: None)
    monkeypatch.setattr(anchor_audit.AnchorConfig, "from_env", lambda: anchor.config)
    assert anchor_audit.main(["--db", str(gateway.db.path)]) == 0
    assert "sent: https://amoy.polygonscan.com/tx/0x" in capsys.readouterr().out
    tx = rpc.transaction
    assert tx["type"] == 2 and tx["chainId"] == 80002 and tx["nonce"] == 7
    assert tx["value"] == 0 and tx["gas"] == 100000
    assert tx["maxFeePerGas"] == 25 and tx["maxPriorityFeePerGas"] == 3
    assert bytes(tx["to"]).hex().lower() == Account.from_key(TEST_KEY).address[2:].lower()
    assert Account.recover_transaction(rpc.raw) == Account.from_key(TEST_KEY).address
    assert bytes(tx["data"]).decode() == f"VAIMO-AUDIT-1:{checkpoint['entries']}:{checkpoint['head_hash']}"
    assert anchor.list()[0]["status"] == "sent"
    rpc.mined = True
    assert anchor_audit.main(["--db", str(gateway.db.path)]) == 0
    row, = anchor.list()
    assert row["status"] == "confirmed" and row["block_number"] == 42
    assert rpc.calls.count("eth_sendRawTransaction") == 1
    assert [e.action for e in gateway.audit()].count("AUDIT_ANCHORED") == 1
    assert audit.verify(gateway.db)["valid"]


def test_ac_2_external_prefix_checkpoint_detects_edited_history(gateway, anchor, rpc):
    add_event(gateway)
    rpc.mined = True
    anchor.anchor_once()
    add_event(gateway, "AFTER_ANCHOR")
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        result = client.get("/v1/audit/verify", params={"anchor": "latest"})
        assert result.status_code == 200, result.text
        assert result.json()["anchor_match"] is True
        assert result.json()["anchor_block_time"] == datetime.fromtimestamp(1700000000, timezone.utc).isoformat().replace("+00:00", "Z")
        tx_hash = anchor.list()[0]["tx_hash"]
        assert client.get("/v1/audit/verify", params={"anchor": tx_hash}).json()["anchor_match"] is True
        with gateway.db.connect(write=True) as conn:
            conn.execute("DROP TRIGGER audit_no_update")
            conn.execute("UPDATE audit SET summary='modified' WHERE id=1")
        result = client.get("/v1/audit/verify", params={"anchor": "latest"}).json()
        assert result["anchor_match"] is False and result["valid"] is False
        with gateway.db.connect(write=True) as conn:
            previous = audit.GENESIS
            for row in conn.execute("SELECT * FROM audit ORDER BY id").fetchall():
                digest = audit.entry_hash(previous, audit.entry_payload(row))
                conn.execute("UPDATE audit SET prev_hash=?,hash=? WHERE id=?", (previous, digest, row["id"]))
                previous = digest
        result = client.get("/v1/audit/verify", params={"anchor": "latest"}).json()
        assert result["valid"] is True and result["anchor_match"] is False
        rpc.transaction["data"] = b"WRONG:1:" + b"0" * 64
        assert client.get("/v1/audit/verify", params={"anchor": "latest"}).json()["anchor_match"] is False


def test_ac_3_unconfigured_is_off_cli_names_missing_variables(gateway, monkeypatch, capsys, respx_mock):
    for name in (*REQUIRED, "ANCHOR_EVERY_N_ENTRIES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(anchor_audit, "load_env", lambda: None)
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        assert client.get("/v1/audit/anchors").json() == []
        assert client.get("/v1/audit/verify", params={"anchor": "latest"}).json()["anchor_match"] is False
        assert "anchoring" not in client.get("/health").json()
    assert anchor_audit.main(["--db", str(gateway.db.path)]) == 1
    error = capsys.readouterr().err
    assert all(name in error for name in REQUIRED)
    assert not respx_mock.calls
    assert not Anchoring(gateway.db, AnchorConfig(private_key=TEST_KEY)).config.enabled


def test_ac_4_automatic_failure_isolated_and_next_trigger_retries(gateway, anchor, rpc, need, upstream):
    anchor.config.every_n_entries = "1"
    rpc.fail = True
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        result = client.post("/v1/offers", json=need.model_dump(mode="json"))
        assert result.status_code == 200, result.text
        anchor._worker.join(timeout=5)
        assert anchor.list()[0]["status"] == "failed"
        rpc.fail, rpc.mined = False, True
        result = client.post("/v1/offers", json=need.model_dump(mode="json"))
        assert result.status_code == 200, result.text
        anchor._worker.join(timeout=5)
        assert [row["status"] for row in anchor.list()] == ["confirmed", "failed"]
        calls = len(rpc.calls)
        anchor.trigger()
        assert len(rpc.calls) == calls
    assert audit.verify(gateway.db)["valid"]


def test_ac_5_key_never_appears_in_responses_logs_audit_or_errors(gateway, anchor, rpc, caplog, capsys, monkeypatch):
    add_event(gateway)
    rpc.mined = True
    anchor.anchor_once()
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        responses = [client.get(path).text for path in ("/health", "/v1/audit/anchors", "/v1/audit/verify?anchor=latest")]
        assert client.get("/health").json()["anchoring"]["address"] == Account.from_key(TEST_KEY).address
    rpc.fail = True
    with pytest.raises(AnchorError) as error:
        anchor.anchor_once()
    monkeypatch.setattr(anchor_audit, "load_env", lambda: None)
    monkeypatch.setattr(anchor_audit.AnchorConfig, "from_env", lambda: anchor.config)
    assert anchor_audit.main(["--db", str(gateway.db.path)]) == 1
    captured = capsys.readouterr()
    with gateway.db.connect() as conn:
        rows = json.dumps([dict(row) for row in conn.execute("SELECT * FROM audit")])
        anchors = json.dumps([dict(row) for row in conn.execute("SELECT * FROM audit_anchors")])
    values = responses + [caplog.text, rows, anchors, str(error.value), repr(anchor.config), captured.out, captured.err]
    assert all(TEST_KEY not in value and TEST_KEY[2:] not in value for value in values)


@pytest.mark.skipif(shutil.which("anvil") is None, reason="anvil is not installed on PATH")
def test_ac_6_local_anvil_real_transaction_and_verification(gateway, respx_mock):
    dev_key = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", 0))
        except PermissionError:
            pytest.skip("Loopback sockets are unavailable in this sandbox; Anvil integration requires a listener")
        port = sock.getsockname()[1]
    process = subprocess.Popen(["anvil", "--host", "127.0.0.1", "--port", str(port), "--chain-id", "31337", "--silent"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    respx_mock.route(url=url).pass_through()
    local = Anchoring(gateway.db, AnchorConfig(url, "31337", dev_key, url + "/tx/"))
    try:
        for _ in range(100):
            try:
                with httpx.Client(trust_env=False) as client:
                    if client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}).status_code == 200: break
            except httpx.TransportError: time.sleep(0.05)
        else: pytest.fail("Anvil did not start")
        add_event(gateway)
        row = local.anchor_once()
        for _ in range(20):
            if row["status"] == "confirmed": break
            time.sleep(0.05); row = local.anchor_once()
        assert row["status"] == "confirmed"
        assert local.verify("latest")["anchor_match"] is True
    finally:
        process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired: process.kill(); process.wait()
