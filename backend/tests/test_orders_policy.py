import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from app import audit
from app.db import DomainError
from app.lookup import warning
from app.models import Quantity
from conftest import quote_request


def fitted_quote(gateway, need, qty=1):
    result = gateway.find_offers(need)
    return gateway.orders.create_quote(quote_request(need, qty=qty, need_ref=result.lookup_id))


@pytest.mark.parametrize("total,fit,warnings,violations,outcome,rules", [
    (10, 0.95, [], [], "CONFIRMED", set()),
    (1501, 0.95, [], [], "PENDING_APPROVAL", {"max_auto_total_eur"}),
    (10, 0.7, [], [], "PENDING_APPROVAL", {"min_fit_probability"}),
    (2000, 0.7, ["SALES_BLOCK", "DATE_SHIFT"], ["missing claim"], "REJECTED",
     {"max_auto_total_eur", "min_fit_probability", "SALES_BLOCK", "DATE_SHIFT", "hard_constraint_violation"}),
])
def test_policy_all_hits(gateway, need, upstream, total, fit, warnings, violations, outcome, rules):
    offer = gateway.find_offers(need).offers[0]
    offer.fit = fit
    offer.warnings = [warning(c) for c in warnings]
    result = gateway.policy.evaluate(total, [offer], violations)
    assert result.outcome == outcome
    assert {h.rule for h in result.hits} == rules


@pytest.mark.parametrize("registered_agents", [("buyer",)], indirect=True)
def test_idempotency_concurrent_retries_and_fixed_price(gateway, need, upstream):
    quote = fitted_quote(gateway, need)
    upstream.price = 50
    with ThreadPoolExecutor(max_workers=4) as pool:
        orders = list(pool.map(lambda _: gateway.orders.place_order(quote.id, "same-key", "buyer"), range(4)))
    assert len({o.id for o in orders}) == 1
    assert orders[0].total == 13
    assert orders[0].status == "CONFIRMED"
    assert orders[0].live_calls.model_dump() == {"price": 0, "availability": 1}
    assert len(upstream.availability_calls) == 3  # lookup, quote, one order
    assert len([a for a in gateway.audit() if a.action == "PLACE_ORDER"]) == 1
    with pytest.raises(DomainError, match="another quote or agent"):
        gateway.orders.place_order(quote.id, "same-key", "different-agent")
    with pytest.raises(DomainError) as error:
        gateway.orders.place_order(quote.id, "different-key", "buyer")
    assert error.value.code == "QUOTE_ALREADY_ACCEPTED"


def test_quote_live_recheck_and_fit_carry(gateway, need, upstream):
    result = gateway.find_offers(need)
    quote = gateway.orders.create_quote(quote_request(need, need_ref=result.lookup_id))
    assert quote.lines[0].fit == 0.91
    assert quote.live_calls.price == quote.live_calls.availability == 1
    assert len(upstream.price_calls) == len(upstream.availability_calls) == 2
    assert quote.expires_at <= quote.created_at + timedelta(minutes=30)
    assert gateway.orders.get_quote(quote.id) == quote


@pytest.mark.parametrize("registered_agents", [("external",)], indirect=True)
def test_expired_quote_persists_and_audits(gateway, need, upstream, monkeypatch):
    quote = fitted_quote(gateway, need)
    monkeypatch.setattr(audit, "utcnow", lambda: quote.expires_at + timedelta(seconds=1))
    with pytest.raises(DomainError) as error:
        gateway.orders.place_order(quote.id, "expired")
    assert error.value.code == "EXPIRED_QUOTE"
    assert gateway.orders.get_quote(quote.id).status == "EXPIRED"
    assert {a.action for a in gateway.audit(quote_id=quote.id)} >= {"QUOTE_EXPIRED", "PLACE_ORDER_FAILED"}


@pytest.mark.parametrize("approve,status", [(True, "CONFIRMED"), (False, "REJECTED")])
@pytest.mark.parametrize("registered_agents", [("external",)], indirect=True)
def test_approval_transitions(gateway, need, upstream, approve, status):
    quote = fitted_quote(gateway, need, qty=200)
    order = gateway.orders.place_order(quote.id, "review")
    assert order.status == "PENDING_APPROVAL"
    method = gateway.orders.approve if approve else gateway.orders.reject
    reviewed = method(order.id, "Ada", "Reviewed the risk")
    assert reviewed.status == status
    assert reviewed.approver == "Ada"
    assert len(gateway.orders.list_orders(True)) == 0
    with pytest.raises(DomainError) as error:
        method(order.id, "Ada")
    assert error.value.code == "INVALID_TRANSITION"
    assert gateway.audit(order_id=order.id)[-2].actor == "human:Ada"


@pytest.mark.parametrize("registered_agents", [("external",)], indirect=True)
def test_stock_reservations_and_release(gateway, need, upstream):
    upstream.model, upstream.amount = "Stock", 10
    quote = fitted_quote(gateway, need, qty=6)
    first = gateway.orders.place_order(quote.id, "reserve")
    assert first.status == "CONFIRMED"
    quote2 = fitted_quote(gateway, need, qty=6)
    second = gateway.orders.place_order(quote2.id, "reserve2")
    assert second.status == "PENDING_APPROVAL"
    assert second.lines[0].availability.available_amount == 4
    gateway.orders.reject(second.id, "Ada")
    gateway.orders.cancel(first.id)
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0


@pytest.mark.parametrize("registered_agents", [("external",)], indirect=True)
def test_pending_expiry_releases_stock(gateway, need, upstream, monkeypatch):
    upstream.model = "Stock"
    quote = fitted_quote(gateway, need, qty=200)
    order = gateway.orders.place_order(quote.id, "pending")
    monkeypatch.setattr(audit, "utcnow", lambda: order.expires_at + timedelta(seconds=1))
    assert gateway.orders.get_order(order.id).status == "EXPIRED"
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0
    assert gateway.orders.place_order(quote.id, "pending").id == order.id


@pytest.mark.parametrize("registered_agents", [("external",)], indirect=True)
def test_no_fit_needs_approval_and_no_override_of_sale_block(gateway, need, upstream):
    gateway.decisions.key = ""
    lookup = gateway.find_offers(need)
    quote = gateway.orders.create_quote(quote_request(need, qty=1, need_ref=lookup.lookup_id))
    order = gateway.orders.place_order(quote.id, "no-fit")
    assert order.status == "PENDING_APPROVAL"
    upstream.sales_block = True
    with pytest.raises(DomainError) as error:
        gateway.orders.approve(order.id, "Ada")
    assert error.value.code == "HARD_CONSTRAINT_VIOLATION"
    assert gateway.orders.get_order(order.id).status == "PENDING_APPROVAL"


@pytest.mark.parametrize("registered_agents", [("external",)], indirect=True)
def test_hard_constraint_violation_rejected(gateway, need, upstream):
    result = gateway.find_offers(need)
    quote = gateway.orders.create_quote(quote_request(need, qty=1, need_ref=result.lookup_id, product_id="2"))
    order = gateway.orders.place_order(quote.id, "hard")
    assert order.status == "REJECTED"
    assert "hard_constraint_violation" in {h.rule for h in order.policy.hits}


@pytest.mark.parametrize("registered_agents", [("external",)], indirect=True)
def test_unknown_upstream_rejected_and_bad_price_cannot_quote(gateway, need, upstream):
    upstream.unknown = True
    lookup = gateway.find_offers(need)
    quote = gateway.orders.create_quote(quote_request(need, qty=1, need_ref=lookup.lookup_id))
    assert gateway.orders.place_order(quote.id, "unknown").status == "REJECTED"
    upstream.price_error = True
    with pytest.raises(DomainError) as error:
        gateway.orders.create_quote(quote_request(need, qty=1, need_ref=lookup.lookup_id))
    assert error.value.code == "PRICE_ERROR"
    assert gateway.audit()[-1].data["live_calls"] == {"price": 1, "availability": 1}


def test_audit_is_append_only(gateway, need, upstream):
    fitted_quote(gateway, need)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with gateway.db.connect(write=True) as conn:
            conn.execute("DELETE FROM audit")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with gateway.db.connect(write=True) as conn:
            conn.execute("UPDATE audit SET actor='changed'")


@pytest.mark.parametrize("registered_agents", [("external",)], indirect=True)
def test_concurrent_stock_orders_do_not_both_auto_confirm(gateway, need, upstream):
    upstream.model, upstream.amount = "Stock", 10
    first_quote = fitted_quote(gateway, need, qty=6)
    second_quote = fitted_quote(gateway, need, qty=6)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(gateway.orders.place_order, q.id, f"stock-{i}") for i, q in enumerate([first_quote, second_quote])]
        orders = [f.result() for f in futures]
    assert sorted(o.status for o in orders) == ["CONFIRMED", "PENDING_APPROVAL"]
    assert sorted(o.lines[0].availability.available_amount for o in orders) == [4, 10]


@pytest.mark.parametrize("price_minutes,cutoff_minutes,expected_minutes", [(5, 10, 5), (10, 5, 5), (60, 60, 30)])
def test_quote_expiry_earliest_bound(gateway, need, upstream, respx_mock, price_minutes, cutoff_minutes, expected_minutes):
    now = audit.utcnow()
    upstream.cutoff = now + timedelta(minutes=cutoff_minutes)

    def price_response(request):
        response = upstream.price_response(request)
        body = response.json()
        for p in body["products"]:
            p["catalogPrice"]["validTo"] = (now + timedelta(minutes=price_minutes)).isoformat()
        import httpx
        return httpx.Response(200, json=body)

    respx_mock.post("https://valio.test/api/price").mock(side_effect=price_response)
    lookup = gateway.find_offers(need)
    quote = gateway.orders.create_quote(quote_request(need, qty=1, need_ref=lookup.lookup_id))
    assert abs((quote.expires_at - (now + timedelta(minutes=expected_minutes))).total_seconds()) < 1


@pytest.mark.parametrize("registered_agents", [("external",)], indirect=True)
def test_stock_recheck_changes_policy_after_quote(gateway, need, upstream):
    quote = fitted_quote(gateway, need, qty=10)
    upstream.amount = 2
    order = gateway.orders.place_order(quote.id, "changed-stock")
    assert order.status == "PENDING_APPROVAL"
    assert "PARTIAL_STOCK" in {h.rule for h in order.policy.hits}
    assert order.lines[0].availability.coverage == 0.2
    assert order.total == quote.total


@pytest.mark.parametrize("registered_agents", [("external",)], indirect=True)
def test_multiline_quote_sums_and_reserves_each_line(gateway, need, upstream):
    from app.models import QuoteLineRequest
    upstream.model = "Stock"
    result = gateway.find_offers(need)
    request = quote_request(need, qty=2, need_ref=result.lookup_id)
    request.lines.append(QuoteLineRequest(product_id="4", quantity_sales_units=3))
    quote = gateway.orders.create_quote(request)
    assert quote.total == 65
    assert upstream.price_calls[-1]["products"] == ["1", "4"]
    order = gateway.orders.place_order(quote.id, "multiline")
    assert order.status == "CONFIRMED"
    with gateway.db.connect() as conn:
        assert {r["product_id"]: r["quantity"] for r in conn.execute("SELECT * FROM reservations")} == {"1": 2, "4": 3}


def test_quote_without_known_price_fails_audited(gateway, need, upstream, respx_mock):
    lookup = gateway.find_offers(need)
    respx_mock.post("https://valio.test/api/price").respond(503)
    with pytest.raises(DomainError) as error:
        gateway.orders.create_quote(quote_request(need, qty=1, need_ref=lookup.lookup_id))
    assert error.value.code == "VALIO_UNAVAILABLE"
    assert gateway.audit()[-1].data["live_calls"] == {"price": 1, "availability": 0}
