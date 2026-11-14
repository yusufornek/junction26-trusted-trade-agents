import json

import pytest

from app.catalog import build
from app.lookup import broadened_terms
from app.models import PolicyDocument, PolicyUpdateRequest
from conftest import raw_product


INTENT = "1000 litres of lactose-free milk for a hotel"


def intent_need(need, intent=INTENT):
    return need.model_copy(update={"search_terms": [], "intent": intent})


@pytest.mark.parametrize("unit", ["kilogram", "kilograms", "gram", "grams", "millilitre", "millilitres", "milliliter", "milliliters"])
def test_review_written_units_are_not_search_terms(gateway, need, upstream, tmp_path, unit):
    assert broadened_terms(f"1000 {unit} of quuxfruit") == ["quuxfruit"]
    replace_catalog(gateway, tmp_path, [raw_product("unit", f"Milk sold in {unit}")])
    request = intent_need(need, f"1000 {unit} of quuxfruit")
    request.exclude_allergens = []
    result = gateway.find_offers(request)
    assert result.matched_count == result.screened_count == 0
    assert not result.offers and not upstream.decision_calls


def test_review_long_broadening_does_not_hit_sqlite_limit(gateway, need, upstream):
    from fastapi.testclient import TestClient
    from app.api import create_app
    from conftest import OPERATOR_HEADERS
    # Alphabetic words are meaningful to the fallback; include a real match to prove OR still works.
    terms = ["quux" + chr(97 + i // 26) + chr(97 + i % 26) for i in range(501)]
    request = intent_need(need, " ".join([*terms, "milk"]))
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        response = client.post("/v1/offers", json=request.model_dump(mode="json"))
    assert response.status_code == 200
    result = response.json()
    assert result["retrieval"]["terms_used"] == [*terms, "milk"]
    assert result["matched_count"] == 5 and result["offers"]


def warning_codes(result):
    return {w.code for w in result.warnings}


def assert_audit(gateway, result):
    event = gateway.audit()[-1]
    assert event.action == "LOOKUP" and event.data["lookup_id"] == result.lookup_id
    assert event.data["retrieval"] == result.retrieval.model_dump()
    for count in ("matched_count", "screened_count", "priced_count"):
        assert event.data[count] == getattr(result, count)
    assert gateway.lookup.get(result.lookup_id) == result


def replace_catalog(gateway, tmp_path, rows):
    source = tmp_path / "fallback-products.json"
    source.write_text(json.dumps(rows))
    build(gateway.db, source, tmp_path / "reliability.csv", tmp_path / "carbon.csv")


def test_ac_1_intent_broadens_and_preserves_original_fit_question(gateway, need, upstream):
    result = gateway.find_offers(intent_need(need))
    assert result.offers and result.retrieval.mode == "intent_broadened"
    assert result.retrieval.terms_used == ["lactose-free", "lactose", "free", "milk", "hotel"]
    assert "RETRIEVAL_BROADENED" in warning_codes(result)
    text = next(w.text for w in result.warnings if w.code == "RETRIEVAL_BROADENED")
    assert all(word in text for word in result.retrieval.terms_used)
    data = upstream.decision_calls[0]["input"][0]["content"][1]["data"]
    assert data["need"]["intent"] == INTENT
    assert result.matched_count == result.screened_count == 5
    assert_audit(gateway, result)


def test_ac_2_explicit_terms_keep_and_retrieval(gateway, need, upstream):
    result = gateway.find_offers(need)
    assert result.retrieval.mode == "terms" and result.retrieval.terms_used == ["milk"]
    assert result.offers and "RETRIEVAL_BROADENED" not in warning_codes(result)
    assert result.excluded_count == 2 and result.matched_count == 5
    assert_audit(gateway, result)


def test_ac_3_matching_intent_uses_original_and_mode(gateway, need, upstream):
    result = gateway.find_offers(intent_need(need, "lactose free milk"))
    assert result.retrieval.mode == "intent"
    assert result.retrieval.terms_used == ["lactose", "free", "milk"]
    assert [o.product_id for o in result.offers] == ["1"]
    assert "RETRIEVAL_BROADENED" not in warning_codes(result)
    assert result.matched_count == result.screened_count == result.priced_count == 1
    assert_audit(gateway, result)


@pytest.mark.parametrize("broaden", [False, True])
def test_ac_4_pool_truncation_reports_all_hard_filtered_matches(gateway, need, upstream, tmp_path, broaden):
    rows = [raw_product(str(i), "Milk 5 dl lactose free") for i in range(1, 44)]
    rows += [raw_product("bad-claim", "Milk conventional", lactose=False),
             raw_product("bad-allergen", "Milk egg mix", egg=True)]
    replace_catalog(gateway, tmp_path, rows)
    result = gateway.find_offers(intent_need(need) if broaden else need)
    assert result.matched_count == 43
    assert result.screened_count == gateway.policy.get().shortlist.catalog_pool == 40
    assert result.priced_count == 25 and len(result.offers) == 10
    assert result.excluded_count == 2
    text = next(w.text for w in result.warnings if w.code == "POOL_TRUNCATED")
    assert "40 of 43 matching products screened" in text and "search terms" in text
    assert len(upstream.decision_calls[0]["questions"]) == 40
    counts, removed_by = {}, {}
    gateway.catalog.search("milk", need.required_claims, need.exclude_allergens, 1,
                           counts=counts, removed_by=removed_by, mode="or" if broaden else "and")
    assert counts == {"matched_count": 43}
    assert removed_by == {"required_claims.FREE_FROM_LACTOSE (request; missing required claim)": 1,
                          "exclude_allergens.AE (request; contains)": 1}
    assert_audit(gateway, result)


def test_ac_5_empty_broadening_names_tried_words_and_suggests_terms(gateway, need, upstream):
    result = gateway.find_offers(intent_need(need, "1000 litres of quuxfruit for a zorbhotel"))
    assert result.offers == [] and result.plans == []
    assert result.retrieval.mode == "intent_broadened"
    assert result.retrieval.terms_used == ["quuxfruit", "zorbhotel"]
    assert all(word in result.no_match_reason for word in result.retrieval.terms_used)
    assert "search_terms" in result.no_match_reason
    assert result.matched_count == result.screened_count == result.priced_count == 0
    assert result.live_calls.price == result.live_calls.availability == result.decision_calls == 0
    assert upstream.price_calls == upstream.availability_calls == upstream.decision_calls == []
    assert_audit(gateway, result)


def test_ac_6_broadening_keeps_claim_and_allergen_hard_constraints(gateway, need, upstream):
    result = gateway.find_offers(intent_need(need))
    assert result.retrieval.mode == "intent_broadened" and result.offers
    assert {o.product_id for o in result.offers} == {"1", "4", "5", "6", "7"}
    assert result.excluded_count == 2
    screened = upstream.decision_calls[0]["input"][0]["content"][1]["data"]["candidates"]
    assert not {"2", "3"} & {p["id"] for p in screened}
    assert all("FREE_FROM_LACTOSE" in p["claims"] and "AE" not in p["allergens_contains"] for p in screened)


def test_or_retrieval_prioritizes_distinct_words_then_fts_rank(gateway, need, upstream, tmp_path):
    rows = [raw_product("1", "Apple pear juice"), raw_product("2", "Apple apple apple juice"),
            raw_product("3", "Pear juice"), raw_product("4", "Apple pear", lactose=False)]
    replace_catalog(gateway, tmp_path, rows)
    request = intent_need(need, "apple pear for a hotel")
    request.exclude_allergens = []  # This retrieval fixture has no AE in its vocabulary.
    result = gateway.find_offers(request)
    assert result.retrieval.mode == "intent_broadened"
    screened = upstream.decision_calls[0]["input"][0]["content"][1]["data"]["candidates"]
    assert screened[0]["id"] == "1"  # Two distinct words beat repetition of just one.
    with gateway.db.connect() as conn:
        ranked = conn.execute(
            'SELECT id FROM products_fts WHERE products_fts MATCH ? ORDER BY bm25(products_fts), id',
            ('"apple" OR "pear" OR "hotel"',)).fetchall()
    assert [p["id"] for p in screened[1:]] == [r["id"] for r in ranked if r["id"] in {"2", "3"}]
    assert result.matched_count == 3 and result.excluded_count == 1


def test_missing_explicit_terms_never_broaden(gateway, need, upstream):
    result = gateway.find_offers(need.model_copy(update={"search_terms": ["quuxfruit", "milk"]}))
    assert result.offers == [] and result.retrieval.mode == "terms"
    assert "RETRIEVAL_BROADENED" not in warning_codes(result)
    assert result.matched_count == 0


def test_stop_words_numbers_and_empty_intent_terms(gateway, need, upstream):
    assert broadened_terms("1000 OF for THE a an AND with TO by per litres liters L KG pcs ja tai sekä MILK") == ["milk"]
    result = gateway.find_offers(intent_need(need, "1000 litres ja kg"))
    assert result.retrieval.terms_used == [] and result.offers == []
    assert "no meaningful words" in result.no_match_reason and "search_terms" in result.no_match_reason
    assert result.matched_count == result.screened_count == result.priced_count == 0


def test_retrieval_warning_codes_are_known_and_informational_by_default(gateway):
    policy = gateway.policy.get()
    informational = {"RETRIEVAL_BROADENED", "POOL_TRUNCATED"}
    assert informational <= set(policy.known_warning_codes)
    assert not informational & set(policy.approval.warnings_need_approval)
    document = policy.model_dump(mode="json", include=set(PolicyDocument.model_fields))
    document["approval"]["warnings_need_approval"] += sorted(informational)
    # Operators may select any known code; the committed defaults keep these informational.
    PolicyUpdateRequest(policy=document, updated_by="Ada")
