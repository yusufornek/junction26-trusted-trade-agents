import pytest

from app.lookup import rank
from app.models import Quantity
from conftest import demo_catalog, raw_product, rebuild_catalog


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


def test_ac_4_mocked_demo_best_is_464306_at_1010(gateway, need, upstream, tmp_path):
    demo_catalog(gateway, tmp_path, upstream)
    result = gateway.find_offers(need, actor="agent:web-console")
    best = result.offers[0]
    assert best.product_id == "464306" and best.rank_label == "best"
    assert best.purchase_total == 1010 and best.overbuy.amount == 0
    assert result.plans[0].total == 1010
