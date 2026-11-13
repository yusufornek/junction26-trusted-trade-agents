from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.api import create_app
from conftest import quote_request


def product_size(gateway, *, name, multiplier, volume=None, weight=None):
    product = gateway.catalog.get("1").model_copy(update={
        "name_en": name, "name_fi": name, "units": {"KI": multiplier},
        "base_volume_l": volume, "base_net_weight_kg": weight,
    })
    with gateway.db.connect(write=True) as conn:
        conn.execute("UPDATE products SET data=? WHERE id='1'", (product.model_dump_json(),))


def first_offer(gateway, need):
    return next(o for o in gateway.find_offers(need).offers if o.product_id == "1")


@pytest.mark.parametrize("ratio", [4.994, 4.85, 5.15])
def test_size_ac1_name_exact_confirmed_200_units(gateway, need, upstream, ratio):
    product_size(gateway, name="Milk 5 x carton 1 L", multiplier=5)
    upstream.price = ratio * 2.6
    upstream.amount = 100
    offer = first_offer(gateway, need)
    assert offer.size_per_sales_unit == 5.0
    assert offer.size_source == "name_parse"
    assert offer.size_confirmed_by_price is True
    assert offer.units_needed == 200
    assert offer.total == round(upstream.price * 200, 2)
    assert offer.price_per_l_or_kg == pytest.approx(upstream.price / 5)
    assert offer.availability.coverage == 0.5
    assert "SIZE_MISMATCH" not in {w.code for w in offer.warnings}


def test_size_ac2_novobox_exact_50_units(gateway, need, upstream):
    product_size(gateway, name="Milk 10 l novobox", multiplier=2)
    upstream.price = 19.94 * 2.6
    offer = first_offer(gateway, need)
    assert offer.size_per_sales_unit == 20.0
    assert offer.size_source == "name_parse"
    assert offer.size_confirmed_by_price is True
    assert offer.units_needed == 50


@pytest.mark.parametrize("name", ["Milk 1 L", "Milk 1.02 L"])
@pytest.mark.parametrize("unit,volume,weight", [("L", 1, None), ("KG", None, 1)])
def test_size_ac3_catalog_beats_name(gateway, need, upstream, respx_mock, name, unit, volume, weight):
    product_size(gateway, name=name if unit == "L" else name.replace("L", "kg"),
                 multiplier=12, volume=volume, weight=weight)
    need.quantity.unit = unit
    upstream.price = 11.98 * 2.6

    def response(request):
        import httpx
        body = upstream.price_response(request).json()
        for product in body["products"]:
            product["comparisonUnit"] = unit
        return httpx.Response(200, json=body)

    respx_mock.post("https://valio.test/api/price").mock(side_effect=response)
    offer = first_offer(gateway, need)
    assert offer.size_per_sales_unit == 12.0
    assert offer.size_source == "catalog"
    assert offer.size_confirmed_by_price is True
    assert offer.units_needed == 84


def test_size_conflicting_exact_sources_resolved_by_price(gateway, need, upstream):
    product_size(gateway, name="Milk 0.5 L", multiplier=12, volume=1)
    upstream.price = 5.99 * 2.6
    offer = first_offer(gateway, need)
    assert offer.size_per_sales_unit == 6.0
    assert offer.size_source == "name_parse"
    assert offer.size_confirmed_by_price is True
    assert "SIZE_MISMATCH" not in {w.code for w in offer.warnings}


@pytest.mark.parametrize("ratio", [2.5, 4.8499, 5.1501])
@pytest.mark.parametrize("registered_agents", [("external",)], indirect=True)
def test_size_ac4_mismatch_visible_and_requires_approval(gateway, need, upstream, ratio):
    product_size(gateway, name="Milk 1 L", multiplier=5)
    upstream.price = ratio * 2.6
    lookup = gateway.find_offers(need)
    offer = next(o for o in lookup.offers if o.product_id == "1")
    assert offer.size_per_sales_unit == pytest.approx(ratio)
    assert offer.size_source == "price_ratio"
    assert offer.size_confirmed_by_price is False
    mismatch = next(w for w in offer.warnings if w.code == "SIZE_MISMATCH")
    assert "5" in mismatch.text and str(ratio) in mismatch.text
    quote = gateway.orders.create_quote(quote_request(need, qty=2, need_ref=lookup.lookup_id))
    assert quote.lines[0].units_needed == 2  # Explicit quantities are never recomputed.
    assert quote.total == round(2 * upstream.price, 2)
    order = gateway.orders.place_order(quote.id, "size-mismatch")
    assert order.status == "PENDING_APPROVAL"
    assert {h.rule for h in order.policy.hits} == {"SIZE_MISMATCH"}


def test_size_ac5_ratio_only_unchanged(gateway, need, upstream):
    product_size(gateway, name="Milk unknown size", multiplier=5)
    offer = first_offer(gateway, need)
    assert offer.size_per_sales_unit == 5.0
    assert offer.size_source == "price_ratio"
    assert offer.size_confirmed_by_price is False
    assert offer.warnings == []


def test_size_ac6_price_error_keeps_unconfirmed_name(gateway, need, upstream):
    product_size(gateway, name="Milk 1 L", multiplier=5)
    upstream.price_error = True
    offer = first_offer(gateway, need)
    assert offer.size_per_sales_unit == 5.0
    assert offer.size_source == "name_parse"
    assert offer.size_confirmed_by_price is False
    assert {w.code for w in offer.warnings} == {"PRICE_ERROR"}
    assert offer.units_needed == 200


def test_size_ac7_policy_and_generated_contract(gateway):
    with TestClient(create_app(gateway)) as client:
        response = client.get("/v1/policy")
        assert response.status_code == 200
        policy = response.json()
        assert "SIZE_MISMATCH" in policy["known_warning_codes"]
        assert "SIZE_MISMATCH" in policy["approval"]["warnings_need_approval"]
    contract = Path(__file__).resolve().parents[2] / "contracts/openapi.yaml"
    schema = yaml.safe_load(contract.read_text())
    assert schema == create_app().openapi()
    field = schema["components"]["schemas"]["OfferCard"]["properties"]["size_confirmed_by_price"]
    assert field["type"] == "boolean" and field["default"] is False
    assert field["description"]


def kg_price(ratio):
    return {"catalogPrice": {"value": ratio * 4.0}, "comparisonPrice": {"value": 4.0}, "comparisonUnit": "KG"}


@pytest.mark.parametrize("price,confirmed", [({}, False), (kg_price(1.0), True)])
def test_size_ac9_kg_sold_is_one_kg_per_unit(gateway, price, confirmed):
    from app.lookup import reconcile_size
    product = gateway.catalog.get("7").model_copy(update={
        "name_en": "Liver pate appr 2,4 kg", "name_fi": "Maksapasteija n 2,4 kg", "base_net_weight_kg": 2.4})
    assert product.sales_unit == "KG"
    assert reconcile_size(product, price, "KG") == (1.0, "base_unit", confirmed, [])


def test_size_ac10_pack_count_in_name(gateway):
    from app.lookup import reconcile_size
    base = gateway.catalog.get("1")
    bun = base.model_copy(update={"name_en": "Bun 55x100g", "name_fi": "Pulla 55x100g", "units": {"KI": 1},
                                  "base_volume_l": None, "base_net_weight_kg": None})
    size, source, confirmed, warnings = reconcile_size(bun, kg_price(5.5), "KG")
    assert (round(size, 6), source, confirmed, warnings) == (5.5, "name_parse", True, [])
    spaced = bun.model_copy(update={"name_en": "Flatbread 72 x 32 g", "name_fi": "Rieska 72 x 32 g"})
    assert round(reconcile_size(spaced, {}, "KG")[0], 6) == 2.304
    milk = base.model_copy(update={"name_en": "5 x Milk 1 L", "name_fi": "5 x Maito 1 L", "units": {"KI": 5},
                                   "base_volume_l": None, "base_net_weight_kg": None})
    assert reconcile_size(milk, {}, "L")[:2] == (5.0, "name_parse")
    portion = base.model_copy(update={"name_en": "Portion milk 100x2 cl", "name_fi": "Annosmaito 100x2 cl",
                                      "units": {"KI": 1}, "base_volume_l": None, "base_net_weight_kg": None})
    assert round(reconcile_size(portion, {}, "L")[0], 6) == 2.0
