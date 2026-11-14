"""Spec 31 acceptance criteria: real pipeline, isolated fixture supplier, no network."""
from datetime import timedelta

from fastapi.testclient import TestClient
import pytest

from app import audit
from app.api import create_app
from app.auth import Credentials
from app.db import DomainError
from app.models import NeedSpec, PolicyUpdateRequest, QuoteRequest
from app.sandbox import scenario_gateway


@pytest.fixture
def hospital():
    with scenario_gateway("hospital_stockout") as gateway:
        yield gateway


def hospital_need(**updates):
    body = dict(customer_id="hospital", deliver_by=audit.utcnow().date() + timedelta(days=3),
                intent="Oat drink for UHT cooking", search_terms=["oat"],
                quantity={"value": 20, "unit": "L"}, substitute_for={"product_id": "usual-oat"},
                function={"product_type": "oat drink", "intended_use": "hospital kitchen cooking",
                          "preparation": "UHT", "acceptable_units": ["L"]})
    body.update(updates)
    return NeedSpec(**body)


def lookup(g):
    return g.find_offers(hospital_need(), "agent:sandbox-agent")


def quote(g, result):
    return g.orders.create_quote(QuoteRequest(customer_id="hospital", deliver_by=result.need.deliver_by,
        need_ref=result.lookup_id, lines=[{"product_id": result.offers[0].product_id,
                                         "quantity_sales_units": result.offers[0].units_needed}]), "sandbox-agent")


def test_AC_1_stockout_offers_functional_substitute_with_allergen_evidence(hospital):
    result = lookup(hospital)
    assert [o.product_id for o in result.offers] == ["approved-oat"]
    best = result.offers[0]
    assert best.rank_label == "best"
    assert best.substitution.model_dump()["for"] == "usual-oat"
    assert best.substitution.differences
    assert best.substitution.allergen_evidence == ["AM declared free-from (catalog nonAllergen)",
        "AE not listed in the catalog allergen declaration"]
    assert "Compliant with the recorded procurement constraints." in best.matched_because
    assert "hospital kitchen cooking" in best.fit_question
    assert hospital.catalog.get(best.product_id).category == hospital.catalog.get("usual-oat").category


def test_AC_2_cheaper_may_contain_candidate_excluded_and_counted(hospital):
    result = lookup(hospital)
    assert "may-contain" not in [o.product_id for o in result.offers]
    assert sum(v for k, v in result.exclusion_counts.items() if "may contain" in k) == 1
    assert "may-contain" not in hospital.valio.calls[0][1]


def test_AC_3_lactose_free_does_not_prove_milk_allergen_absence(hospital):
    assert "FREE_FROM_LACTOSE" in hospital.catalog.get("lactose-free-milk").claims
    result = lookup(hospital)
    assert "lactose-free-milk" not in [o.product_id for o in result.offers]
    assert any("AM" in k and "contains" in k for k in result.exclusion_counts)


def test_AC_4_missing_allergen_data_is_not_offered(hospital):
    result = lookup(hospital)
    assert "missing-evidence" not in [o.product_id for o in result.offers]
    assert any("no allergen data" in k for k in result.exclusion_counts)


def test_AC_5_clinical_review_requires_scoped_buyer_approver(hospital):
    q = quote(hospital, lookup(hospital))
    order = hospital.orders.place_order(q.id, "clinical", "sandbox-agent")
    assert order.status == "PENDING_APPROVAL"
    assert order.clinical_review == "required"
    keys = Credentials("operator-test", "hospital-approver:buyer-test,sandbox-agent:agent-test")
    with TestClient(create_app(hospital, keys=keys)) as client:
        path = f"/v1/approvals/{order.id}/approve"
        body = {"approver": "Clinical buyer", "evidence_revision": 1}
        refused = client.post(path, json=body, headers={"Authorization": "Bearer operator-test"})
        assert refused.status_code == 403 and refused.json()["code"] == "BUYER_APPROVER_REQUIRED"
        approved = client.post(path, json=body, headers={"Authorization": "Bearer buyer-test"})
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "CONFIRMED"
        assert approved.json()["approval_side"] == "buyer"


def test_AC_6_available_preferred_product_is_best_without_substitution(hospital):
    hospital.valio.rows["usual-oat"]["stock"] = 10000
    result = lookup(hospital)
    assert [o.product_id for o in result.offers] == ["usual-oat"]
    assert result.offers[0].rank_label == "best" and result.offers[0].substitution is None


def test_AC_7_excess_price_increase_is_flagged_and_requires_approval(hospital):
    hospital.valio.rows["approved-oat"]["price"] = 3
    policy = hospital.policy.get()
    policy.customers["hospital"].clinical_review = None
    policy.customers["hospital"].substitution_profile.requires_approval = False
    hospital.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Fixture buyer"))
    result = lookup(hospital)
    assert any("50.00%" in d for d in result.offers[0].substitution.differences)
    assert any(w.code == "SUBSTITUTION_PROFILE" for w in result.offers[0].warnings)
    assert result.plans[0].policy_preview.outcome == "PENDING_APPROVAL"
    q = quote(hospital, result)
    assert q.lines[0].substitution is not None
    assert hospital.orders.place_order(q.id, "price-limit", "sandbox-agent").status == "PENDING_APPROVAL"


def test_profile_brand_pack_limits_and_function_unit_validation(hospital):
    policy = hospital.policy.get()
    policy.customers["hospital"].substitution_profile.allowed_brands = ["Usual"]
    policy.customers["hospital"].substitution_profile.pack_size_range.min = 1.5
    hospital.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Fixture buyer"))
    result = lookup(hospital)
    text = next(w.text for w in result.offers[0].warnings if w.code == "SUBSTITUTION_PROFILE")
    assert "brand outside" in text and "pack size outside" in text
    with pytest.raises(DomainError, match="acceptable_units"):
        hospital.find_offers(hospital_need(quantity={"value": 20, "unit": "KG"}), "agent:sandbox-agent")


def test_functional_category_and_fit_cannot_be_bypassed_with_explicit_quote(hospital):
    result = lookup(hospital)
    q = hospital.orders.create_quote(QuoteRequest(customer_id="hospital", deliver_by=result.need.deliver_by,
        need_ref=result.lookup_id, lines=[{"product_id": "wrong-type", "quantity_sales_units": 20}]), "sandbox-agent")
    assert hospital.orders.place_order(q.id, "wrong-type", "sandbox-agent").status == "REJECTED"
