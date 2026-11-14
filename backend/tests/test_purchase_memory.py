"""Spec 41: one end-to-end test per acceptance criterion, offline and isolated."""
import asyncio
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import audit
from app.api import create_app
from app.auth import Principal
from app.mcp_server import create_server
from app.models import PolicyUpdateRequest, Quantity, QuoteRequest
from app.purchase_memory import PurchaseMemory, detect_patterns
from app.sandbox import scenario_gateway
from conftest import OPERATOR_HEADERS, agent_headers, raw_product
from test_verifiable_mandate import buyer as vc_issuer, issue
from test_webhooks import receiver, subscribe


@pytest.fixture
def product_rows():
    return [raw_product("1", "Milk 5 dl lactose free"), raw_product("3", "Milk egg mix", egg=True)]


@pytest.fixture
def memory_setup(gateway, need, upstream):
    policy = gateway.policy._editable(gateway.policy.get())
    policy["agents"]["external"]["recurring"] = True
    policy["agents"]["agent-b"] = {"customer_ids": ["2"], "recurring": True, "max_auto_daily_total_eur": 5000}
    policy["organisations"] = {"H": {"customer_ids": ["1"]}, "B": {"customer_ids": ["2"]}}
    policy["identities"] = {"external": {"organisation_id": "H"},
        "agent-b": {"organisation_id": "B"},
        "hotel-agent": {"kind": "buyer_user", "role": "buyer_admin", "organisation_id": "H"},
        "observer": {"kind": "buyer_user", "role": "buyer_viewer", "organisation_id": "H"},
        "other": {"kind": "supplier_agent", "role": "supplier_agent"}}
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Purchase memory fixture"))
    upstream.sizes["1"] = 5
    upstream.price = 5.05
    orders = []
    # History passes the real confirmation path. It cannot include pending/rejected purchases.
    for month, quantity in zip(range(6, 12), [980, 1000, 1020, 1000, 990, 1010]):
        need.quantity = Quantity(value=quantity, unit="L")
        with patch("app.audit.utcnow", return_value=datetime(2026, month, 1, 9, tzinfo=timezone.utc)):
            gateway.valio.cache.clear()
            lookup = gateway.find_offers(need)
            quote = gateway.orders.create_quote(QuoteRequest(customer_id="1", deliver_by=need.deliver_by,
                need_ref=lookup.lookup_id, lines=[{"product_id": "1", "quantity_sales_units": quantity / 5}]))
            order = gateway.orders.place_order(quote.id, f"history:{month}")
            assert order.status == "CONFIRMED"
            orders.append(order)
    return orders


def suggestions(client, **kwargs):
    response = client.get("/v1/suggestions?horizon=60", **kwargs)
    assert response.status_code == 200, response.text
    return response.json()


def counts(gateway):
    with gateway.db.connect() as conn:
        return tuple(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("quotes", "orders"))


def restart_memory(gateway):
    from app.commands import install
    gateway.memory = PurchaseMemory(gateway)
    for name in ("act", "put_preferences"):
        install(gateway, gateway.memory, name, "mutation")


def tool(gateway, name, arguments, agent="external", mandate=None):
    return asyncio.run(create_server(gateway, agent, mandate=mandate).call_tool(name, arguments))


def test_ac_1_six_monthly_purchases_form_high_confidence_calendar_pattern_and_demo():
    from demo.run_replenishment import run
    with scenario_gateway("monthly_milk") as gateway:
        patterns = gateway.memory.patterns(Principal("sandbox-agent", customer_ids=("1",)))
        assert len(patterns) == 1
        pattern = patterns[0]
        assert pattern["median_interval_days"] == 31
        assert pattern["interval_spread_days"] == 1
        assert pattern["typical_quantity"] == 1000 and pattern["unit"] == "L"
        assert pattern["quantity_spread"] == 40
        assert pattern["last_order_date"] == "2026-11-01"
        assert pattern["predicted_next_need_date"] == "2026-12-01"
        assert pattern["confidence"] == "high" and pattern["confidence_reason"]
        assert len(pattern["evidence"]) == 6
        assert pattern["evidence"][0]["total_paid"] == pytest.approx(989.8)
        assert pattern["environment"] == "sandbox"
        asyncio.run(run(gateway))
        assert counts(gateway) == (6, 6)
        assert len(gateway.recurring.rows()) == 1


def test_ac_2_two_orders_have_no_pattern_irregular_intervals_explain_low_confidence(gateway, memory_setup):
    with gateway.db.connect() as conn:
        purchases = [json.loads(r[0]) for r in conn.execute("SELECT data FROM purchase_memory ORDER BY rowid")]
    assert detect_patterns(purchases[:2]) == []
    irregular = deepcopy(purchases)
    for item, when in zip(irregular, ["2026-01-01", "2026-01-02", "2026-01-03", "2026-05-01", "2026-10-01", "2026-11-01"]):
        item["date"] = when
    pattern = detect_patterns(irregular)[0]
    assert pattern["confidence"] == "low"
    assert pattern["interval_spread_days"] > pattern["median_interval_days"] * .5
    assert "interval spread" in pattern["confidence_reason"] and "threshold" in pattern["confidence_reason"]
    # Mixed request units form distinct evidence series.
    mixed = deepcopy(purchases[:3])
    mixed[0]["unit"] = "KG"
    assert detect_patterns(mixed) == []


def test_ac_3_horizon_scopes_agents_buyers_operator_and_mcp_without_cross_org_leaks(gateway, memory_setup):
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        assert client.get("/v1/suggestions?horizon=1").json() == []
        rows = suggestions(client)
        assert len(rows) == 1 and rows[0]["customer_id"] == "1"
        assert rows[0]["suggested_quantity"] == {"value": 1000, "unit": "L"}
        assert "Shall I prepare" in rows[0]["sentence"] and "_latest" not in rows[0]
        assert suggestions(client, headers=agent_headers("agent-b")) == []
        assert suggestions(client, headers=agent_headers("observer"))[0]["id"] == rows[0]["id"]
        assert suggestions(client, headers=OPERATOR_HEADERS)[0]["id"] == rows[0]["id"]
        assert client.get("/v1/suggestions", headers=agent_headers("other")).status_code == 403
        assert client.post(f"/v1/suggestions/{rows[0]['id']}/prepare", headers=agent_headers("agent-b")).status_code == 404
        result = tool(gateway, "get_replenishment_suggestions", {"horizon": 60})
        assert not result.is_error and result.structured_content["result"][0]["id"] == rows[0]["id"]
        assert tool(gateway, "get_replenishment_suggestions", {"horizon": 60}, "agent-b").structured_content["result"] == []
        assert tool(gateway, "get_replenishment_suggestions", {}, "other").is_error


def test_ac_4_prepare_only_looks_up_then_normal_quote_and_order_apply_mandate_and_approval(gateway, memory_setup, upstream):
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        id = suggestions(client)[0]["id"]
        before = counts(gateway)
        response = client.post(f"/v1/suggestions/{id}/prepare")
        assert response.status_code == 200, response.text
        lookup = response.json()
        assert lookup["plans"] and counts(gateway) == before
        assert not tool(gateway, "prepare_suggestion", {"suggestion_id": id}).is_error
        assert counts(gateway) == before
        policy = gateway.policy._editable(gateway.policy.get())
        policy["agents"]["external"]["max_auto_total_eur"] = 1
        gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Buyer limit"))
        upstream.need.deliver_by = datetime.fromisoformat(lookup["need"]["deliver_by"]).date()
        quote = client.post("/v1/quotes", json={"customer_id": "1", "deliver_by": lookup["need"]["deliver_by"],
            "plan_ref": {"lookup_id": lookup["lookup_id"], "plan_index": 0}})
        assert quote.status_code == 200, quote.text
        order = client.post("/v1/orders", json={"quote_id": quote.json()["id"]}, headers={"Idempotency-Key": "prepared-order"})
        assert order.status_code == 200, order.text
        assert order.json()["status"] == "PENDING_APPROVAL"
        with gateway.db.connect() as conn:
            assert conn.execute("SELECT count(*) FROM purchase_memory").fetchone()[0] == 6
        policy["agents"]["external"]["customer_ids"] = ["different"]
        gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Scope removed"))
        refused = client.post(f"/v1/suggestions/{id}/prepare")
        assert refused.status_code == 403 and refused.json()["next_action"] == "CONTACT_ADMIN"


def test_ac_5_make_recurring_uses_pattern_baseline_normal_runs_and_drift_and_mcp_pause(gateway, memory_setup, upstream):
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        id = suggestions(client)[0]["id"]
        before = counts(gateway)
        response = client.post(f"/v1/suggestions/{id}/make-recurring", json={"permitted_deviations": {"price_pct": 5, "cumulative_price_pct": 8}})
        assert response.status_code == 200, response.text
        row = response.json()
        assert counts(gateway) == before
        assert row["template"]["quantity"] == {"value": 1000, "unit": "L"}
        assert row["baseline"]["lines"][0]["quantity"] == 200
        assert row["baseline"]["basket_cost"] == pytest.approx(1010)
        assert row["cadence"]["rrule"] == "FREQ=MONTHLY;BYMONTHDAY=1"
        assert not tool(gateway, "list_recurring_orders", {}).is_error
        assert suggestions(client) == []
        upstream.need.deliver_by = audit.utcnow().date()
        first = tool(gateway, "run_recurring_order_now", {"recurring_order_id": row["id"]})
        assert not first.is_error and first.structured_content["status"] == "CONFIRMED"
        assert first.structured_content["comparison"]["diff"] == []
        when = audit.utcnow() + timedelta(days=1)
        upstream.need.deliver_by = when.date()
        upstream.price *= 1.2
        second = gateway.recurring.run(row["id"], when)
        assert second["status"] == "PENDING_APPROVAL"
        assert any(h.rule == "recurring_drift" for h in gateway.orders.get_order(second["order_id"]).policy.hits)
        paused = tool(gateway, "pause_recurring_order", {"recurring_order_id": row["id"]})
        assert not paused.is_error and paused.structured_content["status"] == "PAUSED"
        assert tool(gateway, "run_recurring_order_now", {"recurring_order_id": row["id"]}).is_error


def test_ac_6_memory_controls_freeze_updates_and_persist_snooze_dismiss_exclusions_and_forget(gateway, memory_setup, upstream, need):
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        id = suggestions(client)[0]["id"]
        admin = agent_headers("hotel-agent")
        assert client.put("/v1/buyer/memory-preferences", json={"enabled": False}).status_code == 403
        assert client.put("/v1/buyer/memory-preferences?customer_id=2", json={}, headers=admin).status_code == 403
        client.put("/v1/buyer/memory-preferences", json={"enabled": False}, headers=admin).raise_for_status()
        assert suggestions(client) == []
        need.quantity = Quantity(value=1000, unit="L")
        lookup = gateway.find_offers(need)
        quote = gateway.orders.create_quote(QuoteRequest(customer_id="1", deliver_by=need.deliver_by,
            need_ref=lookup.lookup_id, lines=[{"product_id": "1", "quantity_sales_units": 200}]))
        assert gateway.orders.place_order(quote.id, "memory-off").status == "CONFIRMED"
        restart_memory(gateway)  # A restart must not backfill the disabled period.
        client.put("/v1/buyer/memory-preferences", json={"suggestion_horizon": 60}, headers=admin).raise_for_status()
        assert client.get("/v1/suggestions").json()[0]["order_count"] == 6
        client.put("/v1/buyer/memory-preferences", json={"excluded_products": ["1"]}, headers=admin).raise_for_status()
        assert suggestions(client) == []
        client.put("/v1/buyer/memory-preferences", json={"suggestion_horizon": 60}, headers=admin).raise_for_status()
        response = client.post(f"/v1/suggestions/{id}/snooze", json={"days": 3})
        assert response.status_code == 200
        restart_memory(gateway)
        assert suggestions(client) == []
        with patch("app.audit.utcnow", return_value=audit.utcnow() + timedelta(days=4)):
            assert len(suggestions(client)) == 1
            assert client.post(f"/v1/suggestions/{id}/dismiss").status_code == 200
        restart_memory(gateway)
        assert suggestions(client) == []
        assert client.delete("/v1/buyer/memory", headers=admin).status_code == 200
        client.put("/v1/buyer/memory-preferences", json={"enabled": True}, headers=admin).raise_for_status()
        assert gateway.memory.patterns(Principal("external", customer_ids=("1",))) == []
        assert any(e.action == "MEMORY_FORGET_REQUESTED" for e in gateway.audit())


def test_ac_7_mcp_create_recurring_requires_permission_and_returns_recovery(gateway, memory_setup, vc_issuer):
    request = {"approved_order_id": memory_setup[-1].id,
        "cadence": {"rrule": "FREQ=MONTHLY", "start_at": (audit.utcnow() + timedelta(days=20)).isoformat()}}
    policy = gateway.policy._editable(gateway.policy.get())
    policy["agents"]["external"]["recurring"] = False
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Recurring denied"))
    denied = tool(gateway, "create_recurring_order", {"request": request})
    assert denied.is_error
    assert denied.structured_content["code"] == "RECURRING_NOT_AUTHORIZED"
    assert denied.structured_content["next_action"] == "CONTACT_ADMIN"
    assert denied.structured_content["recovery_version"] == 1
    assert denied.structured_content["retryable"] is False
    assert gateway.recurring.rows() == []
    assert tool(gateway, "list_recurring_orders", {}).is_error
    policy["agents"]["external"]["recurring"] = True
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Recurring granted"))
    # Policy permission cannot add a missing claim to a presented buyer-signed mandate.
    from app.mandates import decode, sign_jwt
    claims = decode(issue(vc_issuer))[1]
    claims["sub"] = claims["vc"]["credentialSubject"]["agent_id"] = "external"
    claims["vc"]["credentialSubject"]["recurring"] = False
    token = sign_jwt(vc_issuer, claims, kid="buyer-1")
    denied_vc = tool(gateway, "create_recurring_order", {"request": request}, mandate=token)
    assert denied_vc.is_error and denied_vc.structured_content["code"] == "RECURRING_NOT_AUTHORIZED"
    assert denied_vc.structured_content["next_action"] == "CONTACT_ADMIN"
    allowed = tool(gateway, "create_recurring_order", {"request": request})
    assert not allowed.is_error and len(gateway.recurring.rows()) == 1


def test_ac_8_each_suggestion_action_is_audited_and_emitted_in_scoped_durable_outbox(gateway, memory_setup, receiver):
    subscribe(gateway, event_types=["suggestion." + name for name in ("shown", "prepared", "made_recurring", "dismissed", "snoozed")])
    delivered = []
    gateway.outbox.register("memory-webhook-test", lambda event: delivered.append(event) if event["type"].startswith("suggestion.") else None)
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        id = suggestions(client)[0]["id"]
        assert client.post(f"/v1/suggestions/{id}/prepare").status_code == 200
        assert client.post(f"/v1/suggestions/{id}/snooze", json={"days": 1}).status_code == 200
        with patch("app.audit.utcnow", return_value=audit.utcnow() + timedelta(days=2)):
            result = tool(gateway, "create_recurring_from_suggestion", {"suggestion_id": id})
            assert not result.is_error
        # A second independently confirmed series can be dismissed without restoring hidden state.
        with gateway.db.connect(write=True) as conn:
            rows = conn.execute("SELECT data FROM purchase_memory").fetchall()
            for index, row in enumerate(rows):
                data = json.loads(row[0])
                data["product_id"] = "second-product"
                conn.execute("INSERT INTO purchase_memory VALUES(?,?,?)", (f"second:{index}", "1", json.dumps(data)))
        second = suggestions(client)[0]["id"]
        assert client.post(f"/v1/suggestions/{second}/dismiss").status_code == 200
    actions = {"shown", "prepared", "made_recurring", "dismissed", "snoozed"}
    with gateway.db.connect() as conn:
        events = [dict(r) for r in conn.execute("SELECT * FROM events WHERE type LIKE 'suggestion.%'")]
        for event in events:
            assert event["customer_id"] == "1"
            assert conn.execute("SELECT action FROM audit WHERE id=?", (event["audit_id"],)).fetchone()[0] == "SUGGESTION_" + event["type"].split(".")[1].upper()
            assert conn.execute("SELECT status FROM outbox WHERE event_id=? AND consumer='memory-webhook-test'", (event["id"],)).fetchone()[0] == "delivered"
        assert actions == {event["type"].split(".")[1] for event in events}
    assert actions == {event["type"].split(".")[1] for event in delivered}
    assert actions == {json.loads(request.content)["type"].split(".")[1] for request in receiver["calls"]}


@pytest.mark.parametrize("action,payload", [("prepare", {}), ("make-recurring", {}), ("dismiss", None), ("snooze", {"days": 3})])
def test_review_suggestion_action_replay_is_atomic_and_authorized(gateway, memory_setup, action, payload):
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        id = suggestions(client)[0]["id"]
        path = f"/v1/suggestions/{id}/{action}"
        headers = {"Idempotency-Key": "suggestion-action"}
        first = client.post(path, json=payload, headers=headers)
        replay = client.post(path, json=payload, headers=headers)
        assert first.status_code == replay.status_code == 200
        assert first.json() == replay.json()
        with gateway.db.connect() as conn:
            assert conn.execute("SELECT count(*) FROM commands WHERE key='suggestion-action'").fetchone()[0] == 1
        policy = gateway.policy.get()
        policy.agents["external"].customer_ids = ["different"]
        gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Scope removed"))
        assert client.post(path, json=payload, headers=headers).status_code in {403, 404}


def test_review_memory_interfaces_and_mcp_workflows_remain_registered(gateway):
    schema = create_app(gateway).openapi()
    for path, method in [("/v1/suggestions", "get"), ("/v1/buyer/memory-preferences", "put"),
                         *[(f"/v1/suggestions/{{id}}/{name}", "post") for name in ("prepare", "make-recurring", "dismiss", "snooze")]]:
        assert method in schema["paths"][path]
    names = {t.name for t in asyncio.run(create_server(gateway, "external").list_tools())}
    assert {"get_replenishment_suggestions", "prepare_suggestion", "create_recurring_from_suggestion",
            "list_recurring_orders", "create_recurring_order", "run_recurring_order_now", "pause_recurring_order"} <= names


def test_review_preparation_replay_survives_a_new_purchase(gateway, memory_setup, upstream, need):
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        id = suggestions(client)[0]["id"]
        headers = {"Idempotency-Key": "prepare-before-new-purchase"}
        path = f"/v1/suggestions/{id}/prepare"
        prepared = client.post(path, headers=headers)
        assert prepared.status_code == 200
        need.quantity = Quantity(value=1000, unit="L")
        lookup = gateway.find_offers(need)
        quote = gateway.orders.create_quote(QuoteRequest(customer_id="1", deliver_by=need.deliver_by,
            need_ref=lookup.lookup_id, lines=[{"product_id": "1", "quantity_sales_units": 200}]))
        assert gateway.orders.place_order(quote.id, "new-purchase-after-preparation").status == "CONFIRMED"
        replay = client.post(path, headers=headers)
        assert replay.status_code == 200, replay.text
        assert replay.json()["lookup_id"] == prepared.json()["lookup_id"]
