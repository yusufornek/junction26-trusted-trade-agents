from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.api import create_app
from conftest import OPERATOR_HEADERS, TEST_KEYS
from conftest import quote_request


@pytest.mark.parametrize("index", range(30))
@pytest.mark.parametrize("fraction", ["0.1", "0.2", "0.3", "3"])
@pytest.mark.parametrize("sales_units", [False, True])
def test_review_real_catalog_minimal_packs(need, real_pack_products, index, fraction, sales_units):
    from decimal import Decimal
    from app.lookup import offer_for, reconcile_size
    from app.models import Quantity
    product, unit = real_pack_products[index]
    unit = "SALES_UNIT" if sales_units else unit
    size = Decimal(str(reconcile_size(product, {}, unit)[0])).quantize(Decimal("0.000001"))
    requested = size * Decimal(fraction)
    request = need.model_copy(update={"quantity": Quantity(value=float(requested), unit=unit)})
    offer = offer_for(product, request, {}, {})
    units = Decimal(str(offer.units_needed))
    from app.quantities import sales_step
    step = sales_step(product)
    assert units * size >= requested
    assert (units - step) * size < requested
    if step == 1:
        assert (units - 1) * size < requested
    assert Decimal(str(offer.overbuy.amount)) == units * size - requested


def test_review_portion_weight_preserves_package_weight(gateway, need, actual_product_rows):
    from app.catalog import extract
    from app.lookup import offer_for
    product = extract(next(r for r in actual_product_rows if str(r["id"]) == "460086"), {}, {})
    need.quantity.unit, need.quantity.value = "KG", 1
    price = {"catalogPrice": {"value": 2, "currency": "EUR", "validTo": "9999-12-31T00:00:00Z"}}
    offer = offer_for(product, need, price, {}, {"probability": .99, "question": "fits"})
    assert not product.size_suspect
    assert (offer.size_per_sales_unit, offer.units_needed, offer.purchase_total) == (.5, 2, 4)
    assert {w.code for w in offer.warnings} >= {"SIZE_UNCONFIRMED"}


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


@pytest.mark.parametrize("name,multiplier,ratio", [("Milk unknown size", 5, 5),
    ("12x Milk 1 000 ml", 1, 12), ("12x Milk 0 ml", 1, 12), ("Milk 0 kg", 1, 12)])
def test_size_ac5_ratio_only_unchanged(gateway, need, upstream, name, multiplier, ratio):
    product_size(gateway, name=name, multiplier=multiplier)
    upstream.price = ratio * 2.6
    offer = first_offer(gateway, need)
    assert offer.size_per_sales_unit == ratio
    assert offer.size_source == "price_ratio"
    assert offer.size_confirmed_by_price is False
    assert {w.code for w in offer.warnings} == {"AVAILABILITY_UNCONFIRMED"}


def test_review_zero_name_size_preserves_ratio(gateway, need, upstream):
    from app.catalog import named_case_sizes
    from app.lookup import reconcile_size
    product_size(gateway, name="12x Milk 1 000 ml", multiplier=1)
    product = gateway.catalog.get("1")
    price = {"catalogPrice": {"value": 24}, "comparisonPrice": {"value": 2}, "comparisonUnit": "L"}
    assert reconcile_size(product, price, "L") == (12, "price_ratio", False, [])
    assert named_case_sizes([product.name_en, "Milk 0 L", "12 x 0.00000001 ml"], "L") == []
    upstream.price = 12 * 2.6
    offer = first_offer(gateway, need)
    assert (offer.size_per_sales_unit, offer.units_needed) == (12, 84)


def test_review_all_catalog_sizes_do_not_crash(actual_product_rows, need, record_testsuite_property):
    from app.catalog import extract
    from app.lookup import offer_for
    from app.models import Quantity
    failures, checked = [], 0
    for row in actual_product_rows:
        product = extract(row, {}, {})
        for unit in ("L", "KG", "SALES_UNIT"):
            for value in (1, 7.5, 1000):
                request = need.model_copy(update={"quantity": Quantity(value=value, unit=unit)})
                for with_ratio in (False, True):
                    price = {"catalogPrice": {"value": 24, "currency": "EUR",
                                             "validTo": "9999-12-31T00:00:00Z"}}
                    if with_ratio:
                        price.update(comparisonPrice={"value": 2}, comparisonUnit=unit)
                    try:
                        offer = offer_for(product, request, price, {})
                        assert offer.size_per_sales_unit is None or offer.size_per_sales_unit > 0
                        assert offer.units_needed is None or offer.units_needed > 0
                    except Exception as error:
                        failures.append((product.id, unit, value, with_ratio, repr(error)))
                    checked += 1
    record_testsuite_property("reconciliations_checked", checked)
    assert checked == len(actual_product_rows) * 18
    assert not failures, f"{len(failures)} failures: {failures[:12]}"


def test_size_ac6_price_error_keeps_unconfirmed_name(gateway, need, upstream):
    product_size(gateway, name="Milk 1 L", multiplier=5)
    upstream.price_error = True
    offer = first_offer(gateway, need)
    assert offer.size_per_sales_unit == 5.0
    assert offer.size_source == "name_parse"
    assert offer.size_confirmed_by_price is False
    assert {w.code for w in offer.warnings} == {"PRICE_ERROR", "SIZE_UNCONFIRMED", "AVAILABILITY_UNCONFIRMED"}
    assert offer.units_needed == 200


def test_size_ac7_policy_and_generated_contract(gateway):
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        response = client.get("/v1/policy")
        assert response.status_code == 200
        policy = response.json()
        assert "SIZE_MISMATCH" in policy["known_warning_codes"]
        assert "SIZE_MISMATCH" in policy["approval"]["warnings_need_approval"]
    contract = Path(__file__).resolve().parents[2] / "contracts/openapi.yaml"
    schema = yaml.safe_load(contract.read_text())
    assert schema == create_app(keys=TEST_KEYS).openapi()
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
    for name, multiplier, unit, expected in [
        ("8 x Pastry 4x70g", 8, "KG", 2.24),
        ("4x6x Water 0,33 l", 4, "L", 7.92),
        ("Pastry 10x9x50g", 10, "KG", 4.5),
        ("4 x 6 x 0.33 L", 24, "L", 7.92),
        ("4 x 6 x 0.33 L", 1, "L", 7.92),
        ("12 x Pickles 460/230g", 12, "KG", 5.52),
    ]:
        product = base.model_copy(update={"name_en": name, "name_fi": name,
            "units": {"KI": multiplier}, "base_volume_l": None, "base_net_weight_kg": None})
        assert reconcile_size(product, {}, unit)[:2] == (expected, "name_parse")
