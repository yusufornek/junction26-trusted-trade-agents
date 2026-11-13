import asyncio
import json
from datetime import timedelta

import pytest
import yaml
from fastapi.testclient import TestClient

from app.api import create_app
from app.mcp_server import create_server
from app.models import NeedSpec, PolicyUpdateRequest, QuoteRequest
from conftest import raw_product

AGENT = "hotel-chain-agent"
SIZES = {"1": 1, "2": 10, "3": 6, "4": 4, "5": 1, "6": 1}


@pytest.fixture
def product_rows():
    return [raw_product(pid, f"Milk {size} L", volume=size, multiplier=1,
                        brand="Valio" if int(pid) % 2 else "Arla") for pid, size in SIZES.items()]


@pytest.fixture(autouse=True)
def exception_upstream(upstream):
    upstream.model, upstream.amount = "Stock", 0
    upstream.sizes = SIZES.copy()
    upstream.prices = {pid: size for pid, size in SIZES.items()}


def find(gateway, need, **changes):
    body = need.model_dump(mode="json")
    body.update(changes)
    gateway.valio.cache.clear()
    return gateway.find_offers(NeedSpec.model_validate(body), actor=f"agent:{AGENT}")


def quote(gateway, need, lookup, index=0):
    return gateway.orders.create_quote(QuoteRequest(
        customer_id=need.customer_id, deliver_by=need.deliver_by,
        plan_ref={"lookup_id": lookup.lookup_id, "plan_index": index}), AGENT)


def update(gateway, *, global_limit=None, per_order=None, daily=5000):
    body = gateway.policy.get().model_dump(mode="json")
    if global_limit is not None:
        body["approval"]["max_auto_total_eur"] = global_limit
    body["agents"][AGENT]["max_auto_total_eur"] = per_order
    body["agents"][AGENT]["max_auto_daily_total_eur"] = daily
    gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Ada"))


def test_exceptions_ac_1_single_preview_identity_no_side_effects_and_legacy(gateway, need, upstream):
    upstream.amounts = {"1": 1000}
    with TestClient(create_app(gateway)) as client:
        lookup = client.post("/v1/offers", json=need.model_dump(mode="json"),
                             headers={"X-Agent-Id": AGENT}).json()
        plan, = lookup["plans"]
        assert plan["kind"] == "single" and plan["lines"][0]["units"] == 1000
        assert plan["policy_preview"] == {"outcome": "CONFIRMED", "hits": []}
        unknown = client.post("/v1/offers", json=need.model_dump(mode="json")).json()
        assert unknown["plans"][0]["policy_preview"]["outcome"] == "PENDING_APPROVAL"
        assert unknown["plans"][0]["policy_preview"]["hits"][0]["rule"] == "unregistered_agent"

        async def check_mcp():
            for identity, expected in [(AGENT, "CONFIRMED"), ("unknown-mcp", "PENDING_APPROVAL")]:
                result = await create_server(gateway, identity).call_tool(
                    "find_offers", {"need": need.model_dump(mode="json")})
                assert not result.is_error
                assert result.structured_content["plans"][0]["policy_preview"]["outcome"] == expected
        asyncio.run(check_mcp())
        with gateway.db.connect() as conn:
            assert all(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
                       for table in ("orders", "quotes", "reservations"))
        assert [event.action for event in gateway.audit()] == ["LOOKUP"] * 4
        # Old JSON lacks every additive lookup field. GET and timeline must still work.
        old = dict(lookup)
        old.pop("plans")
        old.pop("no_match_reason")
        old["need"].pop("preferences")
        with gateway.db.connect(write=True) as conn:
            conn.execute("UPDATE lookups SET data=? WHERE id=?", (json.dumps(old), old["lookup_id"]))
        fetched = client.get(f"/v1/lookups/{old['lookup_id']}")
        assert fetched.status_code == 200 and fetched.json()["plans"] == []
        explicit = QuoteRequest(customer_id="1", deliver_by=need.deliver_by, need_ref=old["lookup_id"],
                                lines=[{"product_id": "1", "quantity_sales_units": 1000}])
        q = gateway.orders.create_quote(explicit, AGENT)
        order = gateway.orders.place_order(q.id, "legacy", AGENT)
        assert client.get(f"/v1/orders/{order.id}/timeline").status_code == 200


def test_exceptions_ac_2_split_whole_units_cheapest_and_bounded(gateway, need, upstream):
    upstream.amounts, upstream.prices["2"] = {"1": 380, "2": 70}, 12
    result = find(gateway, need)
    plan, = result.plans
    assert plan.kind == "split" and len(plan.lines) == 2
    assert {line.product_id: line.units for line in plan.lines} == {"1": 380, "2": 62}
    assert plan.coverage.value == sum(line.covers.value for line in plan.lines) == 1000
    assert plan.total == 380 + 62 * 12
    assert plan.policy_preview.outcome == "CONFIRMED"  # No stale per-offer PARTIAL_STOCK.
    for line in plan.lines:
        assert line.units == int(line.units) <= upstream.amounts[line.product_id]
    q = quote(gateway, need, result)
    order = gateway.orders.place_order(q.id, "split", AGENT)
    assert order.policy == plan.policy_preview
    gateway.orders.cancel(order.id, actor=f"agent:{AGENT}")

    # Interior allocation: a cheaper split even though either product could cover the whole need.
    upstream.amounts = {"3": 20, "4": 20}
    upstream.prices.update({"3": 4, "4": 3})
    small = find(gateway, need, quantity={"value": 10, "unit": "L"})
    split, = [p for p in small.plans if p.kind == "split"]
    assert split.total == 7 and {l.product_id: l.units for l in split.lines} == {"3": 1, "4": 1}
    # More than four products would be needed: never emit an invalid fifth line.
    upstream.amounts = {pid: 10 for pid in SIZES}
    limited = find(gateway, need)
    assert len(limited.plans[0].lines) == 4
    assert limited.plans[0].coverage.value == 210


def test_exceptions_ac_3_partial_retains_uncovered_risk_through_order(gateway, need, upstream):
    upstream.amounts = {"1": 200, "2": 40}
    result = find(gateway, need)
    plan, = result.plans
    assert plan.kind == "partial" and plan.coverage.value == 600
    assert plan.uncovered.model_dump() == {"value": 400, "unit": "L"}
    assert [w.code for w in plan.warnings] == ["PARTIAL_STOCK"]
    assert plan.policy_preview.outcome == "PENDING_APPROVAL"
    q = quote(gateway, need, result)
    order = gateway.orders.place_order(q.id, "partial", AGENT)
    assert order.policy == plan.policy_preview
    assert q.warnings[0].code == "PARTIAL_STOCK"


def test_exceptions_ac_4_preview_uses_shared_limits_and_placement_is_authoritative(gateway, need, upstream):
    upstream.amounts = {"1": 10000}
    update(gateway, global_limit=500)
    result = find(gateway, need)
    preview = result.plans[0].policy_preview
    assert preview.outcome == "PENDING_APPROVAL"
    assert [h.rule for h in preview.hits] == ["max_auto_total_eur"]
    assert gateway.orders.place_order(quote(gateway, need, result).id, "global", AGENT).policy == preview
    update(gateway, global_limit=1500, per_order=500)
    result = find(gateway, need)
    assert "mandate" in result.plans[0].policy_preview.hits[0].detail
    assert gateway.orders.place_order(quote(gateway, need, result).id, "mandate", AGENT).policy == result.plans[0].policy_preview
    update(gateway, daily=1500)
    first = find(gateway, need)
    assert gateway.orders.place_order(quote(gateway, need, first).id, "first", AGENT).status == "CONFIRMED"
    second = find(gateway, need)
    preview = second.plans[0].policy_preview
    assert [h.rule for h in preview.hits] == ["max_auto_daily_total_eur"]
    assert "1000.00 + 1000.00 exceeds 1500.00" in preview.hits[0].detail
    assert gateway.orders.place_order(quote(gateway, need, second).id, "daily", AGENT).policy == preview
    # A cached preview does not reserve stock or promise the later result.
    update(gateway, daily=5000)
    fresh = find(gateway, need)
    q = quote(gateway, need, fresh)
    upstream.amounts["1"] = 0
    order = gateway.orders.place_order(q.id, "changed-stock", AGENT)
    assert fresh.plans[0].policy_preview.outcome == "CONFIRMED"
    assert order.status == "PENDING_APPROVAL" and any(h.rule == "PARTIAL_STOCK" for h in order.policy.hits)
    upstream.amounts["1"], upstream.prices["1"] = 10000, 2.675
    fractional = find(gateway, need, quantity={"value": 1, "unit": "L"})
    assert fractional.plans[0].total == 2.67
    assert quote(gateway, need, fractional).total == fractional.plans[0].total


def test_exceptions_ac_5_brand_substitute_ranking_approval_and_old_policy(gateway, need, upstream):
    upstream.amount = 2000
    upstream.prices["2"] = 0.10  # Cheapest product still ranks below every preferred brand.
    result = find(gateway, need, preferences={"brands": ["Valio"]})
    assert [o.product_id for o in result.offers[:3]] == [o.product_id for o in result.offers if int(o.product_id) % 2]
    assert [o.rank_label for o in result.offers[:3]] == ["best", "better", "good"]
    arla = next(o for o in result.offers if o.product_id == "2")
    warning, = [w for w in arla.warnings if w.code == "OUTSIDE_PREFERENCE"]
    assert "Arla" in warning.text and "Valio" in warning.text
    q = gateway.orders.create_quote(QuoteRequest(deliver_by=need.deliver_by, need_ref=result.lookup_id,
        lines=[{"product_id": "2", "quantity_sales_units": arla.units_needed}]), AGENT)
    order = gateway.orders.place_order(q.id, "substitute", AGENT)
    assert order.status == "PENDING_APPROVAL" and [h.rule for h in order.policy.hits] == ["OUTSIDE_PREFERENCE"]
    policy = gateway.policy.get()
    assert "OUTSIDE_PREFERENCE" in policy.known_warning_codes
    assert "OUTSIDE_PREFERENCE" in policy.approval.warnings_need_approval
    upstream.amount, upstream.amounts = 0, {"2": 2000}
    upstream.cutoff -= timedelta(hours=2)
    risky = find(gateway, need, preferences={"brands": ["Valio"]})
    assert gateway.orders.place_order(quote(gateway, need, risky).id, "risky-substitute", AGENT).policy == risky.plans[0].policy_preview
    old = policy.model_dump(mode="json", exclude={"known_warning_codes"})
    old["approval"]["warnings_need_approval"].remove("OUTSIDE_PREFERENCE")
    gateway.policy.runtime_path.write_text(yaml.safe_dump(old))
    assert gateway.policy.get().approval.warnings_need_approval == old["approval"]["warnings_need_approval"]


def test_exceptions_ac_6_pack_price_and_label_differences_are_explained(gateway, need, upstream):
    upstream.amounts = {"1": 2000, "2": 2000}
    result = find(gateway, need, preferences={"pack_size": {"max": 1}, "max_price_per_unit": 0.5,
                                            "labels": ["ORGANIC", "FAIRTRADE"]})
    box = next(o for o in result.offers if o.product_id == "2")
    warning, = [w for w in box.warnings if w.code == "OUTSIDE_PREFERENCE"]
    assert "10 L" in warning.text and "<= 1 L" in warning.text
    assert "1 EUR/L" in warning.text and "0.5" in warning.text
    assert "FAIRTRADE" in warning.text and "ORGANIC" not in warning.text
    minimum = find(gateway, need, preferences={"pack_size": {"min": 2}})
    assert any("preferred >= 2 L" in w.text for w in minimum.offers[-1].warnings)


def test_exceptions_ac_7_plan_ref_rest_mcp_and_validation(gateway, need, upstream):
    upstream.amounts = {"1": 380, "2": 70}
    result = find(gateway, need)
    with TestClient(create_app(gateway)) as client:
        body = {"customer_id": "1", "deliver_by": str(need.deliver_by),
                "plan_ref": {"lookup_id": result.lookup_id, "plan_index": 0}}
        response = client.post("/v1/quotes", json=body, headers={"X-Agent-Id": AGENT})
        assert response.status_code == 200, response.text
        q = response.json()
        assert q["need_ref"] == result.lookup_id
        assert {l["product_id"]: l["units_needed"] for l in q["lines"]} == {l.product_id: l.units for l in result.plans[0].lines}
        for invalid in [dict(body, plan_ref={"lookup_id": result.lookup_id, "plan_index": index}) for index in (-1, 99)]:
            assert client.post("/v1/quotes", json=invalid).status_code == 422
        assert client.post("/v1/quotes", json={k: v for k, v in body.items() if k != "plan_ref"}).status_code == 422
        assert client.post("/v1/quotes", json=dict(body, lines=[{"product_id": "1", "quantity_sales_units": 1}])).status_code == 422
        assert client.post("/v1/quotes", json=dict(body, need_ref="other-lookup")).status_code == 422

        async def check_mcp():
            response = await create_server(gateway, AGENT).call_tool("create_quote", body)
            assert not response.is_error
            assert {l["product_id"]: l["units_needed"] for l in response.structured_content["lines"]} == {l.product_id: l.units for l in result.plans[0].lines}
            assert response.structured_content["total"] == q["total"]
            assert response.structured_content["need_ref"] == result.lookup_id
        asyncio.run(check_mcp())


def test_exceptions_ac_8_no_hard_match_explains_constraint_counts(gateway, need, upstream):
    result = find(gateway, need, required_claims=["FREE_FROM_GLUTEN"], exclude_allergens=["AM"])
    assert result.offers == [] and result.plans == [] and result.excluded_count == 6
    assert "required_claims.FREE_FROM_GLUTEN removed 6" in result.no_match_reason
    assert "exclude_allergens.AM removed 6" in result.no_match_reason
    assert "overlap" in result.no_match_reason
    assert result.live_calls.price == result.live_calls.availability == result.decision_calls == 0


def test_exceptions_ac_9_plans_use_only_priced_offers_without_extra_calls(gateway, need, upstream):
    upstream.amounts = {"1": 380, "2": 70, "3": 1000, "4": 1000, "5": 1000, "6": 1000}
    upstream.fits["3"] = 0.79  # Priced fill-up offer, below the plan suitability floor.
    upstream.shifts["4"] = 1  # Stock after the deadline can never enter a plan.
    result = find(gateway, need)
    assert result.plans
    assert result.live_calls.model_dump() == {"price": 1, "availability": 1}
    assert len(upstream.price_calls) == len(upstream.availability_calls) == len(upstream.decision_calls) == 1
    ids = {o.product_id for o in result.offers}
    assert all(line.product_id in ids - {"3", "4"} for plan in result.plans for line in plan.lines)
    gateway.lookup.get(result.lookup_id)
    assert len(upstream.price_calls) == len(upstream.availability_calls) == len(upstream.decision_calls) == 1


def test_exceptions_brand_preference_ignores_catalog_whitespace(gateway, need, upstream):
    """Catalog brands carry stray whitespace ("Arla "); the preferred brand must still match."""
    upstream.amount = 2000
    product = gateway.catalog.get("2")
    spaced = product.model_copy(update={"brand": f"{product.brand} "})
    with gateway.db.connect(write=True) as conn:
        conn.execute("UPDATE products SET data=? WHERE id='2'", (spaced.model_dump_json(),))
    result = find(gateway, need, preferences={"brands": [f" {product.brand.lower()}"]})
    offer = next(o for o in result.offers if o.product_id == "2")
    assert "OUTSIDE_PREFERENCE" not in {w.code for w in offer.warnings}
