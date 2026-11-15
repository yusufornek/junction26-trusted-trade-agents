"""Spec 41: one end-to-end test per acceptance criterion, offline and isolated."""
import asyncio
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

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
from test_supplier_adapters import nordfood, prepare as prepare_nordfood, reference


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
    mixed = deepcopy(purchases[:3])
    mixed[0]["supplier_id"] = "nordfood"
    assert detect_patterns(mixed) == []  # Matching SKUs from two suppliers never form one series.


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


@pytest.mark.parametrize("monthly_day", [1, 30])
@pytest.mark.usefixtures("fixed_clock")
def test_ac_5_make_recurring_uses_pattern_baseline_normal_runs_and_drift_and_mcp_pause(gateway, memory_setup, upstream, monthly_day):
    from app.recurring import rule_for
    from app.runtime import CLOCK
    if monthly_day != 1:
        with gateway.db.connect(write=True) as conn:
            for row in conn.execute("SELECT id,data FROM purchase_memory").fetchall():
                item = json.loads(row["data"])
                item["date"] = item["date"][:8] + str(monthly_day)
                conn.execute("UPDATE purchase_memory SET data=? WHERE id=?", (json.dumps(item), row["id"]))
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
        expected = "FREQ=MONTHLY;BYMONTHDAY=1" if monthly_day == 1 else "FREQ=MONTHLY;BYMONTHDAY=28,29,30;BYSETPOS=-1"
        assert row["cadence"]["rrule"] == expected
        scheduled = datetime.fromisoformat(row["next_run_at"])
        assert rule_for(row).after(scheduled, inc=True) == scheduled
        assert not tool(gateway, "list_recurring_orders", {}).is_error
        assert suggestions(client) == []
        upstream.need.deliver_by = audit.utcnow().astimezone(ZoneInfo(row["cadence"]["timezone"])).date()
        first = tool(gateway, "run_recurring_order_now", {"recurring_order_id": row["id"]})
        assert not first.is_error and first.structured_content["status"] == "CONFIRMED", first.structured_content
        assert first.structured_content["comparison"]["diff"] == []
        when = audit.utcnow() + timedelta(days=1)
        upstream.need.deliver_by = when.astimezone(ZoneInfo(row["cadence"]["timezone"])).date()
        upstream.price *= 1.2
        second = gateway.recurring.run(row["id"], when)
        assert second["status"] == "PENDING_APPROVAL"
        assert any(h.rule == "recurring_drift" for h in gateway.orders.get_order(second["order_id"]).policy.hits)
        upstream.need.deliver_by = scheduled.date()
        upstream.cutoff = scheduled + timedelta(hours=1)
        token = CLOCK.set(lambda: scheduled)
        class ScheduledDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return scheduled.astimezone(tz) if tz else scheduled.replace(tzinfo=None)
        try:
            with patch("app.audit.utcnow", return_value=scheduled), patch("app.models.datetime", ScheduledDateTime):
                gateway.recurring.due(scheduled)
                gateway.recurring.due(scheduled)
        finally:
            CLOCK.reset(token)
        scheduled_runs = [r for r in gateway.recurring.runs(row["id"]) if datetime.fromisoformat(r["occurrence_at"]) == scheduled]
        assert len(scheduled_runs) == 1 and scheduled_runs[0]["status"] == "PENDING_APPROVAL"
        assert any(h.rule == "recurring_drift" for h in gateway.orders.get_order(scheduled_runs[0]["order_id"]).policy.hits)
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
    import yaml
    from app.config import BACKEND
    from app.developer_docs import generated, tool_schemas
    schema = create_app(gateway).openapi()
    for path, method in [("/v1/suggestions", "get"), ("/v1/buyer/memory-preferences", "put"),
                         *[(f"/v1/suggestions/{{id}}/{name}", "post") for name in ("prepare", "make-recurring", "dismiss", "snooze")]]:
        assert method in schema["paths"][path]
    names = {t.name for t in asyncio.run(create_server(gateway, "external").list_tools())}
    assert {"get_replenishment_suggestions", "prepare_suggestion", "create_recurring_from_suggestion",
            "list_recurring_orders", "create_recurring_order", "run_recurring_order_now", "pause_recurring_order"} <= names
    schemas = asyncio.run(tool_schemas())
    assert (BACKEND / "static/developers.html").read_text() == generated(schemas)
    assert json.loads((BACKEND / "static/tools.json").read_text()) == schemas
    assert yaml.safe_load((BACKEND.parent / "contracts/openapi.yaml").read_text()) == schema


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


def memory_token(key, **scope):
    from app.mandates import decode, sign_jwt
    claims = decode(issue(key))[1]
    claims["sub"] = claims["vc"]["credentialSubject"]["agent_id"] = "external"
    claims["vc"]["credentialSubject"].update(scope)
    return sign_jwt(key, claims, kid="buyer-1")


@pytest.fixture
def nordfood_history(gateway, memory_setup, need, upstream, nordfood):
    request = reference(need, quantity=Quantity(value=24, unit="L"))
    for month in (9, 10, 11):
        with patch("app.audit.utcnow", return_value=datetime(2026, month, 1, 9, tzinfo=timezone.utc)):
            quote = prepare_nordfood(gateway, request)
            order = gateway.orders.place_order(quote.id, f"nf-history:{month}")
            if order.status == "PENDING_APPROVAL":
                order = gateway.orders.approve(order.id, "Historical buyer")
            assert order.status == "CONFIRMED"
    return order


@pytest.mark.parametrize("withdrawal", ["customer", "grant"])
def test_review_memory_current_customer_grant(gateway, memory_setup, withdrawal):
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        id = suggestions(client)[0]["id"]
        policy = gateway.policy._editable(gateway.policy.get())
        if withdrawal == "customer":
            policy["agents"]["external"]["customer_ids"] = ["2"]
        else:
            del policy["agents"]["external"]
        gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Revoke access"))
        code = "CUSTOMER_NOT_IN_MANDATE" if withdrawal == "customer" else "AGENT_NOT_REGISTERED"
        response = client.get("/v1/suggestions?horizon=60")
        assert response.status_code == 403 and response.json()["code"] == code
        result = tool(gateway, "get_replenishment_suggestions", {"horizon": 60})
        assert result.is_error and result.structured_content["code"] == code
        for action, payload in (("dismiss", None), ("snooze", {"days": 2}), ("prepare", {}), ("make-recurring", {})):
            assert client.post(f"/v1/suggestions/{id}/{action}", json=payload).status_code == 403
        assert tool(gateway, "prepare_suggestion", {"suggestion_id": id}).is_error
        assert suggestions(client, headers=agent_headers("observer"))[0]["id"] == id


@pytest.mark.parametrize("authority", ["policy", "vc"])
def test_review_memory_supplier_scope(gateway, nordfood_history, vc_issuer, authority):
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        nf_id = next(r["id"] for r in suggestions(client) if r["product_id"] == "NF-001")
        prepared = client.post(f"/v1/suggestions/{nf_id}/prepare", headers={"Idempotency-Key": "before-supplier-revocation"})
        assert prepared.status_code == 200
    headers = agent_headers("external")
    if authority == "vc":
        headers["Agent-Mandate"] = memory_token(vc_issuer, supplier_ids=["valio"])
    else:
        policy = gateway.policy._editable(gateway.policy.get())
        policy["agents"]["external"]["supplier_ids"] = ["valio"]
        gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Supplier restriction"))
    with TestClient(create_app(gateway), headers=headers) as client:
        rows = suggestions(client)
        assert [r["product_id"] for r in rows] == ["1"]
        result = tool(gateway, "get_replenishment_suggestions", {"horizon": 60}, mandate=headers.get("Agent-Mandate"))
        assert not result.is_error
        assert [r["product_id"] for r in result.structured_content["result"]] == ["1"]
        assert client.post(f"/v1/suggestions/{nf_id}/prepare", headers={"Idempotency-Key": "before-supplier-revocation"}).status_code == 404
        assert client.post(f"/v1/suggestions/{nf_id}/dismiss").status_code == 404
        assert any(r["id"] == nf_id for r in suggestions(client, headers=agent_headers("observer")))


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("purchase_count", [1, 3])
def test_review_memory_categories_use_historical_supplier(gateway, nordfood_history, vc_issuer, legacy, purchase_count):
    if purchase_count == 1:
        with gateway.db.connect(write=True) as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM purchase_memory WHERE json_extract(data,'$.supplier_id')='nordfood' ORDER BY id")]
            for id in ids[1:]:
                conn.execute("DELETE FROM purchase_memory WHERE id=?", (id,))
    if legacy:
        # Old memory entries did not carry a top-level supplier; the stored need did.
        with gateway.db.connect(write=True) as conn:
            for row in conn.execute("SELECT id,data FROM purchase_memory").fetchall():
                item = json.loads(row["data"])
                item.pop("supplier_id", None)
                conn.execute("UPDATE purchase_memory SET data=? WHERE id=?", (json.dumps(item), row["id"]))
    token = memory_token(vc_issuer, categories=["10400"])
    with TestClient(create_app(gateway), headers={**agent_headers("external"), "Agent-Mandate": token}) as client:
        expected = {"1", "NF-001"} if purchase_count == 3 else {"1"}
        assert {r["product_id"] for r in suggestions(client)} == expected


@pytest.mark.parametrize("authority", ["policy", "vc"])
def test_review_prepare_historical_supplier(gateway, nordfood_history, vc_issuer, authority):
    headers = agent_headers("external")
    if authority == "vc":
        headers["Agent-Mandate"] = memory_token(vc_issuer, supplier_ids=["nordfood"])
    else:
        policy = gateway.policy._editable(gateway.policy.get())
        policy["agents"]["external"]["supplier_ids"] = ["nordfood"]
        gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Nordfood authority"))
    with TestClient(create_app(gateway), headers=headers) as client:
        id = next(r["id"] for r in suggestions(client) if r["product_id"] == "NF-001")
        before = counts(gateway)
        response = client.post(f"/v1/suggestions/{id}/prepare", headers={"Idempotency-Key": "nf-prepare"})
        assert response.status_code == 200, response.text
        assert response.json()["supplier_id"] == "nordfood" and response.json()["plans"]
        assert counts(gateway) == before
        replay = client.post(f"/v1/suggestions/{id}/prepare", headers={"Idempotency-Key": "nf-prepare"})
        assert replay.status_code == 200 and replay.json() == response.json()


def test_review_pause_wins_before_run_transaction(gateway, memory_setup, monkeypatch):
    from app.db import DomainError
    from app.purchase_memory import MakeRecurring
    principal = Principal("external", customer_ids=("1",))
    pattern = gateway.memory.suggestions(principal, 60)[0]
    row = gateway.memory.act(pattern["id"], "made_recurring", principal, MakeRecurring())
    original = gateway.limiter.consume
    paused = False
    def admission(agent, category):
        nonlocal paused
        if category == "mutation" and not paused:
            paused = True
            gateway.recurring.pause(row["id"], principal)
            assert gateway.recurring.get(row["id"])["status"] == "PAUSED"
        return original(agent, category)
    before = counts(gateway)
    monkeypatch.setattr(gateway.limiter, "consume", admission)
    with pytest.raises(DomainError, match="INVALID_TRANSITION"):
        gateway.recurring.run(row["id"], principal=principal)
    assert paused and counts(gateway) == before and gateway.recurring.runs(row["id"]) == []


def test_review_month_end_prediction_is_scheduled(gateway, memory_setup, upstream):
    from app.purchase_memory import MakeRecurring
    from app.recurring import rule_for
    from app.runtime import CLOCK
    principal = Principal("external", customer_ids=("1",))
    with gateway.db.connect(write=True) as conn:
        rows = conn.execute("SELECT id,data FROM purchase_memory ORDER BY rowid").fetchall()
        for row, when in zip(rows, ["2026-08-30", "2026-09-30", "2026-10-30", "2026-11-30", "2026-12-30", "2027-01-30"]):
            item = json.loads(row["data"])
            item["date"] = when
            conn.execute("UPDATE purchase_memory SET data=? WHERE id=?", (json.dumps(item), row["id"]))
    pattern = gateway.memory.patterns(principal)[0]
    assert pattern["predicted_next_need_date"] == "2027-02-28"
    when = datetime(2027, 2, 28, tzinfo=timezone.utc)
    token = CLOCK.set(lambda: when)
    try:
        with patch("app.audit.utcnow", return_value=when):
            row = gateway.recurring.from_suggestion(pattern, gateway.memory.need(pattern), "external", MakeRecurring())
    finally:
        CLOCK.reset(token)
    predicted = datetime.fromisoformat(row["next_run_at"])
    assert rule_for(row).after(predicted, inc=True) == predicted
    assert rule_for(row).after(predicted).date().isoformat() == "2027-03-30"
    upstream.need.deliver_by = predicted.date()
    upstream.cutoff = when + timedelta(hours=1)
    class ScheduledDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return when.astimezone(tz) if tz else when.replace(tzinfo=None)
    token = CLOCK.set(lambda: when)
    try:
        with patch("app.audit.utcnow", return_value=when), patch("app.models.datetime", ScheduledDateTime):
            gateway.recurring.due(when)
            gateway.recurring.due(when)
    finally:
        CLOCK.reset(token)
    runs = gateway.recurring.runs(row["id"])
    assert len(runs) == 1 and runs[0]["status"] == "CONFIRMED"
    assert datetime.fromisoformat(runs[0]["occurrence_at"]) == predicted


@pytest.mark.usefixtures("fixed_clock")
def test_review_pattern_scaling_does_not_invent_drift(gateway, memory_setup, upstream):
    from app.purchase_memory import MakeRecurring
    principal = Principal("external", customer_ids=("1",))
    with gateway.db.connect(write=True) as conn:
        rows = conn.execute("SELECT id,data FROM purchase_memory ORDER BY rowid").fetchall()
        for index, row in enumerate(rows):
            item = json.loads(row["data"])
            item["quantity"] = 970 if index == len(rows) - 1 else 1000
            item["baseline"]["lines"][0]["quantity"] = item["quantity"] / 5
            item["baseline"]["basket_cost"] = float(str(item["quantity"])) * 1.01
            conn.execute("UPDATE purchase_memory SET data=? WHERE id=?", (json.dumps(item), row["id"]))
    pattern = gateway.memory.suggestions(principal, 60)[0]
    row = gateway.memory.act(pattern["id"], "made_recurring", principal, MakeRecurring())
    assert row["baseline"]["lines"][0]["quantity"] == 200
    assert row["baseline"]["basket_cost"] == 1010
    upstream.need.deliver_by = audit.utcnow().astimezone(ZoneInfo(row["cadence"]["timezone"])).date()
    result = gateway.recurring.run(row["id"], principal=principal)
    assert result["status"] == "CONFIRMED" and result["comparison"]["diff"] == []
