"""Spec 35 acceptance: fresh purchases, cumulative review, durable occurrences and local time."""
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import audit
from app.api import create_app
from app.auth import Principal
from app.db import DomainError
from app.models import PolicyUpdateRequest, Quantity
from app.recurring import BaselineRequest, RecurringRequest, rule_for
from conftest import agent_headers, quote_request, raw_product, rebuild_catalog
from test_verifiable_mandate import buyer as vc_issuer, issue
from test_supplier_adapters import nordfood, prepare as prepare_nordfood, reference


@pytest.fixture
def product_rows():
    return [raw_product("1", "Milk 5 dl lactose free"), raw_product("3", "Milk egg mix", egg=True)]


@pytest.fixture
def recurring_setup(gateway, need, upstream):
    body = gateway.policy._editable(gateway.policy.get())
    body["agents"]["external"]["recurring"] = True
    body["organisations"] = {"H": {"customer_ids": ["1"]}}
    body["identities"] = {"external": {"organisation_id": "H"},
        "buyer": {"kind": "buyer_user", "role": "buyer_approver", "organisation_id": "H"}}
    gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Fixture"))
    upstream.sizes["1"] = 5
    need.quantity = Quantity(value=10, unit="L")
    lookup = gateway.find_offers(need)
    quote = gateway.orders.create_quote(quote_request(need, qty=2, need_ref=lookup.lookup_id))
    order = gateway.orders.place_order(quote.id, "baseline")
    assert order.status == "CONFIRMED"
    return order


def create(gateway, order, **deviations):
    return gateway.recurring.create(RecurringRequest(approved_order_id=order.id,
        cadence={"rrule": "FREQ=DAILY", "timezone": "Europe/Helsinki", "start_at": audit.utcnow() + timedelta(hours=2)},
        permitted_deviations=deviations), Principal("external", customer_ids=("1",)))


def run(gateway, row, upstream, day, *, price=None):
    when = audit.utcnow() + timedelta(days=day)
    local = when.astimezone(ZoneInfo("Europe/Helsinki"))
    upstream.need.deliver_by = local.date() + timedelta(days=row["delivery_offset_days"])
    if price is not None:
        upstream.price = price
    gateway.valio.cache.clear()
    return gateway.recurring.run(row["id"], when)


def test_ac_1_identical_runs_confirm_without_drift(gateway, upstream, recurring_setup):
    row = create(gateway, recurring_setup)
    first = run(gateway, row, upstream, 0)
    second = run(gateway, row, upstream, 1)
    assert first["status"] == second["status"] == "CONFIRMED"
    assert first["comparison"]["diff"] == second["comparison"]["diff"] == []
    assert first["request_id"] != second["request_id"] and first["quote_id"] != second["quote_id"]
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        assert len(client.get("/v1/recurring-orders").json()) == 1
        assert len(client.get(f"/v1/recurring-orders/{row['id']}/runs").json()) == 2
        assert client.get(f"/v1/recurring-orders/{row['id']}/runs", headers=agent_headers("other")).status_code == 404


@pytest.mark.parametrize("policy_restricted", [False, True])
def test_review_recurring_authorizes_quote_supplier(gateway, need, upstream, recurring_setup, nordfood, vc_issuer, policy_restricted):
    from app.mandates import decode, sign_jwt
    request = reference(need)
    quote = prepare_nordfood(gateway, request)
    order = gateway.orders.place_order(quote.id, "nf-recurring-baseline")
    if order.status == "PENDING_APPROVAL":
        order = gateway.orders.approve(order.id, "Buyer")
    claims = decode(issue(vc_issuer, categories=["10400"]))[1]
    claims["sub"] = claims["vc"]["credentialSubject"]["agent_id"] = "external"
    claims["vc"]["credentialSubject"].update(recurring=True, supplier_ids=["nordfood"])
    token = sign_jwt(vc_issuer, claims, kid="buyer-1")
    policy = gateway.policy._editable(gateway.policy.get())
    if policy_restricted:
        policy["agents"]["external"]["supplier_ids"] = ["nordfood"]
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Supplier authority"))
    with TestClient(create_app(gateway), headers={**agent_headers("external"), "Agent-Mandate": token}) as client:
        args = {"approved_order_id": order.id, "cadence": {"start_at": (audit.utcnow() + timedelta(hours=1)).isoformat()}}
        first = client.post("/v1/recurring-orders", json=args, headers={"Idempotency-Key": "nf-recurring"})
        assert first.status_code == 200, first.text
        assert first.json()["template"]["supplier_id"] == "nordfood"
        again = client.post("/v1/recurring-orders", json=args, headers={"Idempotency-Key": "nf-recurring"})
        assert again.status_code == 200 and again.json() == first.json()
    # Historical workflows without the added top-level supplier still use their template.
    row = gateway.recurring.get(first.json()["id"])
    row.pop("supplier_id")
    with gateway.db.connect(write=True) as conn:
        gateway.recurring.save(conn, row)
    import asyncio
    from app.mcp_server import create_server
    async def workflows():
        server = create_server(gateway, "external", mandate=token)
        listing = await server.call_tool("list_recurring_orders", {})
        assert not listing.is_error
        run = await server.call_tool("run_recurring_order_now", {"recurring_order_id": row["id"]})
        assert not run.is_error, run.structured_content
        assert gateway.orders.get_quote(run.structured_content["quote_id"]).supplier_id == "nordfood"
        paused = await server.call_tool("pause_recurring_order", {"recurring_order_id": row["id"]})
        assert not paused.is_error and paused.structured_content["status"] == "PAUSED"
    asyncio.run(workflows())


def test_ac_2_third_price_step_exceeds_cumulative_tolerance(gateway, upstream, recurring_setup):
    row = create(gateway, recurring_setup, price_pct=5, cumulative_price_pct=8)
    results = [run(gateway, row, upstream, i, price=13 * 1.03 ** (i + 1)) for i in range(3)]
    assert [r["status"] for r in results] == ["CONFIRMED", "CONFIRMED", "PENDING_APPROVAL"]
    order = gateway.orders.get_order(results[-1]["order_id"])
    hits = [h.detail for h in order.policy.hits if h.rule == "recurring_drift"]
    assert any("cumulative +9.3" in h and "unit cost" in h for h in hits)


@pytest.mark.parametrize("request_unit", ["L", "SALES_UNIT"])
def test_ac_3_smaller_pack_shows_conversion_and_requires_review(gateway, upstream, recurring_setup, tmp_path, need, request_unit):
    if request_unit == "SALES_UNIT":
        need.quantity = Quantity(value=2, unit="SALES_UNIT")
        lookup = gateway.find_offers(need)
        q = gateway.orders.create_quote(quote_request(need, qty=2, need_ref=lookup.lookup_id))
        recurring_setup = gateway.orders.place_order(q.id, "sales-unit-ac3")
    row = create(gateway, recurring_setup, price_pct=100, cumulative_price_pct=100, quantity_pct=100, cumulative_quantity_pct=100)
    rebuild_catalog(gateway, tmp_path, [raw_product("1", "Milk 25 cl lactose free", volume=.25), raw_product("3", "Milk egg mix", egg=True)])
    upstream.sizes["1"] = 2.5
    result = run(gateway, row, upstream, 0)
    assert result["status"] == "PENDING_APPROVAL"
    assert any("pack: 5.0 → 2.5" in d for d in result["comparison"]["diff"])
    assert any(h.rule == "recurring_drift" for h in gateway.orders.get_order(result["order_id"]).policy.hits)


def test_ac_4_duplicate_occurrence_creates_one_order_even_after_restart(gateway, upstream, recurring_setup):
    from app.recurring import Recurring
    row = create(gateway, recurring_setup)
    first = run(gateway, row, upstream, 0)
    gateway.recurring = Recurring(gateway)
    again = run(gateway, row, upstream, 0)
    assert first == again
    assert len(gateway.recurring.runs(row["id"])) == 1
    assert len(gateway.orders.list_orders()) == 2  # Approved template plus one occurrence.


def test_ac_5_missing_comparison_evidence_needs_approval(gateway, upstream, recurring_setup):
    row = create(gateway, recurring_setup)
    # Missing historical evidence must never be filled with today's facts.
    row["baseline"]["lines"][0]["pack"] = None
    with gateway.db.connect(write=True) as conn:
        gateway.recurring.save(conn, row)
    result = run(gateway, row, upstream, 0)
    assert result["status"] == "PENDING_APPROVAL"
    assert any("comparison evidence missing" in d for d in result["comparison"]["diff"])


def test_ac_6_buyer_baseline_reset_clears_cumulative_drift_agent_forbidden(gateway, upstream, recurring_setup):
    row = create(gateway, recurring_setup, price_pct=5, cumulative_price_pct=8)
    result = run(gateway, row, upstream, 0, price=13 * 1.06)
    # 6% is above the per-run tolerance; the buyer signs the changed evidence.
    assert result["status"] == "PENDING_APPROVAL"
    gateway.orders.approve(result["order_id"], "Ada", evidence_revision=1)
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        path = f"/v1/recurring-orders/{row['id']}/baseline"
        payload = {"approved_order_id": result["order_id"]}
        assert client.post(path, json=payload).status_code == 403
        response = client.post(path, json=payload, headers=agent_headers("buyer"))
        assert response.status_code == 200, response.text
        assert response.json()["baseline_revision"] == 2
    again = run(gateway, row, upstream, 1, price=13 * 1.06 * 1.03)
    assert again["status"] == "CONFIRMED"
    assert again["comparison"]["diff"] == []


def test_ac_7_revoked_mandate_and_suspension_skip_with_event(gateway, need, upstream, recurring_setup, vc_issuer):
    from app.mandates import decode, sign_jwt
    claims = decode(issue(vc_issuer))[1]
    claims["vc"]["credentialSubject"]["recurring"] = True
    token = sign_jwt(vc_issuer, claims, kid="buyer-1")
    lookup = gateway.find_offers(need, "agent:outsider", mandate=token)
    quote = gateway.orders.create_quote(quote_request(need, qty=2, need_ref=lookup.lookup_id), "outsider", mandate=token)
    order = gateway.orders.place_order(quote.id, "vc-baseline", "outsider", mandate=token)
    row = gateway.recurring.create(RecurringRequest(approved_order_id=order.id,
        cadence={"start_at": audit.utcnow()}), Principal("outsider", customer_ids=("1",)))
    from app.mandates import decode
    claims = decode(token)[1]
    with gateway.db.connect(write=True) as conn:
        conn.execute("INSERT INTO mandate_revocations VALUES(?,?,?,?)", ("1", claims["iss"], claims["jti"], audit.utcnow().isoformat()))
    result = run(gateway, row, upstream, 0)
    assert result["status"] == "SKIPPED" and result["reason"] == "MANDATE_INVALID"
    local = create(gateway, recurring_setup)
    with gateway.db.connect(write=True) as conn:
        conn.execute("INSERT INTO buyer_suspensions VALUES(?,?)", ("external", json.dumps({"suspended": True})))
    assert run(gateway, local, upstream, 0)["reason"] == "AGENT_SUSPENDED"
    with gateway.db.connect() as conn:
        events = [dict(r) for r in conn.execute("SELECT * FROM events WHERE type='recurring.stopped'")]
    assert len(events) == 2 and {e["customer_id"] for e in events} == {"1"}


def test_ac_8_dst_preserves_local_time_and_catch_up_once(gateway, upstream, recurring_setup, monkeypatch):
    row = create(gateway, recurring_setup)
    row["cadence"].update(start_at="2026-10-24T09:00:00+03:00", rrule="FREQ=DAILY")
    rule = rule_for(row)
    before = rule.after(datetime(2026, 10, 24, 8, tzinfo=ZoneInfo("Europe/Helsinki")))
    after = rule.after(before)
    assert before.hour == after.hour == 9
    assert before.utcoffset() == timedelta(hours=3) and after.utcoffset() == timedelta(hours=2)
    # Exercise actual scheduler purchases, including the UTC-offset change.
    row["next_run_at"] = before.isoformat()
    with gateway.db.connect(write=True) as conn:
        gateway.recurring.save(conn, row)
    original_now = audit.utcnow
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return occurrence.astimezone(tz) if tz else occurrence.replace(tzinfo=None)
    monkeypatch.setattr("app.models.datetime", FrozenDateTime)
    from app.runtime import CLOCK
    clock_token = CLOCK.set(lambda: occurrence)
    try:
        for occurrence, expected in ((before, 1), (after, 2)):
            upstream.need.deliver_by = occurrence.date() + timedelta(days=row["delivery_offset_days"])
            upstream.cutoff = occurrence + timedelta(hours=1)
            monkeypatch.setattr(audit, "utcnow", lambda: occurrence - timedelta(minutes=1))
            gateway.recurring.due(audit.utcnow())
            assert len(gateway.recurring.runs(row["id"])) == expected - 1
            monkeypatch.setattr(audit, "utcnow", lambda: occurrence)
            gateway.recurring.due(occurrence)
            gateway.recurring.due(occurrence)
            runs = gateway.recurring.runs(row["id"])
            assert len(runs) == expected
            assert datetime.fromisoformat(runs[-1]["occurrence_at"]).hour == 9
    finally:
        CLOCK.reset(clock_token)
    monkeypatch.setattr(audit, "utcnow", original_now)
    # A missed week consumes only the latest local occurrence and moves the durable cursor.
    row = create(gateway, recurring_setup)
    now = audit.utcnow()
    row["cadence"]["start_at"] = (now - timedelta(days=7)).isoformat()
    row["next_run_at"] = row["cadence"]["start_at"]
    with gateway.db.connect(write=True) as conn:
        gateway.recurring.save(conn, row)
    gateway.recurring.due(now)
    gateway.recurring.due(now)
    assert len(gateway.recurring.runs(row["id"])) == 1
    assert datetime.fromisoformat(gateway.recurring.get(row["id"])["next_run_at"]) > now


def test_review_sales_unit_template_detects_physical_shrink(gateway, need, upstream, recurring_setup, tmp_path):
    from app.models import QuoteRequest
    need.quantity = Quantity(value=2, unit="SALES_UNIT")
    lookup = gateway.find_offers(need)
    q = gateway.orders.create_quote(quote_request(need, qty=2, need_ref=lookup.lookup_id))
    order = gateway.orders.place_order(q.id, "sales-baseline")
    row = create(gateway, order, price_pct=100, cumulative_price_pct=100)
    assert run(gateway, row, upstream, 0)["comparison"]["diff"] == []
    rebuild_catalog(gateway, tmp_path, [raw_product("1", "Milk 25 cl lactose free", volume=.25), raw_product("3", "Milk egg mix", egg=True)])
    upstream.sizes["1"] = 2.5
    result = run(gateway, row, upstream, 1)
    assert result["status"] == "PENDING_APPROVAL"
    assert any("pack: 5.0 → 2.5" in d for d in result["comparison"]["diff"])


def test_review_approved_quantity_and_multiple_lines_are_retained(gateway, need, upstream, recurring_setup, tmp_path):
    from app.models import QuoteRequest
    rebuild_catalog(gateway, tmp_path, [raw_product("1", "Milk 5 dl lactose free"),
        raw_product("2", "Milk second 1 l lactose free", volume=1, multiplier=1), raw_product("3", "Milk egg mix", egg=True)])
    upstream.sizes["2"], upstream.prices["2"] = 1, 8
    need.quantity = Quantity(value=5, unit="L")
    lookup = gateway.find_offers(need)
    q = gateway.orders.create_quote(QuoteRequest(customer_id="1", deliver_by=need.deliver_by, need_ref=lookup.lookup_id,
        lines=[{"product_id": "1", "quantity_sales_units": 4}, {"product_id": "2", "quantity_sales_units": 3}]))
    order = gateway.orders.place_order(q.id, "approved-overbuy")
    row = create(gateway, order)
    assert len(row["approved_lines"]) == 2
    # Templates stored before approved_lines existed recover quantities from their baseline.
    row.pop("approved_lines")
    with gateway.db.connect(write=True) as conn:
        gateway.recurring.save(conn, row)
    result = run(gateway, row, upstream, 0)
    assert result["status"] == "CONFIRMED" and result["comparison"]["diff"] == []
    assert [(l.product_id, l.units_needed) for l in gateway.orders.get_order(result["order_id"]).lines] == [("1", 4), ("2", 3)]


def test_review_overdue_weekly_catchup_executes_once(gateway, upstream, recurring_setup):
    row = create(gateway, recurring_setup)
    row["cadence"].update(start_at=(audit.utcnow() - timedelta(days=6)).isoformat(), rrule="FREQ=WEEKLY")
    row["next_run_at"] = row["cadence"]["start_at"]
    with gateway.db.connect(write=True) as conn:
        gateway.recurring.save(conn, row)
    gateway.recurring.due()
    gateway.recurring.due()
    runs = gateway.recurring.runs(row["id"])
    assert len(runs) == 1
    assert runs[0]["occurrence_key"].endswith((audit.utcnow() - timedelta(days=6)).date().isoformat())
    assert gateway.orders.get_quote(runs[0]["quote_id"]).deliver_by >= audit.utcnow().date()


def test_review_placement_delivery_is_compared(gateway, upstream, recurring_setup, monkeypatch):
    row = create(gateway, recurring_setup)
    original = gateway.orders.place_order
    def shifted(*args, **kwargs):
        upstream.shift = 1
        return original(*args, **kwargs)
    monkeypatch.setattr(gateway.orders, "place_order", shifted)
    result = run(gateway, row, upstream, 0)
    assert result["comparison"]["current"]["lines"][0]["delivery_offset"] == 1
    assert any("delivery_offset" in d for d in result["comparison"]["diff"])
    assert any(h.rule == "recurring_drift" for h in gateway.orders.get_order(result["order_id"]).policy.hits)


def test_review_ancient_schedule_does_not_enumerate_under_writer(gateway, recurring_setup, monkeypatch):
    import app.recurring as module
    row = create(gateway, recurring_setup)
    row["cadence"].update(start_at="0001-01-01T09:00:00+02:00", rrule="FREQ=DAILY")
    row["next_run_at"] = row["cadence"]["start_at"]
    with gateway.db.connect(write=True) as conn:
        gateway.recurring.save(conn, row)
    original = module.rule_for
    def bounded(data):
        # The scheduler may re-anchor a valid ancient record, without scanning millennia.
        assert datetime.fromisoformat(data["cadence"]["start_at"]).year >= audit.utcnow().year - 3
        with gateway.db.connect() as conn:
            assert not conn.in_transaction
        return original(data)
    monkeypatch.setattr(module, "rule_for", bounded)
    monkeypatch.setattr(gateway.recurring, "run", lambda *args: {"status": "SKIPPED"})
    gateway.recurring.due()
    assert datetime.fromisoformat(gateway.recurring.get(row["id"])["next_run_at"]) > audit.utcnow()


@pytest.mark.parametrize("start,rule,now", [
    ("2026-01-31T09:00:00+02:00", "FREQ=MONTHLY;BYMONTHDAY=31", "2026-07-15T12:00:00+03:00"),
    ("2024-02-29T09:00:00+02:00", "FREQ=MONTHLY;INTERVAL=12;BYMONTHDAY=29", "2027-02-28T12:00:00+02:00"),
    ("2096-02-29T09:00:00+02:00", "FREQ=MONTHLY;INTERVAL=48;BYMONTHDAY=29", "2104-02-28T12:00:00+02:00"),
])
def test_review_bounded_monthly_schedule_preserves_skipped_calendar_dates(start, rule, now):
    from app.recurring import recent_schedule
    row = {"cadence": {"start_at": start, "rrule": rule, "timezone": "Europe/Helsinki"}}
    when = datetime.fromisoformat(now).astimezone(ZoneInfo("Europe/Helsinki"))
    original = rule_for(row)
    assert recent_schedule(row, when) == (original.before(when, inc=True), original.after(when))


def test_review_recurring_create_retry_uses_shared_command(gateway, recurring_setup, upstream):
    args = {"approved_order_id": recurring_setup.id, "cadence": {"start_at": (audit.utcnow() - timedelta(seconds=1)).isoformat()}}
    with TestClient(create_app(gateway), headers={**agent_headers("external"), "Idempotency-Key": "recurring-create"}) as client:
        first = client.post("/v1/recurring-orders", json=args)
        again = client.post("/v1/recurring-orders", json=args)
        assert first.status_code == again.status_code == 200
        assert first.json()["id"] == again.json()["id"]
        args["permitted_deviations"] = {"price_pct": 3}
        assert client.post("/v1/recurring-orders", json=args).status_code == 409
    gateway.recurring.due()
    assert len(gateway.recurring.rows()) == 1
    assert len(gateway.orders.list_orders()) == 2


@pytest.mark.parametrize("keyed", [False, True])
def test_review_recurring_creation_charges_one_mutation(gateway, recurring_setup, monkeypatch, keyed):
    from unittest.mock import Mock

    consume = Mock(wraps=gateway.limiter.consume)
    monkeypatch.setattr(gateway.limiter, "consume", consume)
    body = {"approved_order_id": recurring_setup.id,
            "cadence": {"start_at": (audit.utcnow() + timedelta(hours=1)).isoformat()}}
    headers = agent_headers("external")
    if keyed:
        headers["Idempotency-Key"] = "single-recurring-charge"
    with TestClient(create_app(gateway)) as client:
        response = client.post("/v1/recurring-orders", json=body, headers=headers)
        assert response.status_code == 200, response.text
        mutations = [call.args for call in consume.call_args_list if call.args[1] == "mutation"]
        assert mutations == [("external", "mutation")]


def test_review_recurring_rest_mcp_replay_rechecks_permission(gateway, recurring_setup):
    import asyncio
    from app.mcp_server import create_server
    args = {"approved_order_id": recurring_setup.id, "cadence": {"start_at": (audit.utcnow() + timedelta(hours=1)).isoformat()}}
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        first = client.post("/v1/recurring-orders", json=args, headers={"Idempotency-Key": "cross-recurring"})
        assert first.status_code == 200
        async def call():
            return await create_server(gateway, "external").call_tool("create_recurring_order", {"request": args, "idempotency_key": "cross-recurring"})
        replay = asyncio.run(call())
        assert not replay.is_error and replay.structured_content["id"] == first.json()["id"]
        policy = gateway.policy.get()
        policy.agents["external"].recurring = False
        gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Permission removed"))
        denied = client.post("/v1/recurring-orders", json=args, headers={"Idempotency-Key": "cross-recurring"})
        assert denied.status_code == 403 and denied.json()["code"] == "RECURRING_NOT_AUTHORIZED"
        assert len(gateway.recurring.rows()) == 1


def test_review_recurring_replay_uses_bound_signed_mandate(gateway, need, upstream, recurring_setup, vc_issuer):
    from app.mandates import decode, sign_jwt
    claims = decode(issue(vc_issuer))[1]
    claims["vc"]["credentialSubject"]["recurring"] = True
    token = sign_jwt(vc_issuer, claims, kid="buyer-1")
    lookup = gateway.find_offers(need, "agent:outsider", mandate=token)
    quote = gateway.orders.create_quote(quote_request(need, qty=2, need_ref=lookup.lookup_id), "outsider", mandate=token)
    order = gateway.orders.place_order(quote.id, "bound-template", "outsider", mandate=token)
    body = {"approved_order_id": order.id, "cadence": {"start_at": (audit.utcnow() + timedelta(hours=1)).isoformat()}}
    with TestClient(create_app(gateway), headers={**agent_headers("outsider"), "Idempotency-Key": "bound-recurring"}) as client:
        first = client.post("/v1/recurring-orders", json=body)
        assert first.status_code == 200, first.text
        replay = client.post("/v1/recurring-orders", json=body)
        assert replay.status_code == 200, replay.text
        assert replay.json()["id"] == first.json()["id"]


def test_review_historical_sales_unit_template_loads_without_inventing_content(gateway, need, upstream, recurring_setup):
    from app.signing import quote_payload
    need.quantity = Quantity(value=2, unit="SALES_UNIT")
    lookup = gateway.find_offers(need)
    q = gateway.orders.create_quote(quote_request(need, qty=2, need_ref=lookup.lookup_id))
    raw = q.model_dump(mode="json")
    raw["context"]["product_evidence"]["1"].pop("physical_pack")
    raw["signature"] = gateway.signing.sign(quote_payload(raw))
    with gateway.db.connect(write=True) as conn:
        conn.execute("UPDATE quotes SET data=? WHERE id=?", (json.dumps(raw), q.id))
    order = gateway.orders.place_order(q.id, "historical-sales")
    row = create(gateway, order)
    assert row["baseline"]["lines"][0]["pack"] is None
    result = run(gateway, row, upstream, 0)
    assert result["status"] == "PENDING_APPROVAL"
    assert any("comparison evidence missing" in d for d in result["comparison"]["diff"])


def test_r_1_recipe_template_and_explicit_recurring_authority(gateway, need, upstream, recurring_setup):
    from app.models import BasketLookupRequest, QuoteRequest
    from app.recurring import Cadence
    request = BasketLookupRequest(customer_id="1", deliver_by=need.deliver_by,
        manifest={"version": 1, "recipe_name": "Breakfast", "servings": 1,
                  "ingredients": [{"name": "milk", "search_terms": ["milk"],
                      "substitutions_allowed": True,
                      "required_claims": ["FREE_FROM_LACTOSE"], "exclude_allergens": ["AE"],
                      "quantity": {"value": 10, "unit": "L"}}]})
    lookup = gateway.baskets.find_basket_offers(request)
    quote = gateway.orders.create_quote(QuoteRequest(customer_id="1", deliver_by=need.deliver_by,
        basket_ref={"basket_lookup_id": lookup.basket_lookup_id}))
    order = gateway.orders.place_order(quote.id, "recipe-baseline")
    assert order.status == "CONFIRMED"
    row = create(gateway, order)
    result = run(gateway, row, upstream, 0)
    assert row["basket"] and result["status"] == "CONFIRMED"
    assert gateway.orders.get_order(result["order_id"]).basket_ref is not None
    body = gateway.policy._editable(gateway.policy.get())
    body["agents"]["external"]["recurring"] = False
    gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Remove recurring authority"))
    with pytest.raises(DomainError, match="RECURRING_NOT_AUTHORIZED"):
        create(gateway, recurring_setup)
    assert run(gateway, row, upstream, 1)["status"] == "SKIPPED"
    for value in ("FREQ=WEEKLY;BYDAY=MO,WE,FR", "FREQ=MONTHLY;BYMONTHDAY=15"):
        cadence = Cadence(rrule=value, start_at=audit.utcnow())
        assert rule_for({"cadence": cadence.model_dump(mode="json")}).after(cadence.start_at) is not None
    with pytest.raises(ValueError):
        Cadence(rrule="FREQ=DAILY;INTERVAL=0", start_at=audit.utcnow())


def test_r_3_signed_baseline_facts_survive_later_catalog_edits(gateway, upstream, recurring_setup, tmp_path):
    rebuild_catalog(gateway, tmp_path, [raw_product("1", "Milk 5 dl lactose free", brand="Changed"),
                                      raw_product("3", "Milk egg mix", egg=True)])
    row = create(gateway, recurring_setup)
    assert row["baseline"]["lines"][0]["brand"] == "Fixture"
    result = run(gateway, row, upstream, 0)
    assert result["status"] == "PENDING_APPROVAL"
    assert any("brand: Fixture → Changed" in d for d in result["comparison"]["diff"])


@pytest.mark.parametrize("field,value", [("product", "new-sku"), ("brand", "new-brand"),
    ("quantity", 3), ("claims", []), ("allergens", {"AE": ["contains"]}),
    ("delivery_offset", 1), ("evidence", None), ("unit_cost", None)])
def test_r_3_each_comparison_dimension_fails_closed(gateway, recurring_setup, field, value):
    from copy import deepcopy
    from app.recurring import compare, Deviations
    baseline = create(gateway, recurring_setup)["baseline"]
    current = deepcopy(baseline)
    current["lines"][0][field] = value
    diffs = compare(current, baseline, baseline, Deviations())
    assert diffs
    if value is None:
        assert any("comparison evidence missing" in d for d in diffs)


def test_review_recurring_creation_idempotency_and_event(gateway, upstream, recurring_setup):
    body = {"approved_order_id": recurring_setup.id, "cadence": {"rrule": "FREQ=DAILY", "start_at": (audit.utcnow() + timedelta(days=10)).isoformat()}}
    gateway.outbox.register("review-recurring")
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        first = client.post("/v1/recurring-orders", json=body, headers={"Idempotency-Key": "create-once"})
        assert first.status_code == 200, first.text
        replay = client.post("/v1/recurring-orders", json=body, headers={"Idempotency-Key": "create-once"})
        assert replay.json() == first.json()
        import asyncio
        from app.mcp_server import create_server
        mcp = asyncio.run(create_server(gateway, "external").call_tool("create_recurring_order", {
            "request": body, "idempotency_key": "create-once"}))
        assert not mcp.is_error and mcp.structured_content == first.json()
        body["cadence"]["rrule"] = "FREQ=WEEKLY"
        conflict = client.post("/v1/recurring-orders", json=body, headers={"Idempotency-Key": "create-once"})
        assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    with gateway.db.connect() as conn:
        events = conn.execute("SELECT id,audit_id FROM events WHERE type='recurring.created' AND aggregate_id=?", (first.json()["id"],)).fetchall()
        assert len(events) == 1
        assert conn.execute("SELECT 1 FROM audit WHERE id=?", (events[0][1],)).fetchone()
        assert conn.execute("SELECT 1 FROM outbox WHERE event_id=?", (events[0][0],)).fetchone()


def test_review_recurring_creation_audit_failure_is_atomic(gateway, upstream, recurring_setup):
    import sqlite3
    with gateway.db.connect(write=True) as conn:
        conn.execute("CREATE TRIGGER fail_recurring BEFORE INSERT ON audit WHEN NEW.action='RECURRING_CREATED' BEGIN SELECT RAISE(ABORT,'creation audit'); END")
    with pytest.raises(sqlite3.IntegrityError, match="creation audit"):
        create(gateway, recurring_setup)
    assert gateway.recurring.rows() == []
