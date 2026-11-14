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


def test_ac_2_third_price_step_exceeds_cumulative_tolerance(gateway, upstream, recurring_setup):
    row = create(gateway, recurring_setup, price_pct=5, cumulative_price_pct=8)
    results = [run(gateway, row, upstream, i, price=13 * 1.03 ** (i + 1)) for i in range(3)]
    assert [r["status"] for r in results] == ["CONFIRMED", "CONFIRMED", "PENDING_APPROVAL"]
    order = gateway.orders.get_order(results[-1]["order_id"])
    hits = [h.detail for h in order.policy.hits if h.rule == "recurring_drift"]
    assert any("cumulative +9.3" in h and "unit cost" in h for h in hits)


def test_ac_3_smaller_pack_shows_conversion_and_requires_review(gateway, upstream, recurring_setup, tmp_path):
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


def test_ac_8_dst_preserves_local_time_and_catch_up_once(gateway, upstream, recurring_setup):
    row = create(gateway, recurring_setup)
    row["cadence"].update(start_at="2026-10-24T09:00:00+03:00", rrule="FREQ=DAILY")
    rule = rule_for(row)
    before = rule.after(datetime(2026, 10, 24, 8, tzinfo=ZoneInfo("Europe/Helsinki")))
    after = rule.after(before)
    assert before.hour == after.hour == 9
    assert before.utcoffset() == timedelta(hours=3) and after.utcoffset() == timedelta(hours=2)
    # A missed week consumes only the latest local occurrence and moves the durable cursor.
    now = audit.utcnow()
    row["cadence"]["start_at"] = (now - timedelta(days=7)).isoformat()
    row["next_run_at"] = row["cadence"]["start_at"]
    with gateway.db.connect(write=True) as conn:
        gateway.recurring.save(conn, row)
    gateway.recurring.due(now)
    gateway.recurring.due(now)
    assert len(gateway.recurring.runs(row["id"])) == 1
    assert datetime.fromisoformat(gateway.recurring.get(row["id"])["next_run_at"]) > now


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
