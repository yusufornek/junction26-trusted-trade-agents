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
            with pytest.raises(ToolError) as error:
                await create_server(gateway, "external").call_tool("find_offers", {"need": body})
            assert json.loads(str(error.value))["code"] == "VALIDATION_ERROR"
            assert field in json.loads(str(error.value))["message"]
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
                with pytest.raises(ToolError) as error:
                    await create_server(gateway, "external").call_tool("find_offers", {"need": invalid})
                assert json.loads(str(error.value))["code"] == "UNKNOWN_CODE"
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
                with pytest.raises(ToolError) as error:
                    await server.call_tool("find_offers", {"need": body})
                data = json.loads(str(error.value))
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
        case = raw_product("case", "Milk 1 l", multiplier=10)
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
        from app.models import PROTECTED_WARNINGS, Warning
        for code in ("PARTIAL_STOCK", "FUTURE_UNKNOWN_WARNING"):
            offer.warnings = [Warning(code=code, text="Evidence needs review")]
            assert gateway.policy.evaluate(10, [offer]).outcome == "PENDING_APPROVAL"
        for code in PROTECTED_WARNINGS:
            body = copy.deepcopy(policy)
            body["approval"]["ignored_warnings"] = [code]
            assert client.put("/v1/policy", json={"policy": body, "updated_by": "Ada"}).status_code == 422
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
    """Offline replay of captured evidence; live acceptance remains a separate manual check."""
    from pathlib import Path
    from app.config import ROOT
    from app.models import NeedSpec
    capture = json.loads((Path(__file__).parent / "fixtures/demo_lookup_capture.json").read_text())
    recorded = {o["product_id"]: o for o in capture["offers"]}
    report = build(gateway.db)  # Real organiser catalog and its reliability/CO2 files, isolated fixture DB.
    assert report["rows"] == 17546
    actual = gateway.catalog.get("464306")
    assert actual.gtin == "6408430000487" and actual.base_volume_l == 1
    assert actual.name_en == recorded["464306"]["name"]
    now = audit.utcnow()
    body = {**capture["need"], "deliver_by": need.deliver_by.isoformat()}
    request = NeedSpec.model_validate(body)

    def decisions(request):
        questions = json.loads(request.content)["questions"]
        return httpx.Response(200, json={"answers": [
            {"type": "predicate", "name": q["name"], "probability": recorded[q["name"][4:]]["fit"]}
            for q in questions if q["name"][4:] in recorded]})

    def price(request):
        ids = json.loads(request.content)["products"]
        rows = []
        for pid in ids:
            if pid not in recorded:
                continue  # No invented price evidence for candidates absent from the capture.
            offer = recorded[pid]
            rows.append({"productId": pid, "quantity": 1, "error": False,
                "netPriceFound": offer["net_price_found"],
                "catalogPrice": {"value": offer["unit_price"], "currency": "EUR",
                    "valueWithTax": offer["unit_price_with_tax"], "validFrom": now.isoformat(),
                    "validTo": (now + timedelta(days=30)).isoformat()},
                "comparisonPrice": {"value": offer["price_per_l_or_kg"]}, "comparisonUnit": "L"})
        return httpx.Response(200, json={"currency": "EUR", "products": rows})

    def availability(request):
        ids = json.loads(request.content)["products"]
        return httpx.Response(200, json={"timeNow": now.isoformat(), "products": [
            {"productId": pid, "closingTime": (now + timedelta(hours=1)).isoformat(), "salesBlock": False,
             "logisticModel": recorded[pid]["availability"]["logistic_model"], "scheduleLines": [
                 {"availableAmount": recorded[pid]["availability"]["available_amount"],
                  "availableDate": need.deliver_by.isoformat(), "salesUnit": recorded[pid]["sales_unit"], "salesBlock": False}]}
            for pid in ids if pid in recorded]})

    respx_mock.post("https://api.openai.com/v1/decisions").mock(side_effect=decisions)
    respx_mock.post("https://valio.test/api/price").mock(side_effect=price)
    respx_mock.post("https://valio.test/api/availability").mock(side_effect=availability)
    result = gateway.find_offers(request, actor="agent:web-console")
    assert result.matched_count > len(recorded) and result.screened_count == 40
    best = result.offers[0]
    assert (best.product_id, best.purchase_total, best.units_needed) == ("464306", 1010, 1000)
    quote = gateway.orders.create_quote(quote_request(request, qty=1000, product_id="464306", need_ref=result.lookup_id), "web-console")
    assert gateway.orders.place_order(quote.id, "full-catalog-demo", "web-console").status == "CONFIRMED"
