"""Spec 32 acceptance for the shared basket adapter required by spec 34 R-2."""
import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.baskets import BasketRequest, find_basket_offers
from app.mcp_server import create_server
from app.models import PolicyUpdateRequest, QuoteRequest
from conftest import agent_headers, raw_product, rebuild_catalog


@pytest.fixture
def basket_setup(gateway, need, upstream, tmp_path):
    names = ["alpha", "bravo", "charlie", "delta", "echo"]
    rows = [raw_product(str(i + 1), name, volume=1, multiplier=1) for i, name in enumerate(names)]
    rebuild_catalog(gateway, tmp_path, rows)
    for i in range(1, 6):
        upstream.sizes[str(i)] = 1
        upstream.prices[str(i)] = 13
    return {"customer_id": "1", "deliver_by": str(need.deliver_by), "manifest":
            json.loads((Path(__file__).parent / "fixtures/recipe_manifest.json").read_text())}


def offers(gateway, request):
    return find_basket_offers(gateway, BasketRequest.model_validate(request), "external")


def test_ac_1_five_ingredients_two_supplier_calls(gateway, need, upstream, basket_setup):
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        response = client.post("/v1/basket-lookups", json=basket_setup)
        assert response.status_code == 200, response.text
        result = response.json()
    assert len(result["basket"]["lines"]) == 5
    assert len(result["basket"]["coverage_by_ingredient"]) == 5
    assert result["live_calls"] == {"price": 1, "availability": 1}
    assert len(upstream.price_calls) == len(upstream.availability_calls) == 1
    assert set(upstream.price_calls[0]["products"]) == {"1", "2", "3", "4", "5"}
    tool = asyncio.run(create_server(gateway, "external").call_tool("find_basket_offers", {"request": basket_setup}))
    assert not tool.is_error and tool.structured_content["basket"]["lines"]


def test_ac_2_servings_scale_rounding_overbuy_total_and_policy(gateway, need, upstream, basket_setup):
    policy = gateway.policy._editable(gateway.policy.get())
    policy["approval"]["max_auto_total_eur"] = 100
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Fixture"))
    first = offers(gateway, basket_setup)
    basket_setup["manifest"]["servings"] = 2
    second = offers(gateway, basket_setup)
    assert first["basket"]["total"] == 65 and second["basket"]["total"] == 130
    assert first["basket"]["policy_preview"]["outcome"] == "CONFIRMED"
    assert second["basket"]["policy_preview"]["outcome"] == "PENDING_APPROVAL"
    assert second["scaled_manifest"]["ingredients"][0]["quantity"]["value"] == 1.2
    assert first["basket"]["coverage_by_ingredient"][0]["overbuy"] != second["basket"]["coverage_by_ingredient"][0]["overbuy"]


def test_ac_3_duplicate_skus_share_summed_line(gateway, need, upstream, basket_setup):
    basket_setup["manifest"]["ingredients"] = [basket_setup["manifest"]["ingredients"][0]] * 2
    result = offers(gateway, basket_setup)
    assert len(result["basket"]["lines"]) == 1
    assert result["basket"]["lines"][0]["units_needed"] == 2
    assert len(result["basket"]["coverage_by_ingredient"]) == 2


def test_ac_4_uncovered_requires_approval(gateway, need, upstream, basket_setup):
    basket_setup["manifest"]["ingredients"][0]["search_terms"] = ["absent"]
    result = offers(gateway, basket_setup)
    assert result["basket"]["uncovered"][0]["name"] == "alpha"
    assert result["basket"]["policy_preview"]["outcome"] == "PENDING_APPROVAL"


def test_ac_5_standing_exclusions_apply_to_every_ingredient(gateway, need, upstream, basket_setup):
    policy = gateway.policy._editable(gateway.policy.get())
    policy["customers"]["1"] = {"exclude_allergens": ["AM"]}
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Fixture"))
    result = offers(gateway, basket_setup)
    assert len(result["basket"]["uncovered"]) == 5
    assert not result["basket"]["lines"]
    assert all(not ingredient["offers"] for ingredient in result["ingredients"])


def test_ac_6_basket_quote_order_reserves_all_or_none(gateway, need, upstream, basket_setup, monkeypatch):
    upstream.model, upstream.amount = "Stock", 100
    result = offers(gateway, basket_setup)
    request = QuoteRequest(customer_id="1", deliver_by=need.deliver_by, basket_ref={"basket_lookup_id": result["basket_lookup_id"]})
    quote = gateway.orders.create_quote(request)
    reserve = gateway.orders._reserve
    def fail(conn, order):
        reserve(conn, order)
        raise RuntimeError("simulate reservation transaction failure")
    monkeypatch.setattr(gateway.orders, "_reserve", fail)
    with pytest.raises(RuntimeError):
        gateway.orders.place_order(quote.id, "atomic-basket")
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    monkeypatch.setattr(gateway.orders, "_reserve", reserve)
    order = gateway.orders.place_order(quote.id, "atomic-basket")
    assert len(order.lines) == 5
    assert "all-or-none" in order.context["atomicity"]
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reservations WHERE order_id=?", (order.id,)).fetchone()[0] == 5
    import importlib.util
    spec = importlib.util.spec_from_file_location("recipe_demo", Path(__file__).parents[1] / "demo/run_recipe_order.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        assert demo.journey(client, basket_setup)["status"] == "CONFIRMED"


def test_ac_7_grams_to_litres_without_density_uncovered(gateway, need, upstream, basket_setup):
    basket_setup["manifest"]["ingredients"] = [basket_setup["manifest"]["ingredients"][0]]
    basket_setup["manifest"]["ingredients"][0]["quantity"] = {"value": 500, "unit": "G"}
    result = offers(gateway, basket_setup)
    assert result["basket"]["uncovered"][0]["reason"] == "unit cannot be converted"
    assert not result["basket"]["lines"]
