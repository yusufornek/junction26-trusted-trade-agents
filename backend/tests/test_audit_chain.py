"""Spec 11: cross-process chain serialization and external checkpoints."""
import sqlite3
import subprocess
import sys
import time
import hashlib
import tracemalloc

from fastapi.testclient import TestClient
import pytest

from app import audit
from app.api import create_app
from app.config import BACKEND
from app.db import Database
from app.canonical import canonical_json
from conftest import OPERATOR_HEADERS, agent_headers
from test_signed_quotes import independent_verify, make_quote


def add(db, count=3):
    for i in range(count):
        with db.connect(write=True) as conn:
            audit.append(conn, "system", "TEST", f"entry {i}", data={"i": i, "nested": [True, None, 0.5]})


@pytest.mark.parametrize("tamper", ["edit", "delete", "delete_tail"])
def test_ac_1_fresh_chain_detects_edit_and_deletion(gateway, need, upstream, tamper):
    assert audit.verify(gateway.db) == {"valid": True, "entries": 0, "head_hash": audit.GENESIS, "first_break": None}
    quote = make_quote(gateway, need)
    order = gateway.orders.place_order(quote.id, "hash-timeline")
    entries = gateway.audit()
    assert audit.verify(gateway.db)["valid"]
    assert all(len(e.hash) == len(e.prev_hash) == 64 for e in entries)
    assert [step.hash for step in gateway.orders.timeline(order.id).steps] == [e.hash for e in entries]
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        assert all(e["hash"] for e in client.get("/v1/audit").json())
    with gateway.db.connect(write=True) as conn:
        if tamper == "edit":
            conn.execute("DROP TRIGGER audit_no_update")
            conn.execute("UPDATE audit SET summary='changed' WHERE id=2")
        else:
            conn.execute("DROP TRIGGER audit_no_delete")
            conn.execute("DELETE FROM audit WHERE id=?", (len(entries) if tamper == "delete_tail" else 2,))
    result = audit.verify(gateway.db)
    assert not result["valid"]
    assert result["first_break"] == (len(entries) if tamper == "delete_tail" else 2)


def test_ac_2_rest_and_mcp_processes_share_one_valid_chain(gateway, tmp_path):
    # Two independent gateway instances, SQLite connections and signing-key loaders.
    worker = '''
import asyncio, sys, time
from pathlib import Path
from fastapi.testclient import TestClient
from app.api import create_app
from app.auth import Credentials
from app.config import Settings
from app.mcp_server import create_server
from app.service import Gateway
from mcp.server.mcpserver.exceptions import ToolError
mode, database, policy, barrier = sys.argv[1:]
g = Gateway(Settings(db_path=Path(database), policy_path=Path(policy), keys=Credentials('operator', 'external:secret')))
Path(barrier + '.' + mode).touch()
while not Path(barrier).exists():
    time.sleep(.01)
try:
    if mode == 'rest':
        with TestClient(create_app(g), headers={'Authorization':'Bearer secret'}) as client:
            for i in range(20):
                r = client.post('/v1/orders', json={'quote_id':'missing'}, headers={'Idempotency-Key':str(i)})
                assert r.status_code == 404, r.text
    else:
        async def run():
            server = create_server(g, 'external')
            for i in range(20):
                result = await server.call_tool('place_order', {'quote_id':'missing','idempotency_key':str(i)})
                assert result.is_error and result.structured_content['code'] == 'QUOTE_NOT_FOUND'
        asyncio.run(run())
finally:
    g.close()
'''
    barrier = tmp_path / "start"
    processes = [subprocess.Popen([sys.executable, "-c", worker, mode, str(gateway.db.path),
                                  str(gateway.settings.policy_path), str(barrier)], cwd=BACKEND,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for mode in ("rest", "mcp")]
    try:
        deadline = time.monotonic() + 10
        while not all((tmp_path / f"start.{mode}").exists() for mode in ("rest", "mcp")):
            assert time.monotonic() < deadline, "Workers did not initialize"
            assert all(p.poll() is None for p in processes), "Worker exited before barrier"
            time.sleep(.01)
        barrier.touch()
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stdout + stderr
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()
    result = audit.verify(gateway.db)
    entries = gateway.audit()
    alerts = [e for e in entries if e.action == "GOVERNANCE_ALERT"]
    assert result["valid"] and result["entries"] == 40 + len(alerts)
    assert len([e for e in entries if e.action == "PLACE_ORDER_FAILED"]) == 40
    assert all(e.action in {"PLACE_ORDER_FAILED", "GOVERNANCE_ALERT"} for e in entries)
    assert all(e.data["type"] == "refusal.burst" for e in alerts)


def test_ac_3_earlier_checkpoint_survives_appends(gateway):
    add(gateway.db)
    head = audit.head(gateway.db, gateway.signing)
    add(gateway.db, 4)
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        response = client.get("/v1/audit/verify", params={"checkpoint_entries": head["entries"], "checkpoint_hash": head["head_hash"]})
        assert response.status_code == 200
        assert response.json()["checkpoint_match"] is True
        assert response.json()["entries"] == 7 and response.json()["valid"]
        assert client.get("/v1/audit/verify", params={"checkpoint_entries": 0, "checkpoint_hash": audit.GENESIS}).json()["checkpoint_match"]
        assert client.get("/v1/audit/verify", params={"checkpoint_entries": 50, "checkpoint_hash": head["head_hash"]}).json()["checkpoint_match"] is False
        assert client.get("/v1/audit/verify", params={"checkpoint_entries": 1}).status_code == 422


def test_ac_4_head_signature_and_authenticated_access(gateway):
    add(gateway.db)
    with TestClient(create_app(gateway)) as client:
        jwks = client.get("/.well-known/jwks.json").json()
        for route in ("/v1/audit/head", "/v1/audit/verify"):
            assert client.get(route).status_code == 401
            for headers in (agent_headers("external"), OPERATOR_HEADERS):
                response = client.get(route, headers=headers)
                assert response.status_code == 200
                assert set(response.json()) <= {"entries", "head_hash", "at", "jws", "valid", "first_break", "checkpoint_match"}
        head = client.get("/v1/audit/head", headers=agent_headers("external")).json()
        assert head["entries"] == 3
        independent_verify(head["jws"], {k: head[k] for k in ("entries", "head_hash", "at")}, jwks)
        assert client.get("/v1/audit", headers=agent_headers("external")).status_code == 403


def test_ac_5_rewritten_chain_cannot_match_external_checkpoint(gateway):
    add(gateway.db)
    checkpoint = audit.head(gateway.db, gateway.signing)
    with gateway.db.connect(write=True) as conn:
        conn.execute("DROP TRIGGER audit_no_update")
        conn.execute("UPDATE audit SET summary='rewritten' WHERE id=1")
        prev = audit.GENESIS
        for row in conn.execute("SELECT * FROM audit ORDER BY id").fetchall():
            digest = audit.entry_hash(prev, audit.entry_payload(row))
            conn.execute("UPDATE audit SET prev_hash=?,hash=? WHERE id=?", (prev, digest, row["id"]))
            prev = digest
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        result = client.get("/v1/audit/verify", params={"checkpoint_entries": checkpoint["entries"],
                                                     "checkpoint_hash": checkpoint["head_hash"]}).json()
    assert result["valid"] and result["checkpoint_match"] is False


def test_existing_rows_migrated_once_with_start_event(tmp_path):
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as conn:
        conn.execute("""CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
            actor TEXT NOT NULL, action TEXT NOT NULL, order_id TEXT, quote_id TEXT, summary TEXT NOT NULL, data TEXT NOT NULL)""")
        conn.execute("INSERT INTO audit(ts,actor,action,summary,data) VALUES(?,?,?,?,?)",
                     ("2026-11-13T10:00:00+00:00", "system", "LEGACY", "unchanged", '{"old": true}'))
    db = Database(database)
    entries = audit.read(db)
    assert entries[0].summary == "unchanged" and entries[0].data == {"old": True}
    assert entries[1].action == "AUDIT_CHAIN_START" and entries[1].data["migrated_entries"] == 1
    assert audit.verify(db)["valid"]
    assert len(audit.read(Database(database))) == 2
    assert entries[0].model_dump(mode="json")["ts"] == "2026-11-13T10:00:00+00:00"
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with db.connect(write=True) as conn:
            conn.execute("UPDATE audit SET summary='forbidden' WHERE id=1")


def test_review_rest_audit_hash_reproduced_from_export(gateway):
    add(gateway.db)
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        rows = client.get("/v1/audit").json()
    previous = audit.GENESIS
    for row in rows:
        payload = {"seq": row["id"], **{k: row[k] for k in (
            "ts", "actor", "action", "summary", "order_id", "quote_id", "data")}}
        digest = hashlib.sha256(previous.encode("ascii") + canonical_json(payload)).hexdigest()
        assert digest == row["hash"] and row["prev_hash"] == previous
        previous = digest


def test_review_audit_verify_bounded_memory(gateway):
    for i in range(80):
        with gateway.db.connect(write=True) as conn:
            audit.append(conn, "system", "TEST", str(i), data={"blob": "x" * 65536})
    checkpoint = audit.head(gateway.db, gateway.signing)
    tracemalloc.start()
    try:
        result = audit.verify(gateway.db, checkpoint["entries"], checkpoint["head_hash"])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result["valid"] and result["checkpoint_match"] and result["entries"] == 80
    assert peak < 1_500_000, peak
