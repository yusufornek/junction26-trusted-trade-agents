import shutil
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import audit
from app.api import create_app
from conftest import OPERATOR_HEADERS
from app.models import Order, PolicyUpdateRequest
from conftest import quote_request
from scripts import reset_demo as reset_script


@pytest.fixture
def reset_paths(gateway, tmp_path):
    # Reuse the tiny catalog sources from conftest, never backend/var/.
    data = tmp_path / "data"
    data.mkdir()
    for old, new in (("products.json.gz", "product_data.json.gz"),
                     ("reliability.csv", "delivery_reliability.csv"),
                     ("carbon.csv", "carbon_footprint.csv")):
        shutil.copyfile(tmp_path / old, data / new)
    return {
        "db_path": gateway.db.path,
        "runtime_policy_path": gateway.policy.runtime_path,
        "archive_root": tmp_path / "archive",
        "source": data / "product_data.json.gz",
        "reliability_path": data / "delivery_reliability.csv",
        "carbon_path": data / "carbon_footprint.csv",
    }


@pytest.fixture
def demo_state(gateway, need, upstream, reset_paths):
    defaults = gateway.policy.get().model_dump(mode="json")
    upstream.model, upstream.amount = "Stock", 10000
    lookup = gateway.find_offers(need, actor="agent:hotel-chain-agent")
    quote = gateway.orders.create_quote(quote_request(need, need_ref=lookup.lookup_id), "hotel-chain-agent")
    order = gateway.orders.place_order(quote.id, "demo-reset", "hotel-chain-agent")
    assert order.status == "PENDING_APPROVAL"
    changed = gateway.policy.get().model_dump(mode="json")
    changed["approval"]["max_auto_total_eur"] = 10
    gateway.policy.update(PolicyUpdateRequest(policy=changed, updated_by="Ada"))
    return defaults, order


def snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_ac_1_reset_clears_transaction_state_and_restores_default_policy(gateway, reset_paths, demo_state):
    defaults, _ = demo_state
    result = reset_script.reset_demo(**reset_paths, port_check=lambda port: False)
    assert result.products == 7
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM products").fetchone()[0] == result.products
        assert conn.execute("SELECT count(*) FROM products_fts").fetchone()[0] == result.products
        for table in ("orders", "quotes", "lookups", "reservations", "audit"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert not reset_paths["runtime_policy_path"].exists()
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        assert client.get("/v1/policy").json() == defaults
        assert client.get("/v1/approvals").json() == []


def test_ac_2_archive_preserves_database_sidecars_and_changed_policy(gateway, reset_paths, demo_state):
    _, order = demo_state
    runtime = reset_paths["runtime_policy_path"].read_bytes()
    # Keep a real WAL open so all three SQLite files must be moved, including uncheckpointed data.
    keeper = sqlite3.connect(gateway.db.path)
    try:
        audit.append(keeper, "system", "RESET_FIXTURE", "Uncheckpointed archive evidence.")
        keeper.commit()
        assert all(gateway.db.path.with_name(gateway.db.path.name + suffix).exists()
                   for suffix in ("", "-wal", "-shm"))
        result = reset_script.reset_demo(**reset_paths, port_check=lambda port: False)
        assert {path.name for path in result.archive_dir.iterdir()} == {
            "app.db", "app.db-wal", "app.db-shm", "policy.yaml"}
        assert (result.archive_dir / "policy.yaml").read_bytes() == runtime
        with sqlite3.connect(result.archive_dir / "app.db") as archived:
            saved = archived.execute("SELECT data FROM orders WHERE id=?", (order.id,)).fetchone()[0]
            assert Order.model_validate_json(saved) == order
            assert archived.execute("SELECT count(*) FROM audit WHERE action='RESET_FIXTURE'").fetchone()[0] == 1
    finally:
        keeper.close()


def test_ac_3_repeated_resets_keep_distinct_unchanged_archives(reset_paths, demo_state, monkeypatch):
    # Freeze time to exercise collisions without sleeping or overwriting an earlier archive.
    now = reset_script.datetime.now(reset_script.timezone.utc)

    class FrozenDatetime:
        @staticmethod
        def now(tz):
            return now

    monkeypatch.setattr(reset_script, "datetime", FrozenDatetime)
    first = reset_script.reset_demo(**reset_paths, port_check=lambda port: False)
    before = snapshot(first.archive_dir)
    second = reset_script.reset_demo(**reset_paths, port_check=lambda port: False)
    assert first.archive_dir != second.archive_dir
    assert len(list(reset_paths["archive_root"].iterdir())) == 2
    assert snapshot(first.archive_dir) == before
    assert second.products == first.products == 7
    assert (second.archive_dir / "app.db").exists()


def test_ac_4_first_run_builds_catalog_and_an_empty_archive(reset_paths, tmp_path):
    fresh = {**reset_paths, "db_path": tmp_path / "first/app.db",
             "runtime_policy_path": tmp_path / "first/policy.yaml", "archive_root": tmp_path / "first/archive"}
    result = reset_script.reset_demo(**fresh, port_check=lambda port: False)
    assert result.archive_dir.is_dir() and list(result.archive_dir.iterdir()) == []
    assert result.products == 7
    with sqlite3.connect(fresh["db_path"]) as conn:
        assert conn.execute("SELECT count(*) FROM products").fetchone()[0] == 7
        assert conn.execute("SELECT count(*) FROM audit").fetchone()[0] == 0


def test_ac_5_cli_refuses_running_api_without_changes_and_force_proceeds(
        gateway, reset_paths, demo_state, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(reset_script.Settings, "from_env", lambda: gateway.settings)
    monkeypatch.setattr(reset_script, "ROOT", tmp_path)
    checked = []

    def answering(port):
        checked.append(port)
        return True

    monkeypatch.setattr(reset_script, "api_running", answering)
    before = snapshot(tmp_path)
    assert reset_script.main([]) == 1
    assert checked == [8000]
    assert "Stop the API first" in capsys.readouterr().err
    assert snapshot(tmp_path) == before
    assert reset_script.main(["--port", "8123"]) == 1
    assert snapshot(tmp_path) == before
    capsys.readouterr()
    assert reset_script.main(["--port", "8123", "--force"]) == 0
    assert checked == [8000, 8123, 8123]
    output = capsys.readouterr().out
    archive, = reset_paths["archive_root"].iterdir()
    assert str(archive) in output and "Catalog products: 7" in output
    assert ".venv/bin/python -m uvicorn app.api:app --port 8123" in output
    assert gateway.orders.list_orders() == []
