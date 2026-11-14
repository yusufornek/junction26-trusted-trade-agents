"""Spec 39: replay organiser answers offline; one regression per acceptance criterion."""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import httpx
from fastapi.testclient import TestClient

from app import audit
from app.api import create_app
from app.lookup import availability_for
from app.mcp_server import create_server
from app.models import Quantity
from app.valio import RequestCounter, ValioClient
from conftest import (OPERATOR_HEADERS, agent_headers, demo_catalog, quote_request,
                      raw_product, rebuild_catalog, replay_recorded_demo)


def codes(offer):
    return {w.code for w in offer.warnings}


def test_ac_1_whole_box_and_kg_units(gateway, need, upstream, tmp_path):
    demo_catalog(gateway, tmp_path, upstream)
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        result = client.post("/v1/offers", json=need.model_dump(mode="json")).json()
        box = next(o for o in result["offers"] if o["product_id"] == "464306")
        assert box["units_needed"] == 1000 and box["size_per_sales_unit"] == 1
        assert gateway.catalog.get("464306").allowed_lot_size == 1
        assert any(p["lines"] == [{"product_id": "464306", "units": 1000,
                                   "covers": {"value": 1000, "unit": "L"}}] for p in result["plans"])
        invalid = client.post("/v1/quotes", json=quote_request(
            need, qty=10.5, product_id="464306", need_ref=result["lookup_id"]).model_dump(mode="json"))
        assert invalid.status_code == 422 and invalid.json()["code"] == "INVALID_LOT_SIZE"
        assert "multiple of 1" in invalid.json()["message"]
        rebuild_catalog(gateway, tmp_path, [raw_product("7", "Milk bulk", sales="KG", base="KG", multiplier=1),
                                           raw_product("egg", "Egg vocabulary", egg=True)])
        kg_need = need.model_copy(update={"quantity": Quantity(value=10.5, unit="KG")})
        lookup = gateway.find_offers(kg_need)
        offer, = lookup.offers
        assert offer.units_needed == 11 and offer.overbuy.amount == 0.5
        assert lookup.plans[0].lines[0].units == 11
        valid = client.post("/v1/quotes", json=quote_request(
            kg_need, qty=11, product_id="7", need_ref=lookup.lookup_id).model_dump(mode="json"))
        assert valid.status_code == 200 and valid.json()["lines"][0]["units_needed"] == 11
        invalid = client.post("/v1/quotes", json=quote_request(
            kg_need, qty=10.5, product_id="7", need_ref=lookup.lookup_id).model_dump(mode="json"))
        assert invalid.status_code == 422 and invalid.json()["code"] == "INVALID_LOT_SIZE"


def test_ac_2_unknown_stock_orderable_without_reservations(gateway, need, upstream, tmp_path):
    demo_catalog(gateway, tmp_path, upstream)
    assert "AVAILABILITY_UNCONFIRMED" in gateway.policy.get().approval.ignored_warnings
    upstream.amount = 2000
    finite_lookup = gateway.find_offers(need)
    finite_quote = gateway.orders.create_quote(quote_request(need, qty=1, product_id="464306", need_ref=finite_lookup.lookup_id))
    finite_order = gateway.orders.place_order(finite_quote.id, "finite-terminal")
    with gateway.db.connect(write=True) as conn:
        assert conn.execute("SELECT quantity FROM reservations WHERE order_id=?", (finite_order.id,)).fetchone()[0] == 1
        conn.execute("UPDATE reservations SET quantity=? WHERE order_id=?", (1e12, finite_order.id))
    for model in ("Terminal", "Stock"):
        for amount in (999999, 666666666, 9999999999):
            upstream.model, upstream.amount = model, amount
            gateway.valio.cache.clear()
            result = gateway.find_offers(need)
            offer = next(o for o in result.offers if o.product_id == "464306")
            assert offer.availability.coverage == 1 and offer.availability.enough
            assert offer.availability.available_amount is None and not offer.availability.availability_known
            assert "AVAILABILITY_UNCONFIRMED" in codes(offer) and "PARTIAL_STOCK" not in codes(offer)
            assert next(w.text for w in offer.warnings if w.code == "AVAILABILITY_UNCONFIRMED") == (
                "Valio does not know this product's stock; its ordering logic accepts any quantity")
            # Reservations from an earlier known-stock state cannot reduce unknown availability.
            quote = gateway.orders.create_quote(quote_request(need, qty=10, product_id="464306", need_ref=result.lookup_id))
            order = gateway.orders.place_order(quote.id, f"{model}-{amount}")
            assert order.status == "CONFIRMED" and order.lines[0].availability.coverage == 1
            with gateway.db.connect(write=True) as conn:
                assert conn.execute("SELECT count(*) FROM reservations WHERE order_id=?", (order.id,)).fetchone()[0] == 0
    raw = {"logisticModel": "Stock", "scheduleLines": [{"availableAmount": 9999999999,
           "availableDate": need.deliver_by.isoformat(), "salesUnit": "ST"}],
           "closingTime": upstream.cutoff.isoformat()}
    available, _ = availability_for(raw, need.deliver_by, 1e12, reserved_amount=1e12, sales_unit="ST")
    assert available.coverage == 1 and available.available_amount is None
    # The sentinel must not override delivery dates, sales blocks or mismatched units.
    raw["scheduleLines"][0]["availableDate"] = (need.deliver_by + timedelta(days=1)).isoformat()
    assert availability_for(raw, need.deliver_by, 100, sales_unit="ST")[0].coverage == 0
    raw["salesBlock"] = True
    assert not availability_for(raw, need.deliver_by, 100, sales_unit="ST")[0].enough
    raw.pop("salesBlock")
    raw["scheduleLines"][0]["salesUnit"] = "KG"
    assert not availability_for(raw, need.deliver_by, 100, sales_unit="ST")[0].enough


def test_ac_3_empty_availability_rejects_without_rank(gateway, need, upstream):
    upstream.unknown = True
    result = gateway.find_offers(need)
    assert result.offers and not result.plans
    for offer in result.offers:
        assert "NOT_AVAILABLE" in codes(offer) and "UNKNOWN_PRODUCT" not in codes(offer)
        assert offer.rank_label is None
        assert next(w.text for w in offer.warnings if w.code == "NOT_AVAILABLE") == "Valio reports this product as not available"
    assert {"NOT_AVAILABLE", "UNKNOWN_PRODUCT"} <= set(gateway.policy.get().reject.on)
    quote = gateway.orders.create_quote(quote_request(need, need_ref=result.lookup_id))
    assert gateway.orders.place_order(quote.id, "unavailable").status == "REJECTED"
    missing, warnings = availability_for({"error": True}, need.deliver_by, 10)
    assert "AVAILABILITY_ERROR" in {w.code for w in warnings}
    assert not {"NOT_AVAILABLE", "UNKNOWN_PRODUCT"} & {w.code for w in warnings}
    assert missing.available_amount == 0


def test_ac_4_cutoff_warning_names_delivery_and_time(gateway, need, upstream):
    upstream.cutoff = audit.utcnow() - timedelta(hours=1)
    result = gateway.find_offers(need)
    offer = next(o for o in result.offers if o.product_id == "1")
    text = next(w.text for w in offer.warnings if w.code == "CUTOFF_PASSED")
    assert text == f"Order cut-off for delivery on {need.deliver_by} passed at {upstream.cutoff.isoformat()}"
    quote = gateway.orders.create_quote(quote_request(need, qty=1, need_ref=result.lookup_id))
    order = gateway.orders.place_order(quote.id, "past-cutoff")
    assert order.status == "PENDING_APPROVAL" and any(h.rule == "CUTOFF_PASSED" for h in order.policy.hits)


def test_ac_5_429_block_cache_recovery_health_and_audit(gateway, need, upstream, upstream_pacer, respx_mock):
    clock = upstream_pacer.clock
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        cached = gateway.find_offers(need)
        uncached = need.model_copy(update={"deliver_by": need.deliver_by + timedelta(days=1)})
        route = respx_mock.post("https://valio.test/api/price").mock(return_value=httpx.Response(429))
        refused = client.post("/v1/offers", json=uncached.model_dump(mode="json"))
        assert refused.status_code == 503
        body = refused.json()
        assert body["code"] == "UPSTREAM_RATE_LIMITED" and body["retryable"]
        assert body["next_action"] == "RETRY_SAME_COMMAND" and 0 < body["retry_after_seconds"] <= 30
        assert int(refused.headers["Retry-After"]) == body["retry_after_seconds"]
        count = route.call_count
        clock.sleep(5)
        again = client.post("/v1/offers", json=uncached.model_dump(mode="json"))
        assert again.status_code == 503 and 0 < again.json()["retry_after_seconds"] <= 25
        assert route.call_count == count
        mcp = asyncio.run(create_server(gateway, "external").call_tool("find_offers", {"need": uncached.model_dump(mode="json")}))
        assert mcp.is_error and mcp.structured_content["code"] == "UPSTREAM_RATE_LIMITED"
        assert route.call_count == count
        cached_again = client.post("/v1/offers", json=need.model_dump(mode="json"))
        assert cached_again.status_code == 200 and cached_again.json()["live_calls"] == {"price": 0, "availability": 0}
        assert cached_again.json()["offers"] == [o.model_dump(mode="json") for o in cached.offers]
        health = client.get("/health").json()["upstream"]
        assert health["blocked_until"] is not None and health["calls_last_minute"] == 3
        assert client.get("/v1/stats", headers=OPERATOR_HEADERS).json()["upstream_rate_limits"] == 1
        blocks = [e for e in gateway.audit() if e.action == "UPSTREAM_RATE_LIMITED"]
        assert len(blocks) == 1 and blocks[0].data["retry_after_seconds"] == 30
        route.mock(side_effect=upstream.price_response)
        clock.sleep(25)
        assert client.post("/v1/offers", json=uncached.model_dump(mode="json")).status_code == 200
        assert route.call_count == count + 1 and client.get("/health").json()["upstream"]["blocked_until"] is None
        # A 429 inside the order transaction must still persist one block event.
        quote = gateway.orders.create_quote(quote_request(need, qty=1, need_ref=cached.lookup_id))
        stock_route = respx_mock.post("https://valio.test/api/availability").mock(
            return_value=httpx.Response(429, headers={"Retry-After": "7"}))
        refused = client.post("/v1/orders", json={"quote_id": quote.id}, headers={"Idempotency-Key": "retry-order"})
        assert refused.status_code == 503 and refused.json()["retry_after_seconds"] == 7
        assert len([e for e in gateway.audit() if e.action == "UPSTREAM_RATE_LIMITED"]) == 2
        assert gateway.orders.get_quote(quote.id).status == "OPEN"
        with gateway.db.connect() as conn:
            assert conn.execute("SELECT count(*) FROM orders").fetchone()[0] == 0
        clock.sleep(7)
        stock_route.mock(side_effect=upstream.availability_response)
        assert client.post("/v1/orders", json={"quote_id": quote.id}, headers={"Idempotency-Key": "retry-order"}).status_code == 200
        clock.sleep(60)
        assert client.get("/health").json()["upstream"]["calls_last_minute"] == 0


def test_ac_6_twenty_concurrent_lookups_share_five_per_second(gateway, need, upstream, upstream_pacer, respx_mock):
    barrier = threading.Barrier(20)
    starts = []

    def record(handler):
        def response(request):
            starts.append(upstream_pacer.clock())
            return handler(request)
        return response

    respx_mock.post("https://valio.test/api/price").mock(side_effect=record(upstream.price_response))
    respx_mock.post("https://valio.test/api/availability").mock(side_effect=record(upstream.availability_response))

    def lookup(index):
        barrier.wait(timeout=10)
        return gateway.find_offers(need.model_copy(update={"deliver_by": need.deliver_by + timedelta(days=index)}))

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lookup, range(20)))
    assert len(results) == 20 and sum(r.live_calls.price + r.live_calls.availability for r in results) == 40
    assert len(upstream.price_calls) == len(upstream.availability_calls) == 20
    # A second client also consumes the process bucket, rather than obtaining a fresh burst.
    other = ValioClient("https://valio.test", "test-key")
    try:
        other.fetch("price", "1", need.deliver_by, ["1"], RequestCounter())
    finally:
        other.close()
    assert len(starts) == 41
    assert all(sum(at <= candidate < at + 1 for candidate in starts) <= 5 for at in starts)
    assert all(b - a >= .2 - 1e-9 for a, b in zip(starts, starts[1:]))


def test_ac_7_recorded_demo_464306_1010_eur_confirms(gateway, need, respx_mock):
    result = replay_recorded_demo(gateway, need, respx_mock)
    assert (result.offers[0].product_id, result.offers[0].purchase_total) == ("464306", 1010)
    assert gateway.orders.list_orders()[0].status == "CONFIRMED"
