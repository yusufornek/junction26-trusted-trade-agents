from datetime import timedelta

import httpx
import pytest

from app import audit
from app.decisions import OpenAIDecisions
from app.lookup import availability_for, offer_for, size_for
from app.models import Quantity
from app.valio import RequestCounter


def codes(offer):
    return {w.code for w in offer.warnings}


def test_catalog_build_and_hard_filters(gateway):
    assert gateway.catalog.count() == 7
    products, excluded = gateway.catalog.search("milk", ["FREE_FROM_LACTOSE"], ["AE"])
    assert {p.id for p in products} == {"1", "4", "5", "6", "7"}
    assert excluded == 2
    p = gateway.catalog.get("1")
    assert p.delivery_reliability == 0.97
    assert p.co2_kg_per_kg == 1.2
    assert p.free_from == ["ML"]
    assert p.labels == ["ORGANIC"]
    assert not gateway.catalog.search('" OR *', ["ABSENT"], [])[0]


@pytest.mark.parametrize("pid,unit,price,expected,source", [
    ("1", "L", {"catalogPrice": {"value": 13}, "comparisonPrice": {"value": 2.6}, "comparisonUnit": "L"}, 5, "price_ratio"),
    ("4", "L", {}, 10, "catalog"),
    ("1", "L", {}, 5, "name_parse"),
    ("5", "KG", {}, 5, "catalog"),
    ("7", "KG", {}, 1, "base_unit"),
    ("6", "L", {}, None, None),
])
def test_size_paths(gateway, pid, unit, price, expected, source):
    assert size_for(gateway.catalog.get(pid), price, unit) == (expected, source)


def test_name_parse_kg_and_lot_rounding(gateway, need):
    p = gateway.catalog.get("5").model_copy(update={"base_net_weight_kg": None})
    assert size_for(p, {}, "KG") == (5, "name_parse")
    need.quantity = Quantity(value=1.234, unit="KG")
    offer = offer_for(gateway.catalog.get("7"), need, {"catalogPrice": {"value": 3}}, {}, None)
    assert offer.units_needed == 1.24


def test_unknown_size_and_price_flags(gateway, need):
    offer = offer_for(gateway.catalog.get("6"), need, {"error": True}, {"scheduleLines": []})
    assert {"SIZE_UNKNOWN", "PRICE_ERROR", "UNKNOWN_PRODUCT", "NO_FIT_SCORE"} <= codes(offer)
    assert offer.units_needed is None and offer.total is None
    assert size_for(gateway.catalog.get("1"), {"catalogPrice": {"value": -13}, "comparisonPrice": {"value": 2.6}, "comparisonUnit": "L"}, "L") == (5, "name_parse")


def test_warning_flags(need):
    raw = {"_fetched_at": audit.utcnow().isoformat(), "logisticModel": "Stock", "salesBlock": False,
           "closingTime": (audit.utcnow() - timedelta(hours=1)).isoformat(), "scheduleLines": [
               {"availableDate": need.deliver_by.isoformat(), "availableAmount": 5},
               {"availableDate": (need.deliver_by + timedelta(days=1)).isoformat(), "availableAmount": 500}]}
    availability, warnings = availability_for(raw, need.deliver_by, 10, reserved_amount=2)
    assert availability.available_amount == 3
    assert availability.coverage == 0.3
    assert {w.code for w in warnings} == {"DATE_SHIFT", "PARTIAL_STOCK", "CUTOFF_PASSED"}
    raw["scheduleLines"] = []
    assert "UNKNOWN_PRODUCT" in {w.code for w in availability_for(raw, need.deliver_by, 10)[1]}
    raw["salesBlock"] = True
    assert "SALES_BLOCK" in {w.code for w in availability_for(raw, need.deliver_by, 10)[1]}


def test_lookup_batches_filters_and_cache(gateway, need, upstream):
    result = gateway.find_offers(need)
    assert result.excluded_count == 2
    assert result.live_calls.model_dump() == {"price": 1, "availability": 1}
    assert result.decision_calls == 1
    assert len(upstream.price_calls[0]["products"]) <= 10
    assert not {"2", "3"} & set(upstream.price_calls[0]["products"])
    first = next(o for o in result.offers if o.product_id == "1")
    assert first.size_source == "price_ratio" and first.units_needed == 200
    assert first.total == 2600
    assert first.fit == 0.91
    assert first.matched_because == ["FREE_FROM_LACTOSE from catalog nutritionalClaim", "AE absent from catalog allergen CONTAINS"]
    assert [o.rank_label for o in result.offers[:3]] == ["best", "better", "good"]
    second = gateway.find_offers(need)
    assert second.live_calls.price == second.live_calls.availability == 0
    assert len(upstream.availability_calls) == len(upstream.price_calls) == 1
    assert gateway.audit()[-1].data["live_calls"] == {"price": 0, "availability": 0}


def test_no_decisions_key_degrades(gateway, need, upstream):
    gateway.decisions.key = ""
    result = gateway.find_offers(need)
    assert result.decision_calls == 0
    assert "NO_FIT_SCORE" in {w.code for w in result.warnings}
    assert all("NO_FIT_SCORE" in codes(o) for o in result.offers)


def test_decisions_failure_degrades(gateway, need, upstream, respx_mock):
    respx_mock.post("https://api.openai.com/v1/decisions").respond(500)
    result = gateway.find_offers(need)
    assert result.decision_calls == 1
    assert all(o.fit is None for o in result.offers)
    request = upstream.price_calls[0]
    assert request["requestedDate"] == need.deliver_by.isoformat()


def test_upstream_failure_is_visible(gateway, need, upstream, respx_mock):
    respx_mock.post("https://valio.test/api/availability").respond(500)
    result = gateway.find_offers(need)
    assert all("AVAILABILITY_ERROR" in codes(o) for o in result.offers)
    assert result.live_calls.availability == 1


def test_cache_key_date_customer_and_expiry(gateway, need, upstream):
    counter = RequestCounter()
    fetch = gateway.valio.fetch
    fetch("price", "1", need.deliver_by, ["1"], counter)
    fetch("price", "1", need.deliver_by, ["1"], counter)
    fetch("price", "2", need.deliver_by, ["1"], counter)
    fetch("price", "1", need.deliver_by + timedelta(days=1), ["1"], counter)
    assert counter.price == 3
    gateway.valio.ttl = 0
    fetch("price", "1", need.deliver_by, ["1"], counter)
    assert counter.price == 4


def test_decisions_screen_pool_before_live_calls(gateway, need, upstream):
    """A product ranked low by text match still reaches the live shortlist when Decisions rates it best."""
    upstream.decisions_response = None  # replaced below
    def answers(request):
        import json as _json
        body = _json.loads(request.content)
        upstream.decision_calls.append(body)
        return httpx.Response(200, json={"answers": [
            {"type": "predicate", "name": q["name"], "probability": 0.99 if q["name"] == "fit_7" else 0.2}
            for q in body["questions"]]})
    import respx
    respx.post("https://api.openai.com/v1/decisions").mock(side_effect=answers)
    result = gateway.find_offers(need.model_copy(update={"max_candidates": 1}))
    assert len(upstream.decision_calls) == 1
    assert len(upstream.decision_calls[0]["questions"]) == 5  # whole hard-filtered pool screened
    assert [o.product_id for o in result.offers] == ["7"]
    assert upstream.price_calls[0]["products"] == ["7"]  # live calls only for the shortlist
