import asyncio
import copy
import json
import subprocess
import sys
from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from app import audit
from app.api import create_app
from conftest import OPERATOR_HEADERS, agent_headers
from app.catalog import build, extract, hard_violations
from app.db import DomainError
from app.lookup import availability_for, offer_for, rank, timestamp
from app.mcp_server import create_server
from app.models import PolicyUpdateRequest, Quantity, QuoteLineRequest, QuoteRequest
from conftest import demo_catalog, quote_request, raw_product, rebuild_catalog


def assert_review_case_purchase(gateway, need, upstream, tmp_path, respx_mock,
                               actual_product_rows, pid, unit, size, stored):
    row = next(r for r in actual_product_rows if str(r["id"]) == pid)
    rebuild_catalog(gateway, tmp_path, [row])
    product = gateway.catalog.get(pid)
    if stored != "current":
        data = product.model_dump()
        for field in ("size_suspect", "volume_suspect", "weight_suspect"):
            if stored == "missing":
                data.pop(field)
            else:
                data[field] = stored == "suspect"
        with gateway.db.connect(write=True) as conn:
            conn.execute("UPDATE products SET data=? WHERE id=?", (json.dumps(data), pid))
    request = need.model_copy(update={"intent": product.name_en, "search_terms": [],
        "required_claims": [], "exclude_allergens": [], "quantity": Quantity(value=size, unit=unit)})
    upstream.sales_units[pid] = product.sales_unit

    def price(request):
        body = upstream.price_response(request).json()
        for item in body["products"]:
            item.pop("comparisonPrice")
        return httpx.Response(200, json=body)

    respx_mock.post("https://valio.test/api/price").mock(side_effect=price)
    result = gateway.find_offers(request)
    offer, plan = result.offers[0], result.plans[0]
    assert (offer.size_per_sales_unit, offer.units_needed, offer.purchase_total) == (size, 1, 13)
    assert offer.size_source == "catalog"
    assert "SIZE_UNCONFIRMED" in codes(offer) and "SIZE_MISMATCH" not in codes(offer)
    assert (offer.overbuy.amount, plan.lines[0].units, plan.total, plan.coverage.value) == (0, 1, 13, size)
    assert plan.policy_preview.outcome == "PENDING_APPROVAL"
    quote = gateway.orders.create_quote(quote_request(request, qty=1, product_id=pid,
        need_ref=result.lookup_id), "external")
    assert (quote.lines[0].size_per_sales_unit, quote.total) == (size, 13)
    order = gateway.orders.place_order(quote.id, f"review-{pid}-{stored}", "external")
    assert order.status == "PENDING_APPROVAL"
    approved = gateway.orders.approve(order.id, "Ada")
    assert (approved.status, approved.total, approved.lines[0].units_needed) == ("CONFIRMED", 13, 1)
    # Retained catalog flags and serialized lookup/quote/order evidence need no migration.
    assert gateway.lookup.get(result.lookup_id).offers[0].size_per_sales_unit == size
    assert gateway.orders.get_quote(quote.id).total == gateway.orders.get_order(order.id).total == 13


@pytest.mark.parametrize("stored", ["current", "suspect", "not_suspect", "missing"])
@pytest.mark.parametrize("pid,unit,size", [("460913", "KG", 2.24), ("477461", "L", 7.92),
                                         ("462402", "KG", 4.5)])
def test_review_nested_case_counts(gateway, need, upstream, tmp_path, respx_mock,
                                  actual_product_rows, pid, unit, size, stored):
    assert_review_case_purchase(gateway, need, upstream, tmp_path, respx_mock,
                               actual_product_rows, pid, unit, size, stored)


@pytest.mark.parametrize("stored", ["current", "suspect", "not_suspect", "missing"])
def test_review_net_drained_case_weight(gateway, need, upstream, tmp_path, respx_mock,
                                       actual_product_rows, stored):
    assert_review_case_purchase(gateway, need, upstream, tmp_path, respx_mock,
                               actual_product_rows, "462050", "KG", 5.52, stored)


@pytest.mark.parametrize("catalog_size", [7.0, 8.5])
@pytest.mark.parametrize("ratio", [None, 7.92])
def test_review_conflicting_case_evidence_needs_review(gateway, need, upstream, tmp_path, respx_mock,
                                                      catalog_size, ratio):
    row = raw_product("case", "4x6x Drink 0.33 L", multiplier=4)
    row["synkkaData"]["unitConversions"] = [{"synkkaPackagingType": "CASE",
        "volume": {"value": catalog_size, "unit": "LTR"}}]
    rebuild_catalog(gateway, tmp_path, [row])
    request = need.model_copy(update={"search_terms": ["Drink"], "required_claims": [],
        "exclude_allergens": [], "quantity": Quantity(value=7.92, unit="L")})

    def price(request):
        body = upstream.price_response(request).json()
        for item in body["products"]:
            if ratio:
                item.update(comparisonPrice={"value": 13 / ratio}, comparisonUnit="L")
            else:
                item.pop("comparisonPrice")
        return httpx.Response(200, json=body)

    respx_mock.post("https://valio.test/api/price").mock(side_effect=price)
    result = gateway.find_offers(request)
    offer, plan = result.offers[0], result.plans[0]
    if ratio:
        assert (offer.size_per_sales_unit, offer.units_needed, offer.size_confirmed_by_price) == (7.92, 1, True)
    else:
        assert offer.size_per_sales_unit == catalog_size
        assert offer.units_needed == (2 if catalog_size < 7.92 else 1)
        mismatch = next(w for w in offer.warnings if w.code == "SIZE_MISMATCH")
        assert f"catalog={catalog_size:g}" in mismatch.text and "name_parse=7.92" in mismatch.text
        assert f"used={catalog_size:g}" in mismatch.text and "from catalog" in mismatch.text
        assert "SIZE_UNCONFIRMED" in codes(offer)
        assert plan.lines[0].units == offer.units_needed
        assert plan.policy_preview.outcome == "PENDING_APPROVAL"
        assert "SIZE_MISMATCH" in {w.code for w in plan.warnings}
        quote = gateway.orders.create_quote(quote_request(request, qty=plan.lines[0].units,
            product_id="case", need_ref=result.lookup_id), "external")
        assert quote.lines[0].units_needed == plan.lines[0].units
        order = gateway.orders.place_order(quote.id, "review-conflicting-evidence", "external")
        assert order.status == "PENDING_APPROVAL"
        assert "SIZE_MISMATCH" in {hit.rule for hit in order.policy.hits}


@pytest.mark.parametrize("unit,field,measure", [("L", "volume", "LTR"), ("KG", "netWeight", "KGM")])
@pytest.mark.parametrize("count", [2, 6, 24])
@pytest.mark.parametrize("named", [False, True, "single"])
def test_review_case_content_matches_packaging_level(unit, field, measure, count, named):
    name_count = 1 if named == "single" else count
    name = f"{name_count} x Product 0.33 {'L' if unit == 'L' else 'kg'}" if named else "Product"
    row = raw_product("case", name, base="BOT" if unit == "L" else "ST", multiplier=count)
    row["synkkaData"]["unitConversions"] = [
        {"synkkaPackagingType": "BASE_UNIT_OR_EACH", field: {"value": .33, "unit": measure}},
        {"synkkaPackagingType": "CASE", field: {"value": .33, "unit": measure}}]
    from app.lookup import reconcile_size
    product = extract(row, {}, {})
    # Wrong CASE content must not discard the independent, valid BASE evidence.
    assert reconcile_size(product, {}, unit)[0] == round(.33 * count, 6)
    assert product.volume_suspect if unit == "L" else product.weight_suspect
    row["synkkaData"]["unitConversions"][1][field]["value"] = round(.33 * count, 6)
    product = extract(row, {}, {})
    assert not (product.volume_suspect if unit == "L" else product.weight_suspect)
    assert reconcile_size(product, {}, unit)[:2] == (round(.33 * count, 6), "catalog")


@pytest.mark.parametrize("price", [{}, {"catalogPrice": {"value": 13},
    "comparisonPrice": {"value": 13 / 6}, "comparisonUnit": "L"}])
def test_review_case_name_count_conflict_is_visible(price):
    from app.lookup import reconcile_size
    row = raw_product("case", "Milk 24 x 0.33 L", multiplier=6, volume=.33)
    product = extract(row, {}, {})
    size, _, _, warnings = reconcile_size(product, price, "L")
    # Spec 05 R-8 retains the sales multiplier when it already carries a count.
    # A contradictory name must nevertheless be visible, with or without a live ratio.
    assert size == (6 if price else 1.98)
    assert "SIZE_MISMATCH" in {w.code for w in warnings}


def test_review_case_prefix_count_with_unit_multiplier_one():
    from app.lookup import reconcile_size
    row = raw_product("case", "24x Brooklyn beer 0.33 L", base="KI", multiplier=1)
    row["synkkaData"]["unitConversions"] = [{"synkkaPackagingType": "CASE",
        "volume": {"value": .33, "unit": "LTR"}}]
    product = extract(row, {}, {})
    assert product.volume_suspect
    assert reconcile_size(product, {}, "L")[:2] == (7.92, "name_parse")


def test_review_all_real_case_name_sizes(need, real_case_name_products, record_testsuite_property):
    from decimal import Decimal
    failures = []
    record_testsuite_property("catalog_products_covered", len(real_case_name_products))
    for product, expected in real_case_name_products:
        for unit, sizes in expected.items():
            request = need.model_copy(update={"quantity": Quantity(value=1, unit=unit)})
            # A valid mock case price, deliberately without comparisonPrice: no ratio can hide bad content.
            offer = offer_for(product, request,
                {"catalogPrice": {"value": 13, "currency": "EUR", "validTo": "9999-12-31T00:00:00Z"}}, {})
            if not sizes:
                # Count-only names provide no L/KG oracle; retained catalog evidence must support the conversion.
                base = product.base_volume_l if unit == "L" else product.base_net_weight_kg
                case = product.sales_volume_l if unit == "L" else product.sales_net_weight_kg
                sizes = {Decimal(str(case))} if case else (
                    {Decimal(str(base)) * Decimal(str(product.units.get(product.sales_unit, 1)))} if base else set())
            sizes = {size for size in sizes if size > 0}
            if not sizes:
                assert offer.size_per_sales_unit is None or offer.size_per_sales_unit > 0
                continue
            used = Decimal(str(offer.size_per_sales_unit or 0))
            if not used:
                assert "SIZE_UNKNOWN" in codes(offer)
                continue  # Nested pcs counts without a reconciling total are not evidence.
            # An approval warning never permits an estimate above every independent
            # name/catalog bound (the other lane's exhaustive safety assertion).
            bounds = set(sizes)
            if product.base_unit == unit and product.units.get(product.sales_unit, 0) > 0:
                # A KG-to-case conversion is already a measure, not a piece count.
                bounds.add(Decimal(str(product.units[product.sales_unit])))
            base = product.base_volume_l if unit == "L" else product.base_net_weight_kg
            case = product.sales_volume_l if unit == "L" else product.sales_net_weight_kg
            if base and base > 0:
                bounds.add(Decimal(str(base)) * Decimal(str(product.units.get(product.sales_unit, 1))))
            if case and case > 0:
                bounds.add(Decimal(str(case)))
            if bounds and used > max(bounds) * Decimal("1.03"):
                failures.append((product.id, "estimate exceeds independent bounds", unit, str(used), sorted(map(str, bounds))))
            agrees = any(abs(used / size - 1) <= Decimal(".03") for size in sizes)
            mismatches = [w for w in offer.warnings if w.code == "SIZE_MISMATCH"]
            case = product.sales_volume_l if unit == "L" else product.sales_net_weight_kg
            case_agrees = case and any(abs(Decimal(str(case)) / size - 1) <= Decimal(".03") for size in sizes)
            if case_agrees and not agrees:
                failures.append((product.id, "valid CASE rejected", unit, str(used), sorted(map(str, sizes))))
            elif mismatches:
                # Contradictions must be explicit; never skip them as previously.
                text = " ".join(w.text for w in mismatches)
                if not (used > 0 and
                        f"{float(used):g}" in text and "name_case=" in text):
                    failures.append((product.id, "contradiction not visible", unit, str(used), text))
            elif not agrees:

                failures.append((product.id, product.name_en, unit, str(used), sorted(map(str, sizes))))
    assert not failures, f"{len(failures)} failures / {len(real_case_name_products)} products: {failures[:30]}"


@pytest.mark.parametrize("stored", ["current", "legacy_false", "legacy_missing"])
@pytest.mark.parametrize("comparison", [False, True])
def test_review_case_inner_bottle_size(gateway, need, upstream, tmp_path, respx_mock,
                                     actual_product_rows, stored, comparison):
    row = next(r for r in actual_product_rows if str(r["id"]) == "477242")
    rebuild_catalog(gateway, tmp_path, [row])
    product = gateway.catalog.get("477242")
    # Reproduce the earlier persisted catalog; no rebuild or new required fields may be needed.
    if stored != "current":
        data = product.model_dump()
        for field in ("size_suspect", "volume_suspect", "weight_suspect"):
            if stored == "legacy_missing":
                data.pop(field)
            else:
                data[field] = False
        with gateway.db.connect(write=True) as conn:
            conn.execute("UPDATE products SET data=? WHERE id=?", (json.dumps(data), product.id))
    request = need.model_copy(update={"intent": "Brooklyn beer", "search_terms": ["Brooklyn"],
        "required_claims": [], "exclude_allergens": [], "quantity": Quantity(value=7.92, unit="L")})

    def price(request):
        body = upstream.price_response(request).json()
        for item in body["products"]:
            if comparison:
                item["comparisonPrice"] = {"value": 13 / 7.92}
            else:
                item.pop("comparisonPrice")
        return httpx.Response(200, json=body)

    respx_mock.post("https://valio.test/api/price").mock(side_effect=price)
    result = gateway.find_offers(request)
    offer, plan = result.offers[0], result.plans[0]
    assert (offer.size_per_sales_unit, offer.units_needed, offer.purchase_total) == (7.92, 1, 13)
    assert (offer.overbuy.amount, plan.lines[0].units, plan.total, plan.coverage.value) == (0, 1, 13, 7.92)
    assert offer.size_source == "name_parse" and offer.size_confirmed_by_price is comparison
    quote = gateway.orders.create_quote(quote_request(request, qty=plan.lines[0].units,
        product_id=product.id, need_ref=result.lookup_id), "external")
    assert (quote.lines[0].size_per_sales_unit, quote.lines[0].units_needed, quote.total) == (7.92, 1, 13)
    # Old lookup, quote and order JSON remains loadable after the reconciliation fix.
    order = gateway.orders.place_order(quote.id, f"bottle-{stored}-{comparison}", "external")
    assert gateway.lookup.get(result.lookup_id).offers[0].size_per_sales_unit == 7.92
    assert gateway.orders.get_quote(quote.id).total == 13
    assert gateway.orders.get_order(order.id).total == 13
    if stored == "current":
        assert product.volume_suspect


@pytest.mark.parametrize("legacy", [False, True])
def test_review_real_case_weight_once(gateway, need, upstream, tmp_path, respx_mock, actual_product_rows, legacy):
    row = next(r for r in actual_product_rows if str(r["id"]) == "470529")
    rebuild_catalog(gateway, tmp_path, [row])
    product = gateway.catalog.get("470529")
    assert not product.weight_suspect
    if legacy:
        # Older builds persisted the false positive. Reconciliation must recover without a rebuild.
        product.weight_suspect = product.size_suspect = True
        with gateway.db.connect(write=True) as conn:
            conn.execute("UPDATE products SET data=? WHERE id=?", (product.model_dump_json(), product.id))
    request = need.model_copy(update={"search_terms": ["ribs"], "exclude_allergens": [],
                                     "quantity": Quantity(value=100, unit="KG")})

    def price(request):
        body = upstream.price_response(request).json()
        for item in body["products"]:
            item.pop("comparisonPrice")
        return httpx.Response(200, json=body)

    respx_mock.post("https://valio.test/api/price").mock(side_effect=price)
    result = gateway.find_offers(request)
    offer = result.offers[0]
    assert (offer.size_per_sales_unit, offer.units_needed, offer.purchase_total) == (10, 10, 130)
    assert offer.size_source == "catalog" and not offer.size_confirmed_by_price
    assert "SIZE_UNCONFIRMED" in {w.code for w in offer.warnings}
    plan = result.plans[0]
    assert (plan.lines[0].units, plan.total, plan.coverage.value) == (10, 130, 100)
    quote = gateway.orders.create_quote(quote_request(request, qty=plan.lines[0].units,
        product_id=product.id, need_ref=result.lookup_id), "external")
    order = gateway.orders.place_order(quote.id, f"case-{legacy}", "external")
    assert order.status == "PENDING_APPROVAL"
    approved = gateway.orders.approve(order.id, "Ada")
    assert approved.status == "CONFIRMED" and approved.total == 130
    assert approved.lines[0].units_needed == 10


@pytest.mark.parametrize("field", ["max_candidates", "preferences.max_price_per_unit", "preferences.pack_size.min", "preferences.pack_size.max"])
@pytest.mark.parametrize("value", [True, "5"])
@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_review_strict_preference_numbers(gateway, need, upstream, field, value, transport):
    body = need.model_dump(mode="json")
    target = body
    parts = field.split(".")
    for part in parts[:-1]:
        if target.get(part) is None:
            target[part] = {}
        target = target.setdefault(part, {})
    target[parts[-1]] = value
    if transport == "rest":
        with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
            response = client.post("/v1/offers", json=body)
        assert response.status_code == 422
        assert field in response.json()["message"]
    else:
        async def check():
            error = await create_server(gateway, "external").call_tool("find_offers", {"need": body})
            assert error.is_error
            assert error.structured_content["code"] == "VALIDATION_ERROR"
            assert field in error.structured_content["message"]
        asyncio.run(check())
    assert not upstream.price_calls and not upstream.decision_calls


@pytest.mark.parametrize("unit,field,catalog_unit", [("L", "volume", "LTR"), ("KG", "netWeight", "KGM")])
def test_review_case_content_needs_no_multiplier(unit, field, catalog_unit):
    from app.lookup import reconcile_size
    row = raw_product("case", "Milk case", sales="KI", base="ST")
    row["units"] = []
    row["synkkaData"]["unitConversions"] = [{"synkkaPackagingType": "CASE", field: {"value": 6, "unit": catalog_unit}}]
    product = extract(row, {}, {})
    assert not product.size_suspect
    size, source, confirmed, warnings = reconcile_size(product, {}, unit)
    assert (size, source, confirmed) == (6, "catalog", False)
    assert {w.code for w in warnings} == {"SIZE_UNCONFIRMED"}


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("price", [{}, {"catalogPrice": {"value": 40}, "comparisonPrice": {"value": 2}, "comparisonUnit": "L"}])
def test_review_bad_volume_preserves_valid_weight(actual_product_rows, legacy, price):
    from app.lookup import reconcile_size
    from app.models import Product
    product = extract(next(r for r in actual_product_rows if str(r["id"]) == "466324"), {}, {})
    assert product.size_suspect
    if legacy:
        old = product.model_dump(exclude={"volume_suspect", "weight_suspect"})
        product = Product.model_validate(old)
    size, source, confirmed, warnings = reconcile_size(product, price, "KG")
    assert (size, source, confirmed) == (20, "catalog", False)
    assert {w.code for w in warnings} == {"SIZE_UNCONFIRMED"}


def allergen_row(pid, entries=(), free=()):
    row = raw_product(pid, f"Milk {pid} 1 L", multiplier=1, volume=1)
    for group in row["synkkaData"]["classifications"]:
        if group["name"] == "allergen":
            group["values"] = [{"id": code, "unit": status} for code, status in entries]
        elif group["name"] == "nonAllergen":
            group["values"] = [{"id": code, "unit": "FREE_FROM"} for code in free]
    return row


def codes(offer):
    return {w.code for w in offer.warnings}


@pytest.mark.parametrize("packaging", ["BASE_UNIT_OR_EACH", "CASE"])
@pytest.mark.parametrize("unit,field,catalog_unit", [("L", "volume", "LTR"), ("KG", "netWeight", "KGM")])
@pytest.mark.parametrize("stored", ["current", "legacy_false", "legacy_missing"])
def test_review_finnish_size_evidence(gateway, tmp_path, packaging, unit, field, catalog_unit, stored):
    from app.lookup import reconcile_size
    row = raw_product("localized", "Milk", multiplier=2)
    row["synkkaData"]["names"][1]["value"] = f"Maito 1 {'l' if unit == 'L' else 'kg'}"
    row["synkkaData"]["unitConversions"] = [
        {"synkkaPackagingType": packaging, field: {"value": 20 if packaging == "CASE" else 10, "unit": catalog_unit}}]
    rebuild_catalog(gateway, tmp_path, [row])
    product = gateway.catalog.get("localized")
    if stored == "current":
        assert product.size_suspect
        assert getattr(product, "volume_suspect" if unit == "L" else "weight_suspect")
    else:
        data = product.model_dump()
        for key in ("size_suspect", "volume_suspect", "weight_suspect"):
            if stored == "legacy_missing":
                data.pop(key)
            else:
                data[key] = False
        with gateway.db.connect(write=True) as conn:
            conn.execute("UPDATE products SET data=? WHERE id=?", (json.dumps(data), product.id))
        product = gateway.catalog.get(product.id)
    size, source, confirmed, warnings = reconcile_size(product, {}, unit)
    assert (size, source, confirmed) == (2, "name_parse", False)
    assert {w.code for w in warnings} == {"SIZE_UNCONFIRMED"}


def test_review_ac14_requires_price_error_independently(gateway, need, upstream, monkeypatch):
    from app import models
    monkeypatch.setattr(models, "PROTECTED_WARNINGS", models.PROTECTED_WARNINGS - {"PRICE_ERROR"})
    # The AC test must detect removal of the spec's explicit PRICE_ERROR protection.
    with pytest.raises(AssertionError, match="PRICE_ERROR"):
        test_ac_14_policy_fails_closed_and_protected_warnings_cannot_be_ignored(gateway, need, upstream)


def mutate_response(respx_mock, upstream, endpoint, mutate):
    original = getattr(upstream, endpoint + "_response")

    def response(request):
        body = original(request).json()
        mutate(body)
        return httpx.Response(200, json=body)

    respx_mock.post(f"https://valio.test/api/{endpoint}").mock(side_effect=response)


def test_ac_1_exclusion_requires_declaration_or_uncontradicted_free_from(gateway, need, upstream, tmp_path):
    rows = [allergen_row("none"), allergen_row("milk", [("AM", "CONTAINS")]),
            allergen_row("free", free=["AE"]), allergen_row("may", [("AE", "MAY_CONTAIN")]),
            allergen_row("undeclared", [("AE", "UNDECLARED")]),
            allergen_row("conflict", [("AE", "CONTAINS")], ["AE"])]
    rebuild_catalog(gateway, tmp_path, rows)
    result = gateway.find_offers(need)
    assert {o.product_id for o in result.offers} == {"milk", "free"}
    assert result.excluded_count == 4
    assert sum(v for k, v in result.exclusion_counts.items() if "no allergen data" in k) == 1
    assert sum(v for k, v in result.exclusion_counts.items() if "may contain" in k) == 1
    assert "undeclared" in str(result.exclusion_counts)
    by_id = {o.product_id: " ".join(o.matched_because) for o in result.offers}
    assert "AE not listed in the catalog allergen declaration" in by_id["milk"]
    assert "AE declared free-from (catalog nonAllergen)" in by_id["free"]
    rebuild_catalog(gateway, tmp_path, [rows[0], rows[3]])
    assert "no allergen data" in gateway.find_offers(need).no_match_reason


def test_ac_2_x99_is_only_a_declaration_marker(gateway, need, upstream, tmp_path):
    rebuild_catalog(gateway, tmp_path, [allergen_row("x99", [("X99", "CONTAINS")]),
                                      allergen_row("egg", [("AE", "CONTAINS")])])
    product = gateway.catalog.get("x99")
    assert product.has_allergen_declaration
    assert "X99" not in product.allergen_status and "X99" not in product.allergens_contains
    result = gateway.find_offers(need)
    assert [o.product_id for o in result.offers] == ["x99"]
    assert "AE not listed" in " ".join(result.offers[0].matched_because)


def test_ac_3_claim_contradictions_are_not_proof(gateway, need, upstream, tmp_path):
    rows = [allergen_row("contradicted", [("ML", "CONTAINS"), ("AE", "UNDECLARED")])]
    rebuild_catalog(gateway, tmp_path, rows)
    result = gateway.find_offers(need.model_copy(update={"exclude_allergens": []}))
    assert not result.offers and "claim contradicted by allergen data" in result.no_match_reason
    from app.catalog import CLAIM_CONTRADICTIONS
    for claim, allergens in CLAIM_CONTRADICTIONS.items():
        for allergen in allergens:
            for status in ("contains", "may_contain"):
                product = gateway.catalog.get("contradicted").model_copy(update={
                    "claims": [claim], "allergen_status": {allergen: [status]}})
                assert "claim contradicted" in hard_violations(product, [claim], [])[0]


def test_ac_4_codes_normalize_and_unknowns_have_valid_values_rest_and_mcp(gateway, need, upstream):
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        body = need.model_dump(mode="json")
        body["exclude_allergens"] = [" ae "]
        body["required_claims"] = [" free_from_lactose "]
        body["preferences"]["labels"] = [" organic "]
        response = client.post("/v1/offers", json=body)
        assert response.status_code == 200
        assert response.json()["need"]["exclude_allergens"] == ["AE"]
        assert response.json()["need"]["preferences"]["labels"] == ["ORGANIC"]
        for field, value, valid in (("exclude_allergens", "EGG", "AE"),
                                    ("required_claims", "NOT_A_CLAIM", "FREE_FROM_LACTOSE"),
                                    ("preferences.labels", "MADE_UP", "ORGANIC"),
                                    ("quantity.unit", "BOTTLE", "SALES_UNIT")):
            invalid = copy.deepcopy(body)
            target = invalid
            parts = field.split(".")
            for part in parts[:-1]:
                target = target[part]
            target[parts[-1]] = value if field == "quantity.unit" else [value]
            refused = client.post("/v1/offers", json=invalid)
            assert refused.status_code == 422 and refused.json()["code"] == "UNKNOWN_CODE"
            assert field in refused.json()["message"] and valid in refused.json()["message"]

            async def check():
                error = await create_server(gateway, "external").call_tool("find_offers", {"need": invalid})
                assert error.is_error
                assert error.structured_content["code"] == "UNKNOWN_CODE"
            asyncio.run(check())


def test_ac_5_request_bounds_rest_and_mcp_match(gateway, need, upstream):
    today = audit.utcnow().astimezone(ZoneInfo("Europe/Helsinki")).date()
    base = need.model_dump(mode="json")
    cases = []
    for value in ((today - timedelta(days=1)).isoformat(), (today + timedelta(days=61)).isoformat()):
        cases.append(("deliver_by", {**base, "deliver_by": value}))
    for value in (0, -1, 2e6, True, "2"):
        cases.append(("quantity.value", {**base, "quantity": {"value": value, "unit": "L"}}))
    missing = dict(base)
    missing.pop("customer_id")
    cases.extend([("customer_id", missing), ("customer_id", {**base, "customer_id": " "}),
                  ("brands", {**base, "preferences": {"brands": [" "]}}),
                  ("pack_size", {**base, "preferences": {"pack_size": {"min": 2, "max": 1}}})])
    server = create_server(gateway, "external")
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        for field, body in cases:
            response = client.post("/v1/offers", json=body)
            assert response.status_code == 422 and field in response.json()["message"]

            # MCP adds the outer argument name, while preserving the same code and field details.
            async def matching():
                error = await server.call_tool("find_offers", {"need": body})
                assert error.is_error
                data = error.structured_content
                assert data["code"] == response.json()["code"] == "VALIDATION_ERROR"
                assert field in data["message"]
            asyncio.run(matching())
        app = create_server(gateway, "external").streamable_http_app(stateless_http=True, json_response=True)
        with TestClient(app, base_url="http://localhost:8001", headers=agent_headers("external")) as mcp:
            response = mcp.post("/mcp", headers={"Accept": "application/json, text/event-stream"}, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "find_offers", "arguments": {"need": missing}}})
            result = response.json()["result"]
            assert result["isError"] and json.loads(result["content"][0]["text"])["code"] == "VALIDATION_ERROR"
    for value in (0, -1, 2e6, True, "2"):
        with pytest.raises(ValidationError, match="quantity_sales_units"):
            QuoteLineRequest(product_id="1", quantity_sales_units=value)
    assert Quantity(value=1_000_000, unit="L").value == 1_000_000
    # A factual fit question must not invent hard filters for a constraint-free need.
    gateway.find_offers(need)
    assert "hard constraints are already verified" in upstream.decision_calls[-1]["questions"][0]["instructions"]
    gateway.find_offers(need.model_copy(update={"required_claims": [], "exclude_allergens": []}))
    assert "hard constraints are already verified" not in upstream.decision_calls[-1]["questions"][0]["instructions"]


def test_ac_6_invalid_prices_and_missing_currency_cannot_be_quoted(gateway, need, upstream, respx_mock):
    lookup = gateway.find_offers(need)
    for value in (0, -1, None):
        def mutate(body):
            for row in body["products"]:
                row["catalogPrice"]["value"] = value
        mutate_response(respx_mock, upstream, "price", mutate)
        gateway.valio.cache.clear()
        assert all("PRICE_ERROR" in codes(o) for o in gateway.find_offers(need).offers)
        with pytest.raises(DomainError, match="PRICE_ERROR"):
            gateway.orders.create_quote(quote_request(need, qty=1, need_ref=lookup.lookup_id))
    def no_currency(body):
        body.pop("currency")
        for row in body["products"]:
            row["catalogPrice"].pop("currency")
    mutate_response(respx_mock, upstream, "price", no_currency)
    gateway.valio.cache.clear()
    assert all(o.currency is None and "PRICE_ERROR" in codes(o) for o in gateway.find_offers(need).offers)
    with pytest.raises(DomainError, match="PRICE_ERROR"):
        gateway.orders.create_quote(quote_request(need, qty=1, need_ref=lookup.lookup_id))
    p = gateway.catalog.get("1").model_copy(update={"variable_unit": True, "deposits": [{"material": "3", "quantity": 12}]})
    offer = offer_for(p, need, {}, {})
    assert {"VARIABLE_WEIGHT", "DEPOSIT"} <= codes(offer)
    assert "delivered weight" in next(w.text for w in offer.warnings if w.code == "VARIABLE_WEIGHT").lower()
    assert "12" in next(w.text for w in offer.warnings if w.code == "DEPOSIT")
    raw = raw_product("weighed", "Milk weighed goods")
    raw["synkkaData"]["variableUnit"] = True
    raw["deposits"] = [{"material": "3", "quantity": 12}]
    extracted = extract(raw, {}, {})
    assert extracted.variable_unit and extracted.deposits == raw["deposits"]


def test_ac_7_unconfirmed_size_requires_plan_approval(gateway, need, upstream, respx_mock):
    mutate_response(respx_mock, upstream, "price", lambda b: [r.pop("comparisonPrice") for r in b["products"]])
    result = gateway.find_offers(need.model_copy(update={"quantity": Quantity(value=5, unit="L")}), actor="agent:web-console")
    assert "SIZE_UNCONFIRMED" in codes(next(o for o in result.offers if o.product_id == "1"))
    assert result.plans and all(p.policy_preview.outcome == "PENDING_APPROVAL" for p in result.plans)
    offer = next(o for o in result.offers if o.product_id == "1")
    assert offer.name_size_candidates and all(c.source_text for c in offer.name_size_candidates)
    quote = gateway.orders.create_quote(quote_request(result.need, qty=offer.units_needed,
        need_ref=result.lookup_id), "web-console")
    assert gateway.orders.place_order(quote.id, "ac7-unconfirmed-size", "web-console").status == "PENDING_APPROVAL"
    mutate_response(respx_mock, upstream, "price", lambda b: None)
    gateway.valio.cache.clear()
    confirmed = next(o for o in gateway.find_offers(need).offers if o.product_id == "1")
    assert confirmed.size_confirmed_by_price and "SIZE_UNCONFIRMED" not in codes(confirmed)


def test_ac_8_suspect_catalog_size_uses_name_and_case_units_convert(gateway, need, upstream, tmp_path, respx_mock):
    row = raw_product("suspect", "Soy sauce milk 20 l", volume=20_000, multiplier=1)
    other = raw_product("egg", "Egg vocabulary", egg=True)
    rebuild_catalog(gateway, tmp_path, [row, other])
    assert gateway.catalog.get("suspect").size_suspect
    mutate_response(respx_mock, upstream, "price", lambda b: [r.pop("comparisonPrice") for r in b["products"]])
    offer = gateway.find_offers(need).offers[0]
    assert offer.product_id == "suspect" and offer.size_source == "name_parse" and offer.size_per_sales_unit == 20
    assert "SIZE_UNCONFIRMED" in codes(offer)
    for value, unit in ((1000, "MLT"), (100, "CLT")):
        case = raw_product("case", "Milk 1 l", multiplier=12)
        case["synkkaData"]["unitConversions"] = [
            {"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"value": value, "unit": unit}},
            {"synkkaPackagingType": "CASE", "volume": {"value": value * 12, "unit": unit}}]
        product = extract(case, {}, {})
        assert product.base_volume_l == 1 and product.sales_volume_l == 12
        assert offer_for(product, need, {}, {}).size_per_sales_unit == 12
    for weight in (.5, 100_001):
        assert extract(raw_product("w", "Milk mass", weight=weight), {}, {}).size_suspect


def test_ac_9_schedule_unit_mismatch_counts_zero(gateway, need, upstream, respx_mock):
    for sales_unit in ("ST", None):
        mutate_response(respx_mock, upstream, "availability", lambda b: [
            line.update(salesUnit=sales_unit) for r in b["products"] for line in r["scheduleLines"]])
        gateway.valio.cache.clear()
        offer = next(o for o in gateway.find_offers(need).offers if o.product_id == "1")
        assert "UNIT_MISMATCH" in codes(offer)
        assert offer.availability.available_amount == offer.availability.coverage == 0
        assert offer.rank_label is None


def test_ac_10_unknown_stock_is_finite_and_no_stock_rejects(gateway, need, upstream, respx_mock):
    upstream.model, upstream.amount = None, 10
    lookup = gateway.find_offers(need, actor="agent:web-console")
    quote = gateway.orders.create_quote(quote_request(need, qty=6, need_ref=lookup.lookup_id), "web-console")
    order = gateway.orders.place_order(quote.id, "unknown-model", "web-console")
    assert order.status == "PENDING_APPROVAL" and "STOCK_MODEL_UNKNOWN" in codes(order.lines[0])
    gateway.valio.cache.clear()
    offer = next(o for o in gateway.find_offers(need).offers if o.product_id == "1")
    assert offer.availability.available_amount == 4
    upstream.model, upstream.amount = "Stock", 0
    mutate_response(respx_mock, upstream, "availability", lambda b: [line.pop("availableDate")
                    for row in b["products"] for line in row["scheduleLines"]])
    quote = gateway.orders.create_quote(quote_request(need, qty=1, need_ref=lookup.lookup_id), "web-console")
    rejected = gateway.orders.place_order(quote.id, "no-stock", "web-console")
    assert "NO_STOCK" in codes(rejected.lines[0]) and rejected.status == "REJECTED"


def test_ac_11_missing_cutoff_needs_approval_and_naive_time_is_helsinki(gateway, need, upstream, respx_mock):
    mutate_response(respx_mock, upstream, "availability", lambda b: [r.pop("closingTime") for r in b["products"]])
    lookup = gateway.find_offers(need, actor="agent:web-console")
    quote = gateway.orders.create_quote(quote_request(need, qty=1, need_ref=lookup.lookup_id), "web-console")
    order = gateway.orders.place_order(quote.id, "no-cutoff", "web-console")
    assert order.status == "PENDING_APPROVAL" and "CUTOFF_UNKNOWN" in codes(order.lines[0])
    for raw_time in (None, "bad-time"):
        raw = {"closingTime": raw_time, "logisticModel": "Terminal"}
        assert "CUTOFF_UNKNOWN" in {w.code for w in availability_for(raw, need.deliver_by, 1)[1]}
    raw = {"closingTime": "2026-11-14T12:00:00", "_fetched_at": "2026-11-14T10:30:00Z", "logisticModel": "Terminal"}
    available, warnings = availability_for(raw, need.deliver_by, 1)
    assert available.closing_time.utcoffset() == timedelta(hours=2)
    assert "CUTOFF_PASSED" in {w.code for w in warnings}
    # A mandate edited after placement must be checked again during approval.
    body = gateway.policy.get().model_dump(mode="json")
    body["agents"]["web-console"]["customer_ids"] = ["2"]
    gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Ada"))
    with pytest.raises(DomainError) as error:
        gateway.orders.approve(order.id, "Ada")
    assert any(h["rule"] == "CUSTOMER_NOT_IN_MANDATE" for h in error.value.data["rule_hits"])
    assert gateway.orders.reject(order.id, "Ada").status == "REJECTED"


def test_ac_12_unorderable_offers_have_no_rank_label_and_plans_use_full_pool(gateway, need, upstream):
    offers = gateway.find_offers(need).offers
    valid = next(o for o in offers if o.product_id == "1")
    bad = valid.model_copy(deep=True)
    bad.product_id, bad.purchase_total = "cheap-bad", .01
    from app.lookup import warning
    bad.warnings = [warning("UNKNOWN_PRODUCT")]
    ranked = [bad, valid]
    rank(ranked, gateway.policy.weights, catalog_stats=gateway.catalog.metadata("ranking"))
    assert ranked[0] == valid and bad.rank_label is None
    upstream.model, upstream.amount = "Stock", 0
    upstream.amounts = {"1": 100, "4": 50}
    need.max_candidates = 1
    gateway.valio.cache.clear()
    result = gateway.find_offers(need, actor="agent:web-console")
    assert len(result.offers) == 1
    split = next(p for p in result.plans if p.kind == "split")
    assert {l.product_id for l in split.lines} == {"1", "4"}
    quote = gateway.orders.create_quote(QuoteRequest(
        customer_id="1", deliver_by=need.deliver_by,
        plan_ref={"lookup_id": result.lookup_id, "plan_index": result.plans.index(split)}), "web-console")
    assert all(line.fit is not None for line in quote.lines)


def test_ac_13_missing_ranking_data_uses_global_median_and_fit_missing_sorts_last(gateway, need, upstream):
    base = gateway.find_offers(need).offers[0]
    zero = base.model_copy(deep=True)
    zero.product_id, zero.delivery_reliability = "zero", 0
    missing = base.model_copy(deep=True)
    missing.product_id, missing.delivery_reliability = "missing", None
    weights = {key: int(key == "reliability") for key in gateway.policy.weights}
    offers = [zero, missing]
    rank(offers, weights, catalog_stats=gateway.catalog.metadata("ranking"))
    assert offers[0] == missing and missing.score_breakdown["reliability"] == .97
    assert "reliability: no data (neutral)" in missing.matched_because
    zero.delivery_reliability = .3
    rank([zero], weights, catalog_stats=gateway.catalog.metadata("ranking"))
    assert zero.score == .3
    missing.fit = None
    rank(offers, weights, catalog_stats=gateway.catalog.metadata("ranking"))
    assert offers[0] == zero
    co2 = {key: int(key == "co2") for key in weights}
    zero.co2_kg_per_kg, missing.co2_kg_per_kg, missing.fit = 10, None, zero.fit
    rank(offers, co2, catalog_stats={"co2": {"min": 0, "max": 20, "median": 5}})
    assert missing.score == .75 and zero.score == .5


def test_ac_14_policy_fails_closed_and_protected_warnings_cannot_be_ignored(gateway, need, upstream):
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        policy = client.get("/v1/policy").json()
        policy["approval"]["warnings_need_approval"] = []
        assert client.put("/v1/policy", json={"policy": policy, "updated_by": "Ada"}).status_code == 200
        offer = gateway.find_offers(need).offers[0]
        from app.lookup import warning
        from app.models import Warning
        for code in ("PARTIAL_STOCK", "FUTURE_UNKNOWN_WARNING"):
            offer.warnings = [Warning(code=code, text="Evidence needs review")]
            assert gateway.policy.evaluate(10, [offer]).outcome == "PENDING_APPROVAL"
        # Literal spec15 R-14 requirements: do not derive the oracle from production validation.
        for code in ("PRICE_ERROR", "AVAILABILITY_ERROR", "SIZE_UNKNOWN", "SIZE_UNCONFIRMED",
                     "UNIT_MISMATCH", "STOCK_MODEL_UNKNOWN", "NO_FIT_SCORE", "CUTOFF_UNKNOWN"):
            body = copy.deepcopy(policy)
            body["approval"]["ignored_warnings"] = [code]
            assert client.put("/v1/policy", json={"policy": body, "updated_by": "Ada"}).status_code == 422, code
        policy["reject"]["on_hard_constraint_violation"] = False
        assert client.put("/v1/policy", json={"policy": policy, "updated_by": "Ada"}).status_code == 200
        offer.warnings = []
        assert gateway.policy.evaluate(10, [offer], ["hard violation"]).outcome == "PENDING_APPROVAL"
        offer.warnings = [warning("VARIABLE_WEIGHT"), warning("DEPOSIT")]
        assert gateway.policy.evaluate(10, [offer]).outcome == "CONFIRMED"
    legacy = gateway.policy.get().model_dump(mode="json")
    legacy["approval"].pop("ignored_warnings")
    legacy["approval"]["warnings_need_approval"] = []
    legacy["reject"]["on"] = []
    import yaml
    gateway.policy.runtime_path.write_text(yaml.safe_dump(legacy))
    loaded = gateway.policy.get()
    assert {"SIZE_UNCONFIRMED", "UNIT_MISMATCH", "STOCK_MODEL_UNKNOWN", "CUTOFF_UNKNOWN"} <= set(loaded.approval.warnings_need_approval)
    assert "NO_STOCK" in loaded.reject.on


def test_ac_15_build_fails_loudly_skips_deleted_and_reports_coverage(gateway, tmp_path):
    missing = tmp_path / "missing-carbon.csv"
    with pytest.raises(ValueError, match="missing-carbon.csv"):
        build(gateway.db, tmp_path / "products.json.gz", tmp_path / "reliability.csv", missing)
    for name in ("reliability.csv", "carbon.csv"):
        path = tmp_path / name
        original = path.read_text()
        path.write_text("productId,renamedColumn\n1,1\n")
        with pytest.raises(ValueError, match="missing column"):
            build(gateway.db, tmp_path / "products.json.gz", tmp_path / "reliability.csv", tmp_path / "carbon.csv")
        path.write_text(original)
    rows = [raw_product("keep", "Milk 1 L"), {**raw_product("deleted", "Milk 1 L"), "deleted": True}]
    report = rebuild_catalog(gateway, tmp_path, rows)
    assert report["rows"] == report["deleted_skipped"] == 1
    with pytest.raises(DomainError, match="PRODUCT_NOT_FOUND"):
        gateway.catalog.get("deleted")
    from app.models import Product
    assert set(report["coverage"]) == set(Product.model_fields)
    assert all(v["filled"] + v["empty"] == 1 for v in report["coverage"].values())
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        assert client.get("/health").json()["coverage"] == report["coverage"]
    command = [sys.executable, "-m", "app.catalog", "build", "--db", str(tmp_path / "cli.db"),
               "--source", str(tmp_path / "products.json.gz"), "--reliability", str(tmp_path / "reliability.csv"),
               "--carbon", str(tmp_path / "carbon.csv")]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0
    assert "coverage" in json.loads(result.stdout)
    result = subprocess.run([*command[:-1], str(missing)], capture_output=True, text=True)
    assert result.returncode == 1 and "missing-carbon.csv" in result.stderr


def test_demo_smoke_still_buys_464306_for_1010(gateway, need, upstream, tmp_path):
    demo_catalog(gateway, tmp_path, upstream)
    result = gateway.find_offers(need, actor="agent:web-console")
    best = result.offers[0]
    assert best.product_id == "464306" and best.purchase_total == best.total == 1010
    quote = gateway.orders.create_quote(quote_request(need, qty=1000, product_id="464306", need_ref=result.lookup_id), "web-console")
    assert gateway.orders.place_order(quote.id, "demo-evidence", "web-console").status == "CONFIRMED"


def test_ac_16_full_catalog_replays_recorded_demo(gateway, need, upstream, respx_mock):
    from conftest import replay_recorded_demo
    replay_recorded_demo(gateway, need, respx_mock)


@pytest.mark.parametrize("pid,unit,size,requested,units", [
    ("462607", "KG", .15, 1.5, 10), ("464639", "KG", .9, 54, 60),
    ("460892", "KG", 10.8, 10.8, 1), ("477242", "L", 7.92, 7.92, 1),
    ("470979", "KG", 4, 4, 1), ("462191", "KG", 5, 5, 1),
])
@pytest.mark.parametrize("stored", [False, True])
def test_review_real_package_totals_through_approval(gateway, need, upstream, tmp_path, respx_mock,
                                                    actual_product_rows, pid, unit, size, requested, units, stored):
    row = next(r for r in actual_product_rows if str(r["id"]) == pid)
    rebuild_catalog(gateway, tmp_path, [row])
    product = gateway.catalog.get(pid)
    if stored:
        # Old catalogs lack dimension flags; retained content/names must still work.
        data = product.model_dump()
        for field in ("size_suspect", "volume_suspect", "weight_suspect"):
            data.pop(field)
        with gateway.db.connect(write=True) as conn:
            conn.execute("UPDATE products SET data=? WHERE id=?", (json.dumps(data), pid))
    request = need.model_copy(update={"intent": product.name_en, "search_terms": [],
        "required_claims": [], "exclude_allergens": [], "quantity": Quantity(value=requested, unit=unit)})
    upstream.sales_units[pid] = product.sales_unit
    def price(request):
        body = upstream.price_response(request).json()
        for item in body["products"]:
            item.pop("comparisonPrice")
        return httpx.Response(200, json=body)
    respx_mock.post("https://valio.test/api/price").mock(side_effect=price)
    result = gateway.find_offers(request)
    offer, plan = result.offers[0], result.plans[0]
    assert (offer.size_per_sales_unit, offer.units_needed, offer.total) == (size, units, units * 13)
    assert (plan.lines[0].units, plan.coverage.value, plan.total) == (units, requested, units * 13)
    assert "SIZE_UNCONFIRMED" in codes(offer)
    assert plan.policy_preview.outcome == "PENDING_APPROVAL"
    quote = gateway.orders.create_quote(QuoteRequest(customer_id=request.customer_id, deliver_by=request.deliver_by,
        plan_ref={"lookup_id": result.lookup_id, "plan_index": 0}), "external")
    assert (quote.lines[0].size_per_sales_unit, quote.lines[0].units_needed, quote.total) == (size, units, units * 13)
    order = gateway.orders.place_order(quote.id, f"review-total-{pid}-{stored}", "external")
    assert order.status == "PENDING_APPROVAL"
    approved = gateway.orders.approve(order.id, "Ada")
    assert (approved.status, approved.total, approved.lines[0].units_needed) == ("CONFIRMED", units * 13, units)
    assert gateway.lookup.get(result.lookup_id).offers[0].size_per_sales_unit == size
    assert gateway.orders.get_quote(quote.id).total == gateway.orders.get_order(order.id).total == units * 13
