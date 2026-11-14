import gzip
import json
import sys
import threading
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import audit
from app.auth import Credentials
from app.catalog import build
from app.config import BACKEND, Settings
from app.models import NeedSpec, PolicyDocument, QuoteLineRequest, QuoteRequest
from app.policy import Policy
from app.service import Gateway

OPERATOR_TOKEN = "test-operator-token"
TEST_AGENT_IDS = ("external", "hotel-chain-agent", "web-console", "hotel-agent", "buyer", "other", "mcp-agent",
                  "outsider", "hotel", "observer", "agent-a", "agent-b", "unregistered")
AGENT_TOKENS = {agent: f"test-secret-{agent}" for agent in TEST_AGENT_IDS}
TEST_KEYS = Credentials(OPERATOR_TOKEN, ",".join(f"{agent}:{token}" for agent, token in AGENT_TOKENS.items()))
OPERATOR_HEADERS = {"Authorization": f"Bearer {OPERATOR_TOKEN}"}


class PacerClock:
    """Advance supplier pacing deterministically without wall-clock sleeps."""
    def __init__(self):
        self.now = 1000.0
        self.start = audit.utcnow()
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.now

    def sleep(self, seconds):
        with self.lock:
            self.now += max(seconds, 1e-9)

    def utcnow(self):
        return self.start + timedelta(seconds=self() - 1000)


@pytest.fixture(autouse=True)
def upstream_pacer(monkeypatch):
    from app import valio
    clock = PacerClock()
    pacer = valio.UpstreamPacer(clock=clock, sleep=clock.sleep, wall_clock=clock.utcnow)
    monkeypatch.setattr(valio, "UPSTREAM_PACER", pacer)
    return pacer


def agent_headers(agent_id):
    return {"Authorization": f"Bearer {AGENT_TOKENS[agent_id]}"}


def raw_product(pid, name, *, lactose=True, egg=False, volume=None, weight=None, sales="KI", base="ST", multiplier=10, brand="Fixture"):
    conversion = {"synkkaPackagingType": "BASE_UNIT_OR_EACH"}
    if volume is not None:
        conversion["volume"] = {"unit": "LTR", "value": volume}
    if weight is not None:
        conversion["netWeight"] = {"unit": "GRM", "value": weight}
    return {
        "id": pid, "salesUnit": sales, "baseUnit": base, "category": "10400", "allowedLotSize": 0.01,
        "vendorName": "Fixture dairy", "countryOfOrigin": "fi", "temperatureCondition": 4,
        "units": [{"unitId": sales, "sizeInBaseUnits": multiplier}],
        "synkkaData": {"names": [{"language": "en", "value": name}, {"language": "fi", "value": "maito " + name}],
            "brand": brand, "keyIngredients": [{"language": "en", "value": "milk"}],
            "marketingTexts": [{"language": "en", "value": "coffee and breakfast"}],
            "classifications": [
                {"name": "nutritionalClaim", "values": [{"id": "FREE_FROM_LACTOSE"}] if lactose else []},
                {"name": "allergen", "values": [{"id": "AM", "unit": "CONTAINS"}]
                 + ([{"id": "AE", "unit": "CONTAINS"}] if egg else [])},
                {"name": "nonAllergen", "values": [{"id": "ML", "unit": "FREE_FROM"}]},
                {"name": "packagingMarkedLabel", "values": [{"id": "ORGANIC"}]}],
            "unitConversions": [conversion]},
    }


@pytest.fixture
def registered_agents(request):
    """Explicit test-only mandates; production and mandate tests keep unregistered callers untrusted."""
    return getattr(request, "param", ("external",))


@pytest.fixture
def product_rows():
    return [raw_product("1", "Milk 5 dl lactose free"),
            raw_product("2", "Milk conventional", lactose=False),
            raw_product("3", "Milk egg mix", egg=True),
            raw_product("4", "Milk catalog 1L", volume=1),
            raw_product("5", "Milk powder 500 g", weight=500),
            raw_product("6", "Milk unknown size"),
            raw_product("7", "Milk bulk", sales="KG", base="KG", multiplier=1)]


@pytest.fixture
def gateway(tmp_path, registered_agents, product_rows):
    source = tmp_path / "products.json.gz"
    with gzip.open(source, "wt") as f:
        json.dump(product_rows, f)
    reliability = tmp_path / "reliability.csv"
    reliability.write_text("productId,deliveryReliability\n1,0.97\n")
    carbon = tmp_path / "carbon.csv"
    carbon.write_text("productId,AVG kg CO2-eq/kg\n1,1.2\n")
    policy_path = BACKEND / "policy.yaml"
    if registered_agents:
        defaults = Policy._load(policy_path).model_dump(mode="json", include=set(PolicyDocument.model_fields))
        for agent_id in registered_agents:
            defaults["agents"][agent_id] = {"customer_ids": ["1"], "max_auto_daily_total_eur": 5000,
                                            "note": "Explicit fixture mandate for existing lifecycle tests."}
        policy_path = tmp_path / "defaults.yaml"
        policy_path.write_text(yaml.safe_dump(defaults))
    service = Gateway(Settings(db_path=tmp_path / "app.db", policy_path=policy_path,
                               valio_api_base="https://valio.test", valio_api_key="test-key", openai_api_key="test-key",
                               keys=TEST_KEYS))
    build(service.db, source, reliability, carbon)
    yield service
    service.close()


@pytest.fixture
def need():
    return NeedSpec(customer_id="1", intent="lactose-free milk", search_terms=["milk"], required_claims=["FREE_FROM_LACTOSE"],
                    exclude_allergens=["AE"], quantity={"value": 1000, "unit": "L"},
                    deliver_by=audit.utcnow().date() + timedelta(days=3))


class Upstream:
    def __init__(self, need):
        self.need = need
        self.price_calls = []
        self.availability_calls = []
        self.decision_calls = []
        self.amount = 999999
        self.model = "Terminal"
        self.shift = 0
        self.cutoff = audit.utcnow() + timedelta(hours=1)
        self.sales_block = False
        self.unknown = False
        self.fit = 0.91
        self.price = 13.0
        self.price_error = False
        self.amounts = {}
        self.prices = {}
        self.sizes = {}
        self.fits = {}
        self.shifts = {}
        self.sales_units = {"7": "KG"}

    def price_response(self, request):
        body = json.loads(request.content)
        assert all(isinstance(p, str) for p in body["products"])
        assert request.headers["X-API-Key"] == "test-key"
        self.price_calls.append(body)
        return httpx.Response(200, json={"currency": "EUR", "products": [
            {"productId": pid, "quantity": 1.0, "catalogPrice": {
                "value": self.prices.get(pid, self.price), "valueWithTax": self.prices.get(pid, self.price) * 1.135, "currency": "EUR",
                "validFrom": "2020-01-01T00:00:00+0000", "validTo": "9999-12-31T00:00:00+0200"},
             # Product 4 has 10 L of catalog content; keep ordinary lifecycle fixtures consistent.
             "comparisonPrice": {"value": self.prices.get(pid, self.price) / self.sizes[pid] if pid in self.sizes
                                 else self.price / 10 if pid == "4" else 2.6},
             "comparisonUnit": "L", "netPriceFound": False, "error": self.price_error}
            for pid in body["products"]]})

    def availability_response(self, request):
        body = json.loads(request.content)
        assert all(isinstance(p, str) for p in body["products"])
        self.availability_calls.append(body)
        delivery = (self.need.deliver_by + timedelta(days=self.shift)).isoformat()
        return httpx.Response(200, json={"timeNow": audit.utcnow().isoformat(), "products": [
            {"productId": pid, "closingTime": self.cutoff.isoformat(), "salesBlock": self.sales_block,
             "logisticModel": self.model, "scheduleLines": [] if self.unknown else [
                 {"availableAmount": self.amounts.get(pid, self.amount),
                  "availableDate": (self.need.deliver_by + timedelta(days=self.shifts[pid])).isoformat() if pid in self.shifts else delivery,
                  "salesUnit": self.sales_units.get(pid, "KI"), "salesBlock": False}]}
            for pid in body["products"]]})

    def decisions_response(self, request):
        body = json.loads(request.content)
        self.decision_calls.append(body)
        return httpx.Response(200, json={"answers": [
            {"type": "predicate", "name": q["name"], "probability": self.fits.get(q["name"][4:], self.fit)} for q in body["questions"]]})


@pytest.fixture
def upstream(respx_mock, need):
    state = Upstream(need)
    respx_mock.post("https://valio.test/api/price").mock(side_effect=state.price_response)
    respx_mock.post("https://valio.test/api/availability").mock(side_effect=state.availability_response)
    respx_mock.post("https://api.openai.com/v1/decisions").mock(side_effect=state.decisions_response)
    # Each test calls only the subset needed for that operation.
    respx_mock.assert_all_called = False
    return state


def quote_request(need, *, qty=200, need_ref=None, product_id="1"):
    return QuoteRequest(customer_id=need.customer_id, deliver_by=need.deliver_by, need_ref=need_ref,
                        lines=[QuoteLineRequest(product_id=product_id, quantity_sales_units=qty)])


def rebuild_catalog(gateway, tmp_path, rows):
    """Rebuild only a fixture catalog, including vocabulary and global ranking statistics."""
    source = tmp_path / "products.json.gz"
    with gzip.open(source, "wt") as file:
        json.dump(rows, file)
    gateway.valio.cache.clear()
    return build(gateway.db, source, tmp_path / "reliability.csv", tmp_path / "carbon.csv")


def set_customers(gateway, customers):
    from app.models import PolicyUpdateRequest
    body = gateway.policy.get().model_dump(mode="json")
    body["customers"] = customers
    return gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Ada"))


def demo_catalog(gateway, tmp_path, upstream):
    """Recorded demo product identities/sizes with deterministic, synthetic price/stock responses."""
    rows = [raw_product("464306", "Valio Arki Eila Milk 1 l lactose free", sales="ST", volume=1, multiplier=1),
            raw_product("462626", "Valio coffee milk 5 dl lactose free", multiplier=10),
            raw_product("eggs", "Egg declaration vocabulary", egg=True)]
    rebuild_catalog(gateway, tmp_path, rows)
    upstream.sales_units["464306"] = "ST"
    upstream.prices.update({"464306": 1.01, "462626": 13})
    upstream.sizes.update({"464306": 1, "462626": 5})


@pytest.fixture(scope="session")
def actual_product_rows():
    """Unmodified organiser catalog, shared by ingestion regressions and demo replay."""
    from app.config import ROOT
    with gzip.open(ROOT / "data/product_data.json.gz", "rt") as file:
        return json.load(file)


@pytest.fixture(scope="session")
def real_pack_products(actual_product_rows):
    from app.catalog import extract
    from app.lookup import reconcile_size
    selected, seen = [], set()
    # Ten volume, ten weight, ten CASE-only records, all distinct real products.
    for group in ("L", "KG", "CASE"):
        group_products = []
        for row in actual_product_rows:
            if row["id"] in seen:
                continue
            conversions = (row.get("synkkaData") or {}).get("unitConversions") or []
            if group == "CASE" and (not any(c.get("synkkaPackagingType") == "CASE" for c in conversions)
                    or any(c.get("synkkaPackagingType") == "BASE_UNIT_OR_EACH" for c in conversions)):
                continue
            product = extract(row, {}, {})
            unit = "KG" if group == "KG" else "L"
            size = reconcile_size(product, {}, unit)[0]
            if not size and group == "CASE":
                unit = "KG"
                size = reconcile_size(product, {}, unit)[0]
            if size and size >= .001:
                seen.add(row["id"])
                group_products.append((product, unit))
            if len(group_products) == 10:
                break
        assert len(group_products) == 10
        selected.extend(group_products)
    assert len({p.id for p, _ in selected}) == 30
    return selected


def replay_recorded_demo(gateway, need, respx_mock):
    """Offline replay of captured evidence; live acceptance remains a separate manual check."""
    from pathlib import Path
    from app.models import NeedSpec
    from app import audit
    from datetime import timedelta
    import httpx
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
    return result
