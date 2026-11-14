from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.api import create_app
from conftest import OPERATOR_HEADERS, TEST_KEYS
from conftest import quote_request


def review_pack_flow(gateway, need, upstream, tmp_path, respx_mock, actual_product_rows,
                     product_id, requested, expected_size, expected_units, comparison=False):
    import httpx
    from app.models import Quantity
    from conftest import rebuild_catalog
    row = next(r for r in actual_product_rows if str(r["id"]) == product_id)
    rebuild_catalog(gateway, tmp_path, [row])
    product = gateway.catalog.get(product_id)
    request = need.model_copy(update={"intent": product.name_en, "search_terms": [product.name_en.split()[0]],
        "required_claims": [], "exclude_allergens": [], "quantity": Quantity(value=requested, unit="KG")})

    def price(req):
        body = upstream.price_response(req).json()
        for item in body["products"]:
            item["comparisonUnit"] = "KG"
            if comparison:
                item["comparisonPrice"] = {"value": 13 / expected_size}
            else:
                item.pop("comparisonPrice", None)
        return httpx.Response(200, json=body)

    respx_mock.post("https://valio.test/api/price").mock(side_effect=price)
    result = gateway.find_offers(request)
    offer, plan = result.offers[0], result.plans[0]
    assert (offer.size_per_sales_unit, offer.units_needed, offer.purchase_total) == (
        expected_size, expected_units, 13 * expected_units)
    assert (plan.lines[0].units, plan.total) == (expected_units, 13 * expected_units)
    if not comparison:
        assert "SIZE_UNCONFIRMED" in {w.code for w in offer.warnings}
        assert plan.policy_preview.outcome == "PENDING_APPROVAL"
    quote = gateway.orders.create_quote(quote_request(request, qty=plan.lines[0].units,
        product_id=product_id, need_ref=result.lookup_id), "external")
    assert (quote.lines[0].size_per_sales_unit, quote.total) == (expected_size, 13 * expected_units)
    order = gateway.orders.place_order(quote.id, f"review-{product_id}-{comparison}", "external")
    if not comparison:
        assert order.status == "PENDING_APPROVAL"
    assert gateway.lookup.get(result.lookup_id).offers[0].units_needed == expected_units
    assert gateway.orders.get_quote(quote.id).total == quote.total
    assert gateway.orders.get_order(order.id).total == quote.total
    return offer


@pytest.mark.parametrize("comparison", [False, True])
def test_review_explicit_package_total(gateway, need, upstream, tmp_path, respx_mock, actual_product_rows, comparison):
    review_pack_flow(gateway, need, upstream, tmp_path, respx_mock, actual_product_rows,
                     "470742", 27.7, 2.77, 10, comparison)


@pytest.mark.parametrize("comparison", [False, True])
def test_review_repeated_pack_not_nested(gateway, need, upstream, tmp_path, respx_mock, actual_product_rows, comparison):
    offer = review_pack_flow(gateway, need, upstream, tmp_path, respx_mock, actual_product_rows,
                            "476903", 16, 4, 4, comparison)
    assert offer.name_size_candidates
    assert {c.size_per_sales_unit for c in offer.name_size_candidates} == {4}
    assert any("4x1kg Auringonkukansiemen" in c.source_text for c in offer.name_size_candidates)
    from app.catalog import named_case_sizes
    assert named_case_sizes(["8 x Brand 4x70g"], "KG") == [2.24]


@pytest.mark.parametrize("name,multiplier,expected", [
    ("8 x Brand 4x70g", 8, 2.24), ("10x(5x220g)", 10, 11),
    ("5kg/25g 200pcs", 200, 5), ("22g/2.77kg", 126, 2.77),
    ("450 x 310 x 80 mm, 11160 ml, 50 pcs", 450, 558),
])
def test_review_local_pack_counts_and_totals(gateway, name, multiplier, expected):
    from app.lookup import reconcile_size
    product = gateway.catalog.get("1").model_copy(update={"name_en": name, "name_fi": name,
        "units": {"KI": multiplier}, "base_volume_l": None, "base_net_weight_kg": None})
    size, _, confirmed, warnings = reconcile_size(product, {}, "L" if "ml" in name else "KG")
    assert size == expected
    assert not confirmed and "SIZE_UNCONFIRMED" in {w.code for w in warnings}


@pytest.mark.parametrize("comparison", [False, True])
def test_review_inner_portion_count(gateway, need, upstream, tmp_path, respx_mock, actual_product_rows, comparison):
    review_pack_flow(gateway, need, upstream, tmp_path, respx_mock, actual_product_rows,
                     "464706", 62.5, 2.5, 25, comparison)


@pytest.mark.parametrize("product_id,unit,size", [
    ("470742", "KG", 2.77), ("476903", "KG", 4), ("464706", "KG", 2.5),
    ("462607", "KG", .15), ("464639", "KG", .9), ("460892", "KG", 10.8),
    ("477242", "L", 7.92), ("460121", "KG", 5), ("461802", "KG", 5),
    ("461798", "KG", 5), ("471894", "KG", 6), ("472819", "KG", 4),
])
def test_review_independent_real_package_sizes(need, actual_product_rows, product_id, unit, size):
    from app.catalog import extract
    from app.lookup import offer_for
    from app.models import Quantity
    product = extract(next(r for r in actual_product_rows if str(r["id"]) == product_id), {}, {})
    request = need.model_copy(update={"quantity": Quantity(value=size * 10, unit=unit)})
    offer = offer_for(product, request, {}, {})
    assert (offer.size_per_sales_unit, offer.units_needed) == (size, 10)
    assert "SIZE_UNCONFIRMED" in {w.code for w in offer.warnings}
    assert offer.name_size_candidates
    assert all(c.source_text in {product.name_en, product.name_fi} for c in offer.name_size_candidates)


def test_review_every_real_product_size_is_proven_or_requires_approval(need, actual_product_rows,
                                                                      record_testsuite_property):
    """Exhaustive property over the organiser's real records, without supplier calls."""
    from app.catalog import extract
    from app.lookup import offer_for
    from app.models import Quantity
    count = 0
    for row in actual_product_rows:
        if row.get("deleted"):
            continue
        product = extract(row, {}, {})
        count += 1
        for unit in ("L", "KG"):
            request = need.model_copy(update={"quantity": Quantity(value=1, unit=unit)})
            unpriced = offer_for(product, request, {}, {})
            # Missing/wrong-unit comparison data must never silently authorise a name estimate.
            for price in ({}, {"catalogPrice": {"value": 13}, "comparisonPrice": {"value": 2},
                               "comparisonUnit": "KG" if unit == "L" else "L"},
                          {"catalogPrice": {"value": 13}, "comparisonPrice": {"value": 2},
                               "comparisonUnit": unit}):
                offer = offer_for(product, request, price, {})
                codes = {w.code for w in offer.warnings}
                if offer.size_per_sales_unit is None:
                    assert "SIZE_UNKNOWN" in codes, product.id
                elif offer.size_source in {"base_unit", "sales_unit", "price_ratio"}:
                    continue  # Definitions and actual invoiced ratios are independent of names.
                elif "SIZE_UNCONFIRMED" not in codes:
                    # Independently check rule (a), rather than trusting the boolean.
                    ratio = 6.5 if price.get("comparisonUnit") == unit else None
                    assert ratio and abs(ratio / offer.size_per_sales_unit - 1) <= .030000000001, product.id
                    assert offer.size_confirmed_by_price, product.id
                if not price and offer.size_source == "name_parse":
                    assert unpriced.name_size_candidates, product.id
                    assert offer.size_per_sales_unit == min(c.size_per_sales_unit
                        for c in offer.name_size_candidates), product.id
    record_testsuite_property("size_confirmation_real_products", count)
    assert count == 17546


def test_review_legacy_size_evidence_keeps_signed_shape(gateway, need, upstream):
    import json
    from app.signing import verify_quote
    lookup = gateway.find_offers(need)
    quote = gateway.orders.create_quote(quote_request(need, need_ref=lookup.lookup_id))
    data = quote.model_dump(mode="json")
    for line in data["lines"]:
        line.pop("name_size_candidates", None)
        line.pop("minimum_order_quantity", None)
    data["signature"] = gateway.signing.sign_quote(data)
    legacy_lookup = lookup.model_dump(mode="json")
    for offer in legacy_lookup["offers"]:
        offer.pop("name_size_candidates", None)
        offer.pop("minimum_order_quantity", None)
    with gateway.db.connect(write=True) as conn:
        conn.execute("UPDATE quotes SET data=? WHERE id=?", (json.dumps(data), quote.id))
        conn.execute("UPDATE lookups SET data=? WHERE id=?", (json.dumps(legacy_lookup), lookup.lookup_id))
    loaded = gateway.orders.get_quote(quote.id)
    assert all("name_size_candidates" not in line for line in loaded.model_dump(mode="json")["lines"])
    verify_quote(loaded, gateway.signing.jwks())
    assert all("name_size_candidates" not in o for o in gateway.lookup.get(lookup.lookup_id).model_dump(mode="json")["offers"])
    order = gateway.orders.place_order(quote.id, "legacy-name-candidates")
    historical = order.model_dump(mode="json")
    for line in historical["lines"]:
        line.pop("name_size_candidates", None)
        line.pop("minimum_order_quantity", None)
    with gateway.db.connect(write=True) as conn:
        conn.execute("UPDATE orders SET data=? WHERE id=?", (json.dumps(historical), order.id))
    assert all("name_size_candidates" not in line for line in
               gateway.orders.get_order(order.id).model_dump(mode="json")["lines"])


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
def test_size_ac9_kg_sold_is_one_kg_per_unit(gateway, need, price, confirmed):
    from app.lookup import reconcile_size
    product = gateway.catalog.get("7").model_copy(update={
        "name_en": "Liver pate appr 2,4 kg", "name_fi": "Maksapasteija n 2,4 kg", "base_net_weight_kg": 2.4})
    assert product.sales_unit == "KG"
    assert reconcile_size(product, price, "KG") == (1.0, "base_unit", confirmed, [])
    from app.lookup import offer_for
    request = need.model_copy(update={"quantity": need.quantity.model_copy(update={"value": 10, "unit": "KG"})})
    assert offer_for(product, request, price, {}).units_needed == 10


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


@pytest.mark.parametrize("name,expected", [("Meatpie 10/1500G", 1.5), ("Meatpie 10 / 1500 G", 1.5),
                                         ("Pickles 400/240G", .4), ("Pickles 460 / 230 g", .46)])
def test_review_count_total_weight(name, expected):
    from app.catalog import name_sales_sizes
    assert name_sales_sizes([name], "KG", 1) == [expected]


@pytest.mark.parametrize("names,expected", [
    (["Box 900g/60pcs", "60KPL/900G PAK"], .9),
    (["Bag 200g / 4pcs"], .2), (["Bag 4pcs / 200g"], .2),
    (["Box 60 KPL / 900 G", "Box 900 g / 60 pcs"], .9),
])
def test_review_translated_portion_totals(names, expected):
    from app.catalog import name_sales_sizes
    assert set(name_sales_sizes(names, "KG", 1)) == {expected}


def test_review_complete_nested_pieces(gateway):
    from app.catalog import name_measures
    from app.lookup import reconcile_size
    name = "Sausage (9x15pcs) 10,8kg/80g"
    measures = name_measures(name, "KG")
    assert (.08, [9, 15]) in measures
    assert (10.8, []) in measures
    product = gateway.catalog.get("1").model_copy(update={"name_en": name, "name_fi": name,
        "units": {"KI": 1}, "base_volume_l": None, "base_net_weight_kg": 10.8})
    assert reconcile_size(product, {}, "KG")[:2] == (10.8, "catalog")


@pytest.mark.parametrize("ratio,expected,source", [(.5, .5, "name_parse"), (.51, .5, "name_parse"),
                                                  (1., 1., "catalog")])
@pytest.mark.parametrize("reverse", [False, True])
def test_review_price_confirmed_translation(gateway, need, ratio, expected, source, reverse):
    from app.lookup import offer_for
    names = ["Drink 500 ml", "Drink 1 l"]
    product = gateway.catalog.get("1").model_copy(update={"name_en": names[reverse], "name_fi": names[not reverse],
        "units": {"KI": 1}, "base_volume_l": 1})
    request = need.model_copy(update={"quantity": need.quantity.model_copy(update={"value": 10.1})})
    price = {"catalogPrice": {"value": 13}, "comparisonPrice": {"value": 13 / ratio}, "comparisonUnit": "L"}
    offer = offer_for(product, request, price, {})
    assert (offer.size_per_sales_unit, offer.size_source, offer.size_confirmed_by_price) == (expected, source, True)
    assert offer.units_needed == (21 if expected == .5 else 11)
    assert "SIZE_MISMATCH" not in {w.code for w in offer.warnings}


@pytest.mark.parametrize("catalog,names", [(1., ["Drink 500 ml", "Drink 1 l"]),
                                         (1., ["Drink 500 ml"]),
                                         (5., ["Meatballs 5kg/15g"]),
                                         (4., ["Meat balls 12g, 4kg"])])
def test_review_catalog_anchor_without_ratio(gateway, catalog, names):
    from app.lookup import reconcile_size
    unit = "L" if catalog == 1 else "KG"
    product = gateway.catalog.get("1").model_copy(update={"name_en": names[0], "name_fi": names[-1],
        "units": {"KI": 1}, "base_volume_l": catalog if unit == "L" else None,
        "base_net_weight_kg": catalog if unit == "KG" else None})
    result = reconcile_size(product, {}, unit)
    assert result[:2] == (catalog, "catalog")
    if catalog == 1:
        assert {w.code for w in result[3]} == {"SIZE_UNCONFIRMED", "SIZE_MISMATCH"}


def test_review_whole_catalog_anchor_guard(actual_product_rows):
    import hashlib
    import json
    from app.config import ROOT
    from app.catalog import extract
    from app.lookup import reconcile_size
    fixture = json.loads((Path(__file__).parent / "fixtures/catalog_anchor_sizes.json").read_text())
    assert fixture["catalog_sha256"] == hashlib.sha256((ROOT / "data/product_data.json.gz").read_bytes()).hexdigest()
    assert fixture["product_count"] == len(actual_product_rows)
    checked, failures = 0, []
    for row in actual_product_rows:
        for unit, expected in fixture["sizes"].get(str(row["id"]), {}).items():
            actual = reconcile_size(extract(row, {}, {}), {}, unit)[0]
            if actual != expected:
                failures.append((str(row["id"]), unit, expected, actual))
            checked += 1
    assert checked == fixture["anchor_count"]
    assert not failures, f"{len(failures)} anchor regressions: {failures[:20]}"


@pytest.mark.parametrize("name,anchor,expected", [
    ("Sausage (9x15pcs) 80g", None, []),
    ("Sausage (9x15pcs) 80g", 10.8, [(.08, [9, 15])]),
    ("Sausage (9x15pcs) 10.8kg/50g", 10.8, [(10.8, [])]),
])
def test_review_nested_pieces_require_total(name, anchor, expected):
    from app.catalog import name_measures
    assert name_measures(name, "KG", anchor) == expected


def test_review_portion_chain_preserves_total_with_multiplier():
    from app.catalog import name_sales_sizes
    assert set(name_sales_sizes(["Sausage (9x15pcs) 10.8kg/80g"], "KG", 2)) == {21.6}


def test_review_dimensions_are_not_pack_counts():
    from app.catalog import name_sales_sizes
    assert name_sales_sizes(["Tray 136x218*155*60mm 1200ml"], "L", 1) == [1.2]


@pytest.mark.parametrize("ratio", [.97, 1.03])
def test_review_kg_inclusive_boundary(gateway, ratio):
    from app.lookup import reconcile_size
    assert reconcile_size(gateway.catalog.get("7"), kg_price(ratio), "KG")[2] is True


def test_review_name_count_cannot_override_known_multiplier(gateway, need, upstream):
    product_size(gateway, name="Milk 5 x carton 1 L", multiplier=10)
    upstream.price = 5 * 2.6
    result = gateway.find_offers(need)
    offer = next(o for o in result.offers if o.product_id == "1")
    assert (offer.size_per_sales_unit, offer.size_source, offer.size_confirmed_by_price) == (5, "price_ratio", False)
    assert "SIZE_MISMATCH" in {w.code for w in offer.warnings}
    quote = gateway.orders.create_quote(quote_request(need, qty=2, need_ref=result.lookup_id))
    assert gateway.orders.place_order(quote.id, "conflicting-count").status == "PENDING_APPROVAL"


def test_size_ac8_recorded_milk_lookup_all_three_pack_counts(gateway, need, respx_mock):
    from conftest import replay_recorded_demo
    result = replay_recorded_demo(gateway, need, respx_mock)
    offers = {o.product_id: o for o in result.offers}
    for pid, size, units in [("469330", 5, 200), ("463038", 20, 50), ("464768", 12, 84)]:
        assert (offers[pid].size_per_sales_unit, offers[pid].units_needed) == (size, units)


def test_review_known_count_without_ratio_keeps_conflict_visible(gateway):
    from app.lookup import reconcile_size
    product = gateway.catalog.get("1").model_copy(update={"name_en": "Yoghurt 4x125g", "name_fi": "Yoghurt 4x125g",
        "units": {"KI": 6}, "base_volume_l": None, "base_net_weight_kg": None})
    size, source, confirmed, warnings = reconcile_size(product, {}, "KG")
    assert (size, source, confirmed) == (.75, "name_parse", False)
    assert {"SIZE_UNCONFIRMED", "SIZE_MISMATCH"} <= {w.code for w in warnings}
