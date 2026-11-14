import pytest

from app.lookup import rank
from app.models import Quantity
from conftest import replay_recorded_demo, raw_product, rebuild_catalog


@pytest.mark.parametrize("count,ml,requested,size,units", [(12, 290, 34.8, 3.48, 10), (18, 300, 16.2, 5.4, 3)])
@pytest.mark.parametrize("source", ["catalog", "name_multiplier", "name_count"])
def test_review_decimal_multipack(gateway, need, upstream, tmp_path, count, ml, requested, size, units, source):
    row = raw_product("pack", f"Milk {count}x{ml}ml" if source == "name_count" else f"Milk {ml}ml",
                      volume=ml / 1000 if source == "catalog" else None,
                      multiplier=1 if source == "name_count" else count)
    if source == "catalog":
        row["synkkaData"]["unitConversions"][0]["volume"] = {"value": ml, "unit": "MLT"}
    rebuild_catalog(gateway, tmp_path, [row, raw_product("egg", "Egg vocabulary", egg=True)])
    upstream.prices["pack"], upstream.sizes["pack"] = 10, size
    result = gateway.find_offers(need.model_copy(update={"quantity": Quantity(value=requested, unit="L")}))
    offer = result.offers[0]
    assert (offer.size_per_sales_unit, offer.units_needed, offer.purchase_total, offer.overbuy.amount) == (size, units, units * 10, 0)
    assert result.plans[0].lines[0].units == units
    assert result.plans[0].uncovered.value == 0
    from app.models import QuoteRequest
    quote = gateway.orders.create_quote(QuoteRequest(customer_id=need.customer_id, deliver_by=need.deliver_by,
        plan_ref={"lookup_id": result.lookup_id, "plan_index": 0}), "external")
    assert (quote.lines[0].units_needed, quote.total) == (units, units * 10)


def test_review_ac4_uses_real_catalog(gateway, need, upstream, tmp_path, respx_mock, monkeypatch):
    import app.catalog as catalog
    original = catalog.extract

    def remove_eligibility(*args, **kwargs):
        product = original(*args, **kwargs)
        if product.gtin == "6408430000487":
            product.claims = []
        return product

    monkeypatch.setattr(catalog, "extract", remove_eligibility)
    with pytest.raises(AssertionError):
        test_ac_4_mocked_demo_best_is_464306_at_1010(gateway, need, upstream, tmp_path, respx_mock)


def test_review_unknown_sizes_compare_unit_prices(gateway, need, upstream, respx_mock, tmp_path):
    import httpx
    rebuild_catalog(gateway, tmp_path, [raw_product("a", "Milk unknown"), raw_product("b", "Milk unknown"),
                                      raw_product("egg", "Egg vocabulary", egg=True)])
    upstream.prices = {"a": 20, "b": 10}
    def no_comparison(request):
        body = upstream.price_response(request).json()
        for row in body["products"]:
            row.pop("comparisonPrice")
        return httpx.Response(200, json=body)
    respx_mock.post("https://valio.test/api/price").mock(side_effect=no_comparison)
    result = gateway.find_offers(need)
    rank(result.offers, {k: int(k == "price") for k in gateway.policy.weights})
    assert [o.product_id for o in result.offers] == ["b", "a"]
    assert [o.score_breakdown["price"] for o in result.offers] == [1, 0]
    assert all(o.rank_label is None and "SIZE_UNKNOWN" in {w.code for w in o.warnings} for o in result.offers)


def total_case(gateway, need, upstream, tmp_path, *, amount=1001, pack_size=5, pack_price=4.975):
    rebuild_catalog(gateway, tmp_path, [raw_product("a", "Milk 1 L", volume=1, multiplier=1),
                                      raw_product("b", f"Milk {pack_size} L", volume=pack_size, multiplier=1),
                                      raw_product("egg", "Egg vocabulary", egg=True)])
    upstream.prices = {"a": 1, "b": pack_price}
    upstream.sizes = {"a": 1, "b": pack_size}
    result = gateway.find_offers(need.model_copy(update={"quantity": Quantity(value=amount, unit="L")}))
    return {o.product_id: o for o in result.offers}


def test_review_exact_decimal_packs(gateway, need, upstream, tmp_path):
    offer = total_case(gateway, need, upstream, tmp_path, amount=2.1, pack_size=0.3, pack_price=1)["b"]
    assert (offer.units_needed, offer.purchase_total, offer.overbuy.amount) == (7, 7, 0)
    assert "7.00 EUR" in " ".join(offer.matched_because)
    assert "overbuy: 0 L" in " ".join(offer.matched_because)


def test_ac_1_purchase_totals_and_overbuy_use_whole_packs(gateway, need, upstream, tmp_path):
    offers = total_case(gateway, need, upstream, tmp_path)
    a, b = offers["a"], offers["b"]
    assert (a.units_needed, a.purchase_total, a.overbuy.amount) == (1001, 1001, 0)
    assert (b.units_needed, b.purchase_total, b.overbuy.amount) == (201, 999.98, 4)
    assert b.overbuy.unit == "L" and b.unit_price == 4.975
    assert b.score_breakdown["price"] > a.score_breakdown["price"]
    assert "999.98 EUR" in " ".join(b.matched_because) and "overbuy: 4 L" in " ".join(b.matched_because)


def test_ac_2_price_ranking_follows_total_when_per_litre_order_differs(gateway, need, upstream, tmp_path):
    offers = total_case(gateway, need, upstream, tmp_path, amount=11, pack_size=10, pack_price=9)
    a, b = offers["a"], offers["b"]
    assert b.price_per_l_or_kg < a.price_per_l_or_kg
    assert a.purchase_total == 11 < b.purchase_total == 18
    ranked = [b, a]
    rank(ranked, {k: int(k == "price") for k in gateway.policy.weights})
    assert ranked[0] == a and a.score_breakdown["price"] == 1 and b.score_breakdown["price"] == 0


def test_ac_3_score_breakdown_sums_to_score_and_repeated_ranking_is_stable(gateway, need, upstream):
    result = gateway.find_offers(need)
    for offer in result.offers:
        assert set(offer.score_breakdown) == {"price", "coverage", "reliability", "co2", "fit"}
        assert sum(offer.score_breakdown.values()) == pytest.approx(offer.score)
    before = [(o.product_id, o.score, o.rank_label) for o in result.offers]
    rank(result.offers, gateway.policy.weights, gateway.policy.get().approval.min_fit_probability,
         catalog_stats=gateway.catalog.metadata("ranking"))
    assert [(o.product_id, o.score, o.rank_label) for o in result.offers] == before


def test_ac_4_mocked_demo_best_is_464306_at_1010(gateway, need, upstream, tmp_path, respx_mock):
    result = replay_recorded_demo(gateway, need, respx_mock)
    best = result.offers[0]
    assert best.product_id == "464306" and best.rank_label == "best"
    assert best.purchase_total == 1010 and best.overbuy.amount == 0
    assert result.plans[0].total == 1010
