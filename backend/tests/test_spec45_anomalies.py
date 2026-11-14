"""Spec 45 anomaly AC-5..AC-10: pure tables and real offline quote/order paths."""
import asyncio
import json
from datetime import timedelta, timezone, datetime

import pytest
from fastapi.testclient import TestClient

from app import audit
from app.anomaly import Point, expected_band, price_check, quantity_check, velocity_check
from app.api import create_app
from app.auth import Principal
from app.db import DomainError
from app.mcp_server import create_server
from app.models import PolicyUpdateRequest, Quantity
from app.signing import verify_quote
from conftest import agent_headers, demo_catalog, quote_request
from scripts.reset_demo import seed_anomaly_history


def seed(gateway, *, quantity=200, price=13, product="1", agent="external", customer="1", count=8, unit="L"):
    now = audit.utcnow()
    with gateway.db.connect(write=True) as conn:
        for i in range(count):
            at = now - timedelta(days=(i + 1) * 7)
            data = {"order_id": f"seed:{agent}:{customer}:{product}:{i}", "customer_id": customer,
                    "agent_id": agent, "product_id": product, "category": "10400", "supplier_id": "valio",
                    "date": at.date().isoformat(), "timestamp": at.isoformat(), "quantity": quantity * 5,
                    "unit": unit, "total_paid": quantity * price, "sales_units": quantity, "unit_price": price,
                    "synthetic": True}
            conn.execute("INSERT INTO purchase_memory VALUES(?,?,?)", (data["order_id"], customer, json.dumps(data)))


def check(resource, code, product=None):
    return next(c for c in resource.anomalies if c.code == code and (product is None or c.product_id == product))


def quote(gateway, need, qty=200, agent="external", product="1", lookup=None):
    lookup = lookup or gateway.find_offers(need, actor=f"agent:{agent}")
    return gateway.orders.create_quote(quote_request(need, qty=qty, need_ref=lookup.lookup_id, product_id=product), agent)


def test_ac_5_anomaly_quote_preserves_task_links_and_signed_evidence(gateway, need, upstream):
    from app.foundation import TASK

    seed(gateway, quantity=10)
    need.quantity = Quantity(value=500, unit="L")
    token = TASK.set("anomaly-merge-task")
    try:
        original = quote(gateway, need, qty=100)
        assert original.links["task_id"] == "anomaly-merge-task"
        assert verify_quote(original.model_dump(mode="json"), gateway.signing.jwks())
        assert check(original, "QUANTITY_ANOMALY").status == "anomaly"
        order = gateway.orders.place_order(original.id, "anomaly-merge-task")
        assert order.total == 1300 and order.status == "PENDING_APPROVAL"
        assert "QUANTITY_ANOMALY" in {hit.rule for hit in order.policy.hits}
        with gateway.db.connect() as conn:
            events = conn.execute("SELECT type,payload FROM events WHERE aggregate_id=?", (original.id,)).fetchall()
        assert {row["type"] for row in events} == {"quote.created", "quote.anomaly_detected", "quote.accepted"}
        assert all(json.loads(row["payload"])["links"]["task_id"] == "anomaly-merge-task" for row in events)
    finally:
        TASK.reset(token)


def test_ac_9_forecast_environment_preserves_frontend_oauth_handoff(tmp_path, monkeypatch):
    import os
    from app.config import load_env

    values = {"FORECAST_ENABLED": "true", "FORECAST_MODEL_PATH": str(tmp_path / "weights"),
              "FRONTEND_CONSENT_URL": "https://console.example/consent"}
    monkeypatch.setattr(os, "environ", os.environ.copy())
    for name in values:
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / "merge.env"
    env.write_text("\n".join(f'{key}="{value}"' for key, value in values.items()))
    load_env(env)
    assert {key: os.environ.get(key) for key in values} == values
    monkeypatch.setenv("FRONTEND_CONSENT_URL", "https://process.example/consent")
    load_env(env)
    assert os.environ["FRONTEND_CONSENT_URL"] == "https://process.example/consent"


@pytest.mark.parametrize("values,observed,status,band", [
    ([200] * 3, 2000, "insufficient_history", None),
    ([200] * 4, 2000, "anomaly", (200, 200)),
    ([190, 200, 210, 200], 210, "normal", (185, 215)),
    ([190, 200, 210, 200], 180, "anomaly", (185, 215)),
    ([190, 200, 210, 200], 220, "anomaly", (185, 215)),
])
def test_ac_5_robust_quantity_table(values, observed, status, band):
    at = audit.utcnow()
    points = [Point(at - timedelta(days=i + 1), x) for i, x in enumerate(values)]
    result = quantity_check(points, observed, at)
    assert result["status"] == status
    if band:
        assert (result["expected_band"]["lower"], result["expected_band"]["upper"]) == band
    else:
        assert result["expected_band"] is None
    # Four same-weekday observations win over other-day outliers.
    weekly = [Point(at - timedelta(weeks=i + 1), 200) for i in range(4)]
    assert expected_band(weekly + [Point(at - timedelta(days=1), 9000)], at)["expected_band"]["upper"] == 200
    variable = [Point(at - timedelta(weeks=i + 1), x) for i, x in enumerate([190, 200, 210, 200])]
    assert quantity_check(variable, 212, at, k=2)["status"] == "anomaly"
    assert quantity_check(variable, 212, at, k=5)["status"] == "normal"


def test_ac_5_tenfold_quantity_fix_requotes_pending_order_and_split_holds_remainder(gateway, need, upstream):
    seed(gateway)
    need.quantity = Quantity(value=10000, unit="L")
    original = quote(gateway, need, 2000)
    anomaly = check(original, "QUANTITY_ANOMALY", "1")
    assert anomaly.status == "anomaly" and anomaly.expected_band == {"lower": 200, "upper": 200, "median": 200}
    assert anomaly.observed == 2000 and anomaly.history_window["points"] == 8
    fix = next(f for f in anomaly.fixes if f.kind == "correct_quantity")
    assert fix.quantity_sales_units == 200
    order = gateway.orders.place_order(original.id, "tenfold")
    assert order.status == "PENDING_APPROVAL" and order.next_action == "WAIT_FOR_APPROVAL"
    assert "QUANTITY_ANOMALY" in {h.rule for h in order.policy.hits}
    # No agent of another customer may apply a proposed fix.
    with TestClient(create_app(gateway), headers=agent_headers("agent-b")) as client:
        assert client.post(f"/v1/quotes/{original.id}/anomaly-fixes/{fix.id}").status_code == 404
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        response = client.post(f"/v1/quotes/{original.id}/anomaly-fixes/{fix.id}")
        assert response.status_code == 200, response.text
        corrected = response.json()["quotes"][0]
        assert corrected["lines"][0]["units_needed"] == 200
        assert verify_quote(corrected, gateway.signing.jwks())
        assert gateway.orders.get_order(order.id).status == "CANCELLED"
        assert client.post(f"/v1/quotes/{original.id}/anomaly-fixes/{fix.id}").status_code == 409
    second = quote(gateway, need, 2000)
    from app.foundation import PRINCIPAL
    from dataclasses import replace

    server = create_server(gateway, "external")
    scoped = gateway.accounts.bind(Principal("external", customer_ids=("1",),
        connection_id="scoped-anomaly-test", scopes=("read",)))
    scope_token = PRINCIPAL.set(scoped)
    try:
        denied = asyncio.run(server.call_tool("apply_anomaly_fix",
            {"quote_id": second.id, "fix_id": "1:split"}))
        assert denied.is_error and denied.structured_content["code"] == "INSUFFICIENT_SCOPE"
    finally:
        PRINCIPAL.reset(scope_token)
    scope_token = PRINCIPAL.set(replace(scoped, scopes=("quotes:write",)))
    try:
        result = asyncio.run(server.call_tool("apply_anomaly_fix",
            {"quote_id": second.id, "fix_id": "1:split"}))
    finally:
        PRINCIPAL.reset(scope_token)
    assert not result.is_error, result.structured_content
    split = result.structured_content["quotes"]
    assert [q["lines"][0]["units_needed"] for q in split] == [200, 1800]
    held = gateway.orders.place_order(split[1]["id"], "remainder")
    assert held.status == "PENDING_APPROVAL" and "QUANTITY_ANOMALY" in {w.code for w in held.warnings}
    assert sum(c.code == "QUANTITY_ANOMALY" and c.product_id == "1" for c in held.anomalies) == 1
    policy = gateway.policy._editable(gateway.policy.get())
    policy["approval"]["max_auto_total_eur"] = 10000
    policy["agents"]["external"]["max_auto_daily_total_eur"] = 100000
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Split isolation"))
    usual_remainder = quote(gateway, need, 400)
    pair = gateway.anomalies.apply_fix(usual_remainder.id, "1:split").quotes
    assert [q.lines[0].units_needed for q in pair] == [200, 200]
    assert gateway.orders.place_order(pair[0].id, "usual-part").status == "CONFIRMED"
    remainder = gateway.orders.place_order(pair[1].id, "usual-remainder")
    assert remainder.status == "PENDING_APPROVAL"
    assert check(remainder, "QUANTITY_ANOMALY").explanation_text
    assert sum(c.code == "QUANTITY_ANOMALY" for c in remainder.anomalies) == 1
    assert any(e.action == "ANOMALY_FIX_APPLIED" for e in gateway.audit())


@pytest.mark.parametrize("increase,expected", [(19.99, "normal"), (20, "anomaly"), (25, "anomaly")])
def test_ac_6_price_threshold_table(increase, expected):
    now = audit.utcnow()
    result = price_check([Point(now - timedelta(days=i + 1), 100) for i in range(4)], 100 + increase, now)
    assert result["status"] == expected
    assert result["expected_band"]["upper"] == 120


def test_ac_6_price_25_percent_above_last_paid_has_ranked_cheaper_fix(gateway, need, upstream):
    seed(gateway)
    upstream.prices.update({"1": 16.25, "4": 10})
    upstream.sizes.update({"1": 5, "4": 10})
    lookup = gateway.find_offers(need)
    original = quote(gateway, need, lookup=lookup)
    anomaly = check(original, "PRICE_ANOMALY", "1")
    assert anomaly.status == "anomaly" and anomaly.observed == 16.25
    assert anomaly.expected_band["last_paid"] == 13 and anomaly.expected_band["upper"] == 15.6
    fix = next(f for f in anomaly.fixes if f.kind == "cheaper_alternative")
    assert fix.product_id in {o.product_id for o in lookup.offers}
    changed = gateway.anomalies.apply_fix(original.id, fix.id).quotes[0]
    assert changed.lines[0].product_id == fix.product_id and changed.total < original.total
    assert len(upstream.price_calls) >= 3  # Replacement is freshly priced.
    ordered = gateway.orders.place_order(quote(gateway, need, lookup=lookup).id, "price-anomaly")
    assert ordered.status == "PENDING_APPROVAL" and check(ordered, "PRICE_ANOMALY").status == "anomaly"
    stale = quote(gateway, need, lookup=lookup)
    stale_fix = next(f for f in check(stale, "PRICE_ANOMALY").fixes if f.kind == "cheaper_alternative")
    upstream.prices[stale_fix.product_id] = 10000
    with pytest.raises(DomainError) as error:
        gateway.anomalies.apply_fix(stale.id, stale_fix.id)
    assert error.value.code == "FIX_NOT_FOUND"
    assert gateway.orders.get_quote(stale.id).status == "OPEN"


@pytest.mark.parametrize("observed,status,pause", [(5, "normal", False), (6, "anomaly", False), (10, "anomaly", True)])
def test_ac_7_velocity_table(observed, status, pause):
    now = audit.utcnow()
    times = [now - timedelta(days=i + 1) for i in range(4)]
    result = velocity_check(times, observed, now)
    assert result["status"] == status and result["pause"] == pause


def test_ac_7_burst_rechecks_at_placement_pauses_alerts_and_buyer_resumes(gateway, need, upstream):
    seed(gateway)
    policy = gateway.policy._editable(gateway.policy.get())
    policy["organisations"] = {"H": {"customer_ids": ["1"]}}
    policy["identities"] = {"external": {"organisation_id": "H"}}
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Fixture scope"))
    events = []
    gateway.outbox.register("anomaly-test", events.append)
    lookup = gateway.find_offers(need)
    quotes = [quote(gateway, need, qty=200, lookup=lookup) for _ in range(10)]
    orders = [gateway.orders.place_order(q.id, f"burst:{i}") for i, q in enumerate(quotes)]
    assert check(orders[5], "VELOCITY_ANOMALY").status == "anomaly"
    assert orders[5].status == "PENDING_APPROVAL"
    assert orders[-1].status == "PENDING_APPROVAL"
    assert check(orders[-1], "VELOCITY_ANOMALY").observed == 10
    with pytest.raises(DomainError) as error:
        quote(gateway, need)
    assert error.value.code == "AGENT_PAUSED" and error.value.status == 423
    with gateway.db.connect() as conn:
        suspension = json.loads(conn.execute("SELECT data FROM buyer_suspensions WHERE agent_id='external'").fetchone()[0])
        alert = conn.execute("SELECT id,payload FROM events WHERE type='agent.anomaly_alert'").fetchone()
        assert suspension["source"] == "anomaly" and suspension["reason"]
        assert json.loads(alert[1])["customer_id"] == "1"
        assert conn.execute("SELECT status FROM outbox WHERE event_id=? AND consumer='anomaly-test'", (alert[0],)).fetchone()[0] == "delivered"
    assert any(e["type"] == "agent.anomaly_alert" for e in events)
    # The buyer controls already in spec 28 remain the authorized pause/resume seam.
    admin = Principal("reviewer", kind="buyer_user", role="buyer_admin", organisation_id="H", customer_ids=("1",))
    gateway.buyer.control("external", False, "Burst investigated", admin)
    assert gateway.orders.get_order(orders[-1].id).status == "PENDING_APPROVAL"
    assert quote(gateway, need).id
    assert {"AGENT_SUSPENDED", "AGENT_RESUMED", "AGENT_ANOMALY_ALERT"} <= {e.action for e in gateway.audit()}


def test_ac_8_new_category_cold_start_scoped_history_and_category_fallback(gateway, need, upstream, tmp_path):
    # A different customer/agent must neither supply a baseline nor suppress novelty.
    seed(gateway, agent="someone-else")
    seed(gateway, customer="2")
    seed(gateway, count=3)
    cold = quote(gateway, need, qty=2000)
    assert all(c.status == "insufficient_history" for c in cold.anomalies)
    assert not {w.code for w in cold.warnings} & {"NEW_CATEGORY", "QUANTITY_ANOMALY", "PRICE_ANOMALY", "VELOCITY_ANOMALY"}
    with gateway.db.connect(write=True) as conn:
        conn.execute("DELETE FROM purchase_memory WHERE json_extract(data,'$.agent_id')='external' AND customer_id='1'")
    seed(gateway)
    # New product, same category: normalized category history protects the quantity.
    category = quote(gateway, need, product="4", qty=1000)
    assert check(category, "QUANTITY_ANOMALY").status == "anomaly"
    assert check(category, "QUANTITY_ANOMALY").unit == "L"
    with gateway.db.connect(write=True) as conn:
        raw = json.loads(conn.execute("SELECT data FROM products WHERE id='4'").fetchone()[0])
        raw["category"] = "new-category"
        conn.execute("UPDATE products SET data=? WHERE id='4'", (json.dumps(raw),))
    novel = quote(gateway, need, product="4", qty=100)
    assert check(novel, "NEW_CATEGORY").status == "anomaly"
    ordered = gateway.orders.place_order(novel.id, "new-category")
    assert "NEW_CATEGORY" in {h.rule for h in ordered.policy.hits}


def test_ac_9_forecast_stub_enabled_fallback_and_lazy_loading(gateway, need, upstream, monkeypatch, tmp_path):
    from app.config import Settings
    from app.forecast_timestral import load_forecaster
    from app.service import Gateway
    class Stub:
        def predict(self, series, horizon):
            assert len(series) >= 4 and horizon == 1
            return {.05: 150, .95: 250}
    stub = Stub()
    monkeypatch.setenv("FORECAST_ENABLED", "true")
    monkeypatch.setenv("FORECAST_MODEL_PATH", str(tmp_path / "weights"))
    settings = Settings.from_env()
    assert settings.forecast_enabled and settings.forecast_model_path == tmp_path / "weights"
    monkeypatch.setattr("app.forecast_timestral.load_forecaster", lambda enabled, path: stub if enabled else None)
    other = Gateway(Settings(db_path=tmp_path / "forecast/app.db", forecast_enabled=True))
    try:
        assert other.anomalies.forecaster is stub
    finally:
        other.close()
    seed(gateway)
    gateway.anomalies.forecaster = stub
    first = quote(gateway, need)
    assert check(first, "QUANTITY_ANOMALY").band_source == "forecast"
    assert check(first, "QUANTITY_ANOMALY").expected_band["upper"] == 250
    class Broken:
        def predict(self, series, horizon):
            raise RuntimeError("model failure")
    gateway.anomalies.forecaster = Broken()
    second = quote(gateway, need)
    assert check(second, "QUANTITY_ANOMALY").band_source == "statistical"
    assert check(second, "QUANTITY_ANOMALY").expected_band["upper"] == 200
    assert load_forecaster(False, None) is None and load_forecaster(True, tmp_path / "missing") is None
    assert not __import__("sys").modules.get("timestral")


def test_ac_10_synthetic_90_day_history_keeps_hotel_demo_confirmed(gateway, need, upstream, tmp_path):
    demo_catalog(gateway, tmp_path, upstream)
    assert seed_anomaly_history(gateway.db) == 180
    with gateway.db.connect() as conn:
        rows = [json.loads(r[0]) for r in conn.execute("SELECT data FROM purchase_memory")]
        assert len(rows) == 180 and all(r["synthetic"] for r in rows)
        assert len({r["date"] for r in rows}) == 90
        assert len({r["unit_price"] for r in rows}) == 3
    milk = quote(gateway, need, qty=1000, agent="hotel-chain-agent", product="464306")
    assert milk.total == 1010 and all(c.status == "normal" for c in milk.anomalies)
    ordered = gateway.orders.place_order(milk.id, "demo-spec45", "hotel-chain-agent")
    assert ordered.status == "CONFIRMED" and ordered.total == 1010
    assert all(c.status == "normal" for c in ordered.anomalies)
    assert not {w.code for w in ordered.warnings} & {"QUANTITY_ANOMALY", "PRICE_ANOMALY", "VELOCITY_ANOMALY", "NEW_CATEGORY"}
    # Stored confirmed orders still supply history when memory rows are unavailable.
    with gateway.db.connect(write=True) as conn:
        conn.execute("DELETE FROM purchase_memory")
        history = gateway.anomalies.history(conn, "hotel-chain-agent", "1", "valio", audit.utcnow() + timedelta(seconds=1), 90)
        assert len(history) == 1 and history[0]["sales_units"] == 1000


@pytest.mark.parametrize("tenant,role,status", [("hotel-aurora", "approver", 200),
    ("hotel-aurora", "viewer", 403), ("kaari-hospital", "approver", 404)])
def test_ac_5_tenant_reviewer_fix_preserves_scope_and_role(gateway, need, upstream, tenant, role, status):
    from app.foundation import PRINCIPAL

    seed(gateway)
    need.quantity = Quantity(value=10000, unit="L")
    original = quote(gateway, need, 2000)
    reviewer = Principal("tenant-reviewer", kind="tenant_user", role=role, tenant_id=tenant,
        user_id="tenant-reviewer", customer_ids=("1",) if tenant == "hotel-aurora" else ("2",))
    token = PRINCIPAL.set(reviewer)
    try:
        if status == 200:
            result = gateway.anomalies.apply_fix(original.id, "1:correct",
                reviewer.agent_id, principal=reviewer)
            assert result.quotes[0].lines[0].units_needed == 200
        else:
            with pytest.raises(DomainError) as error:
                gateway.anomalies.apply_fix(original.id, "1:correct",
                    reviewer.agent_id, principal=reviewer)
            assert error.value.status == status
    finally:
        PRINCIPAL.reset(token)
