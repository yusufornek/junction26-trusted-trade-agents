import json

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from app.api import create_app
from app.models import PolicyUpdateRequest, Quantity
from conftest import quote_request


@pytest.fixture
def shortlist(gateway, need, upstream, respx_mock, monkeypatch):
    products = [gateway.catalog.get("1").model_copy(update={"id": str(i)}) for i in range(1, 16)]
    monkeypatch.setattr(gateway.catalog, "search", lambda *args, **kwargs: (products, 0))
    need = need.model_copy(update={"quantity": Quantity(value=1, unit="SALES_UNIT")})
    fits = {p.id: 0.96 - i * 0.001 for i, p in enumerate(products)}

    def decisions(request):
        body = json.loads(request.content)
        upstream.decision_calls.append(body)
        return httpx.Response(200, json={"answers": [
            {"type": "predicate", "name": q["name"], "probability": fits[q["name"][4:]]}
            for q in body["questions"]]})

    def prices(request):
        response = upstream.price_response(request).json()
        for row in response["products"]:
            row["catalogPrice"]["value"] = 16 - int(row["productId"])
        return httpx.Response(200, json=response)

    respx_mock.post("https://api.openai.com/v1/decisions").mock(side_effect=decisions)
    respx_mock.post("https://valio.test/api/price").mock(side_effect=prices)
    return need, fits


def test_ac_1_prices_all_suitable_in_one_batch(gateway, upstream, shortlist):
    need, _ = shortlist
    result = gateway.find_offers(need)
    assert result.priced_count == result.screened_count == 15
    assert len(result.offers) == 10
    assert len(upstream.price_calls) == len(upstream.availability_calls) == 1
    assert len(upstream.price_calls[0]["products"]) == 15
    assert upstream.price_calls[0]["products"] == upstream.availability_calls[0]["products"]
    assert result.live_calls.model_dump() == {"price": 1, "availability": 1}
    assert gateway.lookup.get(result.lookup_id) == result
    assert gateway.audit()[-1].data["priced_count"] == 15


def test_ac_2_cheapest_with_lowest_suitable_fit_is_best(gateway, shortlist):
    need, fits = shortlist
    fits.update({pid: 0.950001 for pid in fits})
    fits["15"] = 0.95
    result = gateway.find_offers(need)
    assert result.offers[0].product_id == min(fits, key=fits.get) == "15"
    assert result.offers[0].rank_label == "best"
    assert result.offers[0].fit == fits["15"]


def test_ac_3_shuffled_suitable_fits_keep_top_three(gateway, shortlist):
    need, fits = shortlist
    # Tiny, non-monotonic fit noise used to normalize into a full ranking-weight difference.
    fits.update({pid: 0.95 + (int(pid) % 3) * 0.000001 for pid in fits})
    first = gateway.find_offers(need)
    fits.update(zip(fits, reversed(list(fits.values()))))
    gateway.valio.cache.clear()
    second = gateway.find_offers(need)
    assert [o.product_id for o in first.offers[:3]] == [o.product_id for o in second.offers[:3]] == ["15", "14", "13"]
    assert second.live_calls.model_dump() == {"price": 1, "availability": 1}


def test_ac_4_fill_up_preserves_low_fits_and_approval(gateway, upstream, shortlist):
    need, fits = shortlist
    fits.update({str(i): 0.9 if i >= 13 else 0.79 - i * 0.01 for i in range(1, 16)})
    result = gateway.find_offers(need)
    expected = sorted(fits, key=lambda pid: -fits[pid])[:10]
    assert upstream.price_calls[0]["products"] == expected
    assert result.priced_count == 10
    assert sum(o.fit >= 0.8 for o in result.offers) == 3
    assert all(o.fit == fits[o.product_id] for o in result.offers)
    quote = gateway.orders.create_quote(quote_request(need, qty=1, need_ref=result.lookup_id))
    order = gateway.orders.place_order(quote.id, "low-fit", "hotel-chain-agent")
    assert order.status == "PENDING_APPROVAL"
    assert "min_fit_probability" in {h.rule for h in order.policy.hits}


def test_ac_5_live_max_caps_highest_fits(gateway, upstream, shortlist):
    need, fits = shortlist
    body = gateway.policy.get().model_dump(mode="json")
    body["shortlist"]["live_max"] = 12
    gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Ada"))
    result = gateway.find_offers(need)
    assert result.priced_count == 12
    assert upstream.price_calls[0]["products"] == sorted(fits, key=lambda pid: -fits[pid])[:12]


def test_ac_6_no_decisions_uses_catalog_order(gateway, upstream, shortlist):
    need, _ = shortlist
    gateway.decisions.key = ""
    result = gateway.find_offers(need)
    assert upstream.price_calls[0]["products"] == [str(i) for i in range(1, 11)]
    assert result.priced_count == 10 and result.screened_count == 15
    assert result.decision_calls == 0
    assert all(o.fit is None and "NO_FIT_SCORE" in {w.code for w in o.warnings} for o in result.offers)


def test_ac_7_policy_live_max_validation_and_legacy_update(gateway):
    with TestClient(create_app(gateway)) as client:
        policy = client.get("/v1/policy").json()
        assert policy["shortlist"]["live_max"] == 25
        for value in (10, 40, 12):
            policy["shortlist"]["live_max"] = value
            response = client.put("/v1/policy", json={"policy": policy, "updated_by": "Ada"})
            assert response.status_code == 200
            assert response.json()["shortlist"]["live_max"] == value
        for value in (9, 41, 12.5):
            policy["shortlist"]["live_max"] = value
            assert client.put("/v1/policy", json={"policy": policy, "updated_by": "Ada"}).status_code == 422
        del policy["shortlist"]["live_max"]
        response = client.put("/v1/policy", json={"policy": policy, "updated_by": "Ada"})
        assert response.status_code == 200
        assert response.json()["shortlist"]["live_max"] == 12
        # Runtime documents from older versions inherit the committed live cap.
        gateway.policy.runtime_path.write_text(yaml.safe_dump(policy))
        assert gateway.policy.get().shortlist.live_max == 25


def test_ac_9_fit_question_carries_suitability_only(gateway, need, upstream):
    """Hard constraints are verified by the gateway and never re-judged by the Decisions model."""
    gateway.find_offers(need.model_copy(update={"exclude_allergens": ["AE"], "required_claims": ["FREE_FROM_LACTOSE"]}))
    sent = upstream.decision_calls[-1]
    payload = next(part["data"] for part in sent["input"][0]["content"] if part["type"] == "input_json")
    assert set(payload["need"]) == {"intent", "quantity", "deliver_by"}
    assert "exclude_allergens" not in str(payload["need"]) and "required_claims" not in str(payload["need"])
