"""Spec 32 acceptance criteria, including transport parity and atomic reservation failure."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import pytest

from app import audit
from app.api import create_app
from app.auth import Credentials
from app.baskets import BasketRequest, find_basket_offers
from app.db import DomainError
from app.mcp_server import create_server
from app.models import BasketLookupRequest, CustomerConstraints, PolicyUpdateRequest, QuoteRequest
from app.sandbox import scenario_gateway
from conftest import agent_headers, raw_product, rebuild_catalog

MANIFEST = json.loads((Path(__file__).parent / "fixtures/lasagne_manifest.json").read_text())


@pytest.fixture
def recipe():
    with scenario_gateway("full_stock") as gateway:
        policy = gateway.policy.get()
        policy.customers = {"recipe": CustomerConstraints(exclude_allergens=["AM"])}
        policy.approval.max_auto_total_eur = 200
        gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Fixture buyer"))
        yield gateway


def request(manifest=None, servings=None):
    return BasketLookupRequest(customer_id="recipe", deliver_by=audit.utcnow().date() + timedelta(days=3),
                               manifest=manifest or deepcopy(MANIFEST), servings=servings)


def find(g, manifest=None, servings=None):
    return g.baskets.find_basket_offers(request(manifest, servings), "agent:sandbox-agent")


def quote(g, result):
    return g.orders.create_quote(QuoteRequest(customer_id="recipe", deliver_by=result.deliver_by,
        basket_ref={"basket_lookup_id": result.basket_lookup_id}), "sandbox-agent")


def test_AC_1_five_ingredients_one_basket_two_supplier_calls_and_transport_parity(recipe):
    keys = Credentials("operator-test", "sandbox-agent:recipe-test", environment="sandbox")
    with TestClient(create_app(recipe, keys=keys), headers={"Authorization": "Bearer recipe-test"}) as client:
        response = client.post("/v1/basket-lookups", json=request().model_dump(mode="json"))
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["basket"]["lines"]) == 5
        assert all(c["covered"] == 1 for c in body["basket"]["coverage_by_ingredient"])
        assert body["basket"]["uncovered"] == []
        assert body["live_calls"] == {"price": 1, "availability": 1}
        assert len(recipe.valio.calls) == 2
        for endpoint, ids in recipe.valio.calls:
            assert len(ids) == len(set(ids)) == 5
        saved = client.get("/v1/basket-lookups/" + body["basket_lookup_id"])
        assert saved.status_code == 200 and saved.json() == body
    async def mcp():
        server = create_server(recipe, "sandbox-agent")
        result = await server.call_tool("find_basket_offers", {"request": request().model_dump(mode="json")})
        assert not result.is_error
        q = await server.call_tool("create_quote", {"customer_id": "recipe", "deliver_by": str(request().deliver_by),
            "basket_ref": {"basket_lookup_id": body["basket_lookup_id"]}})
        assert not q.is_error
    asyncio.run(mcp())


def test_remote_mcp_basket_purchase_uses_authenticated_owner(recipe):
    keys = Credentials("operator-test", "sandbox-agent:recipe-test,outsider:other-test", environment="sandbox")
    headers = {"Authorization": "Bearer recipe-test", "Accept": "application/json, text/event-stream"}
    with TestClient(create_app(recipe, keys=keys), base_url="http://localhost", headers=headers) as client:
        def call(name, arguments, **kwargs):
            response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": arguments}}, **kwargs)
            assert response.status_code == 200, response.text
            return response.json()["result"]

        result = call("find_basket_offers", {"request": request().model_dump(mode="json")})
        assert not result.get("isError"), result
        basket = result["structuredContent"]
        assert basket["agent_id"] == "sandbox-agent"
        quote_args = {"customer_id": "recipe", "deliver_by": str(request().deliver_by),
            "basket_ref": {"basket_lookup_id": basket["basket_lookup_id"]}, "idempotency_key": "remote-basket-quote"}
        result = call("create_quote", quote_args)
        assert not result.get("isError"), result
        quoted = result["structuredContent"]
        assert quoted["agent_id"] == "sandbox-agent"
        assert quoted["basket_ref"]["basket_lookup_id"] == basket["basket_lookup_id"]
        assert call("create_quote", quote_args)["structuredContent"]["id"] == quoted["id"]
        result = call("place_order", {"quote_id": quoted["id"], "idempotency_key": "remote-basket-order"})
        assert not result.get("isError"), result
        order = result["structuredContent"]
        assert order["status"] == "CONFIRMED" and order["agent_id"] == "sandbox-agent"
        receipt = call("get_order_receipt", {"order_id": order["id"]})
        assert not receipt.get("isError"), receipt
        assert receipt["structuredContent"]["receipt"]
        refused = call("get_order_receipt", {"order_id": order["id"]},
                       headers={"Authorization": "Bearer other-test"})
        assert refused["isError"] and refused["structuredContent"]["code"] == "ORDER_NOT_FOUND"


def test_basket_command_replay_rechecks_authority_without_supplier_calls(recipe):
    body = request()
    first = recipe.baskets.find_basket_offers(body, "agent:sandbox-agent", command_key="basket-lookup")
    calls = list(recipe.valio.calls)
    replay = recipe.baskets.find_basket_offers(body, "agent:sandbox-agent", command_key="basket-lookup")
    assert replay.basket_lookup_id == first.basket_lookup_id
    assert recipe.valio.calls == calls
    with pytest.raises(DomainError) as conflict:
        recipe.baskets.find_basket_offers(request(servings=200), "agent:sandbox-agent", command_key="basket-lookup")
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    policy = recipe.policy.get()
    del policy.agents["sandbox-agent"]
    recipe.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Fixture buyer"))
    with pytest.raises(DomainError) as refused:
        recipe.baskets.find_basket_offers(body, "agent:sandbox-agent", command_key="basket-lookup")
    assert refused.value.code == "AGENT_NOT_REGISTERED"
    assert recipe.valio.calls == calls


def test_AC_2_servings_scale_rounding_overbuy_total_and_policy(recipe):
    base = find(recipe)
    doubled = find(recipe, servings=200)
    assert doubled.scaled_manifest.servings == 200
    assert doubled.scaled_manifest.ingredients[0].quantity.value == 2.2
    assert base.basket.total < doubled.basket.total
    assert base.basket.lines[0].units_needed < doubled.basket.lines[0].units_needed
    assert base.basket.lines[0].overbuy.amount != doubled.basket.lines[0].overbuy.amount
    assert base.basket.policy_preview.outcome == "CONFIRMED"
    assert doubled.basket.policy_preview.outcome == "PENDING_APPROVAL"


def test_AC_3_shared_sku_sums_quantities_before_pack_rounding(recipe):
    manifest = deepcopy(MANIFEST)
    manifest["ingredients"] = [dict(name="Sauce oat", quantity={"value": .4, "unit": "L"},
        search_terms=["oat"], substitutions_allowed=True), dict(name="Topping oat", quantity={"value": .4, "unit": "L"},
        search_terms=["oat"], substitutions_allowed=False)]
    result = find(recipe, manifest)
    assert len(result.basket.lines) == 1
    assert result.basket.lines[0].units_needed == 1
    assert result.basket.lines[0].overbuy.amount == pytest.approx(.2)
    assert [r.chosen_line for r in result.ingredients] == ["oat", "oat"]
    assert all(c.covered == 1 for c in result.basket.coverage_by_ingredient)
    _check_exact_shared_demand(recipe, False)
    _check_exact_shared_demand(recipe, True)


def test_AC_4_no_compliant_product_is_uncovered_partial_basket_needs_approval(recipe):
    manifest = deepcopy(MANIFEST)
    manifest["ingredients"][0]["search_terms"] = ["unobtainable"]
    result = find(recipe, manifest)
    assert result.basket.uncovered[0].ingredient_index == 0
    assert result.basket.uncovered[0].reason == "no compliant product"
    assert result.basket.policy_preview.outcome == "PENDING_APPROVAL"
    order = recipe.orders.place_order(quote(recipe, result).id, "partial-basket", "sandbox-agent")
    assert order.status == "PENDING_APPROVAL"
    assert any(w.code == "PARTIAL_BASKET" for w in order.warnings)


def test_AC_5_standing_allergen_exclusion_applies_to_every_ingredient(recipe):
    manifest = deepcopy(MANIFEST)
    for ingredient in manifest["ingredients"]:
        ingredient["search_terms"] = ["oat"]
        ingredient["quantity"] = {"value": 1.0, "unit": "L"}
    result = find(recipe, manifest)
    assert len(result.basket.lines) == 1
    for ingredient in result.ingredients:
        assert [o.product_id for o in ingredient.offers] == ["oat"]
        assert any("AM" in k and "customer_policy" in k and "contains" in k for k in ingredient.exclusion_counts)


def test_AC_6_basket_quote_order_reserves_all_lines_or_none(recipe):
    result = find(recipe)
    q = quote(recipe, result)
    confirmed = recipe.orders.place_order(q.id, "all-lines", "sandbox-agent")
    assert confirmed.status == "CONFIRMED" and "all-or-none" in confirmed.atomicity
    with recipe.db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM reservations WHERE order_id=?", (confirmed.id,)).fetchone()[0] == 5
    second = quote(recipe, find(recipe))
    policy = recipe.policy.get()
    policy.approval.warnings_need_approval = [w for w in policy.approval.warnings_need_approval if w != "PARTIAL_STOCK"]
    policy.approval.ignored_warnings.append("PARTIAL_STOCK")
    recipe.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Basket atomicity despite ignored warning"))
    recipe.valio.rows["tomato"]["stock"] = 0
    pending = recipe.orders.place_order(second.id, "no-lines", "sandbox-agent")
    assert pending.status == "PENDING_APPROVAL"
    with recipe.db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM reservations WHERE order_id=?", (pending.id,)).fetchone()[0] == 0
    with pytest.raises(DomainError, match="All basket lines"):
        recipe.orders.approve(pending.id, "Buyer")
    recipe.valio.rows["tomato"]["stock"] = 10000
    third = quote(recipe, find(recipe))
    with recipe.db.connect(write=True) as conn:
        conn.execute("CREATE TRIGGER fail_basket BEFORE INSERT ON reservations WHEN NEW.product_id='tomato' "
                     "BEGIN SELECT RAISE(ABORT,'controlled reservation failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        recipe.orders.place_order(third.id, "rollback-lines", "sandbox-agent")
    with recipe.db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM orders WHERE quote_id=?", (third.id,)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM reservations").fetchone()[0] == 5
    assert recipe.orders.get_quote(third.id).status == "OPEN"


def test_AC_7_grams_to_litres_without_density_is_uncovered(recipe):
    manifest = deepcopy(MANIFEST)
    manifest["ingredients"] = [dict(name="Oat drink", search_terms=["oat"],
        quantity={"value": 1500, "unit": "G"}, substitutions_allowed=True)]
    result = find(recipe, manifest)
    assert result.basket.lines == []
    assert result.basket.uncovered[0].reason == "unit cannot be converted"
    manifest["ingredients"][0]["density_kg_per_l"] = 1.5
    proven = find(recipe, manifest)
    assert proven.basket.uncovered == []
    assert proven.basket.lines[0].units_needed == 1
    assert proven.line_needs["oat"].quantity.unit == "L"


def test_basket_bound_constraints_cannot_be_bypassed_at_order(recipe):
    result = find(recipe)
    q = quote(recipe, result)
    with recipe.db.connect(write=True) as conn:
        product = recipe.catalog.get("oat")
        product.allergen_status = {"AM": ["contains"]}
        conn.execute("UPDATE products SET data=? WHERE id='oat'", (product.model_dump_json(),))
    assert recipe.orders.place_order(q.id, "changed-allergen", "sandbox-agent").status == "REJECTED"


def test_catalog_density_evidence_allows_mass_volume_conversion(recipe):
    with recipe.db.connect(write=True) as conn:
        product = recipe.catalog.get("oat")
        product.density_kg_per_l = 1.5
        conn.execute("UPDATE products SET data=? WHERE id='oat'", (product.model_dump_json(),))
    manifest = deepcopy(MANIFEST)
    manifest["ingredients"] = [dict(name="Oat drink", search_terms=["oat"],
        quantity={"value": 1500, "unit": "G"}, substitutions_allowed=True)]
    result = find(recipe, manifest)
    assert not result.basket.uncovered
    assert result.basket.lines[0].units_needed == 1


def test_pending_basket_stock_change_revises_evidence_and_releases_all_lines(recipe):
    result = find(recipe, servings=200)
    pending = recipe.orders.place_order(quote(recipe, result).id, "pending-change", "sandbox-agent")
    assert pending.status == "PENDING_APPROVAL"
    recipe.valio.rows["tomato"]["stock"] = 0
    with pytest.raises(DomainError) as caught:
        recipe.orders.approve(pending.id, "Buyer")
    assert caught.value.code == "EVIDENCE_CHANGED"
    refreshed = recipe.orders.get_order(pending.id)
    assert refreshed.evidence_revision == 2
    with recipe.db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM reservations WHERE order_id=?", (pending.id,)).fetchone()[0] == 0


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


def test_explicit_base_servings_round_trip_and_request_override(gateway, need, upstream, basket_setup):
    basket_setup["manifest"]["servings"] = 2
    basket_setup["servings"] = 3
    result = offers(gateway, basket_setup)
    manifest = result["scaled_manifest"]
    assert manifest["servings"] == manifest["base_servings"] == 3
    assert manifest["ingredients"][0]["quantity"]["value"] == pytest.approx(1.8)
    repeated = offers(gateway, {"customer_id": "1", "deliver_by": str(need.deliver_by), "manifest": manifest})
    assert repeated["scaled_manifest"] == manifest
    assert repeated["basket"]["total"] == result["basket"]["total"]


@pytest.mark.parametrize("density", [False, True])
def test_review_shared_sku_exact_demand_rounds_once(recipe, density):
    _check_exact_shared_demand(recipe, density)


def _check_exact_shared_demand(recipe, density):
    manifest = deepcopy(MANIFEST)
    if density:
        manifest["ingredients"] = [dict(name="Oat", search_terms=["oat"], substitutions_allowed=True,
            density_kg_per_l=1.2, quantity={"value": value, "unit": "KG"}) for value in (.4, .8)]
        pid = "oat"
    else:
        manifest["ingredients"] = [dict(name="Pasta", search_terms=["pasta"], substitutions_allowed=True,
            quantity={"value": .0625, "unit": "KG"}) for _ in range(24)]
        pid = "pasta"
        with recipe.db.connect(write=True) as conn:
            product = recipe.catalog.get(pid)
            product.sales_net_weight_kg = 1.5
            product.base_net_weight_kg = 1.5
            product.units[product.sales_unit] = 1
            conn.execute("UPDATE products SET data=? WHERE id=?", (product.model_dump_json(), pid))
        recipe.valio.rows[pid]["size"] = 1.5
    result = find(recipe, manifest)
    assert len(result.basket.lines) == 1
    assert result.basket.lines[0].units_needed == 1
    order = recipe.orders.place_order(quote(recipe, result).id, f"exact-packs:{density}", "sandbox-agent")
    assert order.lines[0].units_needed == 1
    assert order.total == result.basket.lines[0].unit_price


def test_review_ingredient_coverage_uses_unrounded_demand(recipe):
    recipe.valio.rows["oat"]["stock"] = 1
    manifest = deepcopy(MANIFEST)
    manifest["ingredients"] = [dict(name="Oat", search_terms=["oat"], substitutions_allowed=True,
        quantity={"value": value, "unit": "L"}) for value in (.4, .7)]
    result = find(recipe, manifest)
    assert result.basket.lines[0].availability.coverage == .5
    assert sum(c.quantity.value * c.coverage for c in result.basket.coverage_by_ingredient) == pytest.approx(1)
    assert all(c.coverage == pytest.approx(1 / 1.1) for c in result.basket.coverage_by_ingredient)


def test_review_basket_shortage_cannot_confirm_when_warning_ignored(recipe):
    q = quote(recipe, find(recipe))
    policy = recipe.policy.get()
    policy.approval.warnings_need_approval = [w for w in policy.approval.warnings_need_approval if w != "PARTIAL_STOCK"]
    policy.approval.ignored_warnings.append("PARTIAL_STOCK")
    recipe.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Ignore stock warning"))
    recipe.valio.rows["tomato"]["stock"] = 0
    order = recipe.orders.place_order(q.id, "shortage-ignored", "sandbox-agent")
    assert order.status == "PENDING_APPROVAL"
    with recipe.db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM reservations WHERE order_id=?", (order.id,)).fetchone()[0] == 0


def test_review_basket_unknown_stock_keeps_spec39_exception(recipe):
    recipe.valio.rows["tomato"]["stock"] = 999999
    order = recipe.orders.place_order(quote(recipe, find(recipe)).id, "unknown-stock", "sandbox-agent")
    assert order.status == "CONFIRMED"
    with recipe.db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM reservations WHERE order_id=?", (order.id,)).fetchone()[0] == 4
