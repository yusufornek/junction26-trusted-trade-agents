import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from conftest import OPERATOR_HEADERS
from app.db import DomainError
from app.models import PolicyUpdateRequest, QuoteRequest
from conftest import quote_request, set_customers


def unconstrained(need):
    return need.model_copy(update={"required_claims": [], "exclude_allergens": []})


def test_ac_1_lookup_unions_standing_claims_and_shows_sources(gateway, need, upstream):
    set_customers(gateway, {"1": {"required_claims": ["free_from_lactose"], "note": "Hotel buyer"}})
    result = gateway.find_offers(unconstrained(need), actor="agent:web-console")
    assert result.offers and "2" not in {o.product_id for o in result.offers}
    assert result.effective_constraints.model_dump() == {
        "required_claims": [{"code": "FREE_FROM_LACTOSE", "source": "customer_policy"}], "exclude_allergens": []}
    assert all("customer_policy" in " ".join(o.matched_because) for o in result.offers)
    assert result.need.required_claims == []  # Standing rules never rewrite the agent's stored request.


def test_ac_2_explicit_line_cannot_escape_standing_claim(gateway, need, upstream):
    set_customers(gateway, {"1": {"required_claims": ["FREE_FROM_LACTOSE"]}})
    need = unconstrained(need)
    result = gateway.find_offers(need, actor="agent:web-console")
    quote = gateway.orders.create_quote(quote_request(need, qty=1, product_id="2", need_ref=result.lookup_id), "web-console")
    assert any("customer_policy" in v and "FREE_FROM_LACTOSE" in v for v in quote.hard_constraint_violations)
    order = gateway.orders.place_order(quote.id, "standing-explicit", "web-console")
    assert order.status == "REJECTED"
    with pytest.raises(DomainError, match="INVALID_TRANSITION"):
        gateway.orders.approve(order.id, "Ada")


def test_ac_3_every_quote_requires_stored_request_and_plan_ref_binds_it(gateway, need, upstream):
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        response = client.post("/v1/quotes", json=quote_request(need, qty=1).model_dump(mode="json"))
        assert response.status_code == 422 and response.json()["code"] == "NEED_REF_REQUIRED"
        result = gateway.find_offers(need)
        request = QuoteRequest(customer_id="1", deliver_by=need.deliver_by,
                               plan_ref={"lookup_id": result.lookup_id, "plan_index": 0})
        quote = gateway.orders.create_quote(request)
        assert quote.need_ref == result.lookup_id
        with pytest.raises(DomainError, match="NEED_NOT_FOUND"):
            gateway.orders.create_quote(quote_request(need, qty=1, need_ref="made-up"))


def test_ac_4_current_policy_added_after_quote_rejects_at_placement(gateway, need, upstream):
    need = unconstrained(need)
    lookup = gateway.find_offers(need, actor="agent:web-console")
    quote = gateway.orders.create_quote(quote_request(need, qty=1, need_ref=lookup.lookup_id), "web-console")
    assert not quote.hard_constraint_violations
    set_customers(gateway, {"1": {"exclude_allergens": ["AM"]}})
    order = gateway.orders.place_order(quote.id, "standing-after-quote", "web-console")
    assert order.status == "REJECTED"
    assert any(h.rule == "hard_constraint_violation" and "customer_policy" in h.detail and "AM" in h.detail for h in order.policy.hits)


def test_ac_5_current_policy_blocks_pending_approval_but_allows_rejection(gateway, need, upstream):
    lookup = gateway.find_offers(unconstrained(need), actor="agent:web-console")
    quote = gateway.orders.create_quote(quote_request(need, qty=200, need_ref=lookup.lookup_id), "web-console")
    order = gateway.orders.place_order(quote.id, "standing-after-order", "web-console")
    assert order.status == "PENDING_APPROVAL"
    set_customers(gateway, {"1": {"exclude_allergens": ["AM"]}})
    with pytest.raises(DomainError) as error:
        gateway.orders.approve(order.id, "Ada")
    assert error.value.code == "HARD_CONSTRAINT_VIOLATION"
    assert any("customer_policy" in hit["detail"] for hit in error.value.data["rule_hits"])
    assert gateway.orders.get_order(order.id).status == "PENDING_APPROVAL"
    assert gateway.orders.reject(order.id, "Ada").status == "REJECTED"


def test_ac_6_customer_policy_get_put_preservation_vocabulary_and_audit(gateway):
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        body = client.get("/v1/policy").json()
        body["customers"] = {"1": {"required_claims": [" free_from_lactose "], "exclude_allergens": ["ae"], "note": "Hotel"}}
        updated = client.put("/v1/policy", json={"policy": body, "updated_by": "Ada"})
        assert updated.status_code == 200
        customers = updated.json()["customers"]
        assert customers["1"]["required_claims"] == ["FREE_FROM_LACTOSE"]
        assert customers["1"]["exclude_allergens"] == ["AE"]
        diff = gateway.audit()[-1].data["diff"]
        assert gateway.audit()[-1].action == "POLICY_UPDATED" and diff["customers.1"]["after"] == customers["1"]
        body.pop("customers")
        body["approval"]["max_auto_total_eur"] = 100
        response = client.put("/v1/policy", json={"policy": body, "updated_by": "Ada"})
        assert response.status_code == 200 and response.json()["customers"] == customers
        for field in ("required_claims", "exclude_allergens"):
            body["customers"] = {"1": {field: ["MADE_UP"]}}
            response = client.put("/v1/policy", json={"policy": body, "updated_by": "Ada"})
            assert response.status_code == 422 and response.json()["code"] == "UNKNOWN_CODE"
            assert field in response.json()["message"]
        assert client.get("/v1/policy").json()["customers"] == customers


def test_ac_7_request_and_customer_exclusion_union_counts_both_sources(gateway, need, upstream):
    set_customers(gateway, {"1": {"exclude_allergens": ["AM"]}})
    result = gateway.find_offers(need.model_copy(update={"required_claims": []}))
    assert not result.offers and result.excluded_count == 7
    assert result.effective_constraints.model_dump()["exclude_allergens"] == [
        {"code": "AE", "source": "request"}, {"code": "AM", "source": "customer_policy"}]
    assert sum(v for k, v in result.exclusion_counts.items() if "AE" in k and "request" in k) == 1
    assert sum(v for k, v in result.exclusion_counts.items() if "AM" in k and "customer_policy" in k) == 7
    assert "customer_policy" in result.no_match_reason and "request" in result.no_match_reason
