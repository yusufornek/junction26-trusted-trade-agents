"""Exactly one focused regression per spec 46 acceptance criterion, entirely offline."""
import asyncio
import json
from datetime import timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
import socket
import subprocess
import sys

import httpx
import pytest

from app import audit
from app.auth import Principal
from app.brief_scenarios_selling import SuggestCampaign, suggest
from app.mcp_server import create_server
from app.models import BasketLookupRequest, NeedSpec, PolicyUpdateRequest, QuoteRequest
from app.proposals import AcceptProposal, ProposalPreferences
from app.recurring import RecurringRequest
from app.sandbox import scenario_gateway
from app.webhooks import SubscriptionCreate, verify_webhook
from conftest import demo_catalog


FUNCTION = {"product_type": "oat drink", "intended_use": "cooking", "preparation": "UHT", "acceptable_units": ["L"]}
AGENT = Principal("sandbox-agent", customer_ids=("recipe", "hospital"))


def need(customer="recipe", days=3):
    return NeedSpec(customer_id=customer, deliver_by=audit.utcnow().astimezone(ZoneInfo("Europe/Helsinki")).date()+timedelta(days=days),
        intent="Oat drink", search_terms=["oat", "drink"], quantity={"value": 10.0, "unit": "L"},
        function=FUNCTION, substitute_for={"product_id": "usual-oat"})


def basket(**ingredient):
    return BasketLookupRequest(customer_id="recipe", deliver_by=audit.utcnow().astimezone(ZoneInfo("Europe/Helsinki")).date()+timedelta(days=1), manifest={
        "recipe_name": "Seasonal lunch", "servings": 10.0, "focus_ingredient": "Sauce", "ingredients": [{
            "name": "Sauce", "quantity": {"value": 10.0, "unit": "L"}, "search_terms": ["UHT"],
            "function": FUNCTION, "substitutions_allowed": True, "substitute_for": {"product_id": "usual-oat"}, **ingredient}]})


def buy(g, request=None, key="seed"):
    request = request or need()
    lookup = g.find_offers(request, "agent:sandbox-agent")
    offer = lookup.offers[0]
    quote = g.orders.create_quote(QuoteRequest(customer_id=request.customer_id, deliver_by=request.deliver_by,
        need_ref=lookup.lookup_id, lines=[{"product_id": offer.product_id, "quantity_sales_units": offer.units_needed}]), "sandbox-agent")
    order = g.orders.place_order(quote.id, key, "sandbox-agent")
    return lookup, quote, order


def test_AC_1_on_hand_deficit_and_focus_coverage():
    with scenario_gateway("scenario_1_recipe_order") as g:
        request = basket(on_hand={"value": 7000.0, "unit": "ML"})
        result = g.baskets.find_basket_offers(request, "agent:sandbox-agent")
        coverage = result.basket.coverage_by_ingredient[0]
        assert (coverage.needed.value, coverage.on_hand.value, coverage.to_buy.value) == (10, 7, 3)
        assert result.basket.lines[0].units_needed == 3
        assert result.line_needs["usual-oat"].quantity.value == 3
        quote = g.orders.create_quote(QuoteRequest(customer_id="recipe", deliver_by=request.deliver_by,
            basket_ref={"basket_lookup_id": result.basket_lookup_id}), "sandbox-agent")
        assert g.orders.place_order(quote.id, "deficit", "sandbox-agent").lines[0].units_needed == 3
        request.servings = 20
        scaled = g.baskets.find_basket_offers(request, "agent:sandbox-agent")
        assert scaled.basket.coverage_by_ingredient[0].to_buy.value == 13
        request.servings = None
        request.manifest.ingredients[0].on_hand.value = 20000
        full = g.baskets.find_basket_offers(request, "agent:sandbox-agent")
        assert not full.basket.lines and not full.basket.uncovered
        assert full.basket.coverage_by_ingredient[0].covered == 1


def test_AC_2_tomorrow_deadline_alternative_and_cutoff():
    with scenario_gateway("scenario_2_last_minute_event") as g:
        result = g.baskets.find_basket_offers(basket(), "agent:sandbox-agent")
        assert result.ingredients[0].chosen_line == "new-oat"
        assert result.basket.lines[0].arrives_in_time is True
        late_basket = next(o for o in result.ingredients[0].offers if o.product_id == "usual-oat")
        assert late_basket.arrives_in_time is False and late_basket.cannot_source_in_time["in_time_alternatives"]
        assert result.basket.policy_preview.outcome == "PENDING_APPROVAL"
        # Ordinary lookup retains late evidence with alternatives, even when not a substitution request.
        request = need(days=1).model_copy(update={"substitute_for": None})
        lookup = g.find_offers(request, "agent:sandbox-agent")
        late = next(o for o in lookup.offers if o.product_id == "usual-oat")
        assert late.arrives_in_time is False
        assert late.cannot_source_in_time["earliest_possible_date"] == str(audit.utcnow().astimezone(ZoneInfo("Europe/Helsinki")).date()+timedelta(days=3))
        assert late.cannot_source_in_time["in_time_alternatives"][0]["trade_off"]
        g.valio.rows["new-oat"]["cutoff_hours"] = -1
        missed = g.find_offers(request, "agent:sandbox-agent")
        assert all(not o.arrives_in_time for o in missed.offers)


def test_AC_3_functional_substitute_complete_tradeoff_and_approval():
    with scenario_gateway("scenario_5_allergen_safe_substitution") as g:
        result = g.baskets.find_basket_offers(basket(), "agent:sandbox-agent")
        offer = result.basket.lines[0]
        assert offer.product_id == "new-oat" and offer.substitution.for_product == "usual-oat"
        assert "similar-wrong" not in {o.product_id for o in result.ingredients[0].offers}
        assert set(offer.trade_off) >= {"price_difference_per_required_quantity", "pack_change", "unit_change",
            "allergen_differences", "claim_differences", "delivery_date_difference", "explanation"}
        assert offer.trade_off["price_difference_per_required_quantity"] == pytest.approx(2)
        assert result.basket.policy_preview.outcome == "PENDING_APPROVAL"
        q = g.orders.create_quote(QuoteRequest(customer_id="recipe", deliver_by=result.deliver_by,
            basket_ref={"basket_lookup_id": result.basket_lookup_id}), "sandbox-agent")
        order = g.orders.place_order(q.id, "substitute", "sandbox-agent")
        assert order.status == "PENDING_APPROVAL"
        assert order.lines[0].trade_off == q.lines[0].trade_off and q.lines[0].trade_off["price_difference_per_required_quantity"] == pytest.approx(2)


def test_AC_4_recurring_three_day_signed_webhook_and_scoped_inbox(respx_mock, monkeypatch):
    with scenario_gateway("scenario_3_stockout") as g:
        _, _, order = buy(g, need(days=0))
        recurring = g.recurring.create(RecurringRequest(approved_order_id=order.id, cadence={"rrule": "FREQ=WEEKLY",
            "timezone": "Europe/Helsinki", "start_at": audit.utcnow()+timedelta(days=3)}), AGENT)
        calls = []
        monkeypatch.setattr(g.webhooks, "resolver", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
        def receive(request):
            body = json.loads(request.content)
            if body["type"] == "webhook.challenge":
                return httpx.Response(200, json={"challenge": body["challenge"]})
            calls.append(request)
            return httpx.Response(200)
        respx_mock.post("https://93.184.216.34/hook").mock(side_effect=receive)
        g.webhooks.create(SubscriptionCreate(url="https://buyer.test/hook", customer_ids=["recipe"],
            event_types=["stockout.predicted"]), AGENT)
        g.valio.rows["usual-oat"]["stock"] = 0
        result = g.stockout_watch.run()
        alert = result["alerts"][0]
        assert alert["source"].startswith("recurring:"+recurring["id"])
        assert alert["order_date"] == str(audit.utcnow().astimezone(ZoneInfo("Europe/Helsinki")).date()+timedelta(days=3))
        assert alert["substitute"]["product_id"] == "new-oat" and alert["substitute"]["trade_off"]
        assert len(calls) == 1 and verify_webhook(calls[0].content, calls[0].headers, g.signing.jwks())
        assert json.loads(calls[0].content)["payload"]["code"] == "STOCKOUT_PREDICTED"
        assert g.stockout_watch.inbox(AGENT) and not g.stockout_watch.inbox(Principal("other", customer_ids=("other",)))
        assert not g.stockout_watch.run()["alerts"] and len(calls) == 1
        g.valio.rows["usual-oat"]["stock"] = 1000
        open_basket = g.baskets.find_basket_offers(basket(), "agent:sandbox-agent")
        quoted_basket = g.baskets.find_basket_offers(basket(), "agent:sandbox-agent")
        open_quote = g.orders.create_quote(QuoteRequest(customer_id="recipe", deliver_by=quoted_basket.deliver_by,
            basket_ref={"basket_lookup_id": quoted_basket.basket_lookup_id}), "sandbox-agent")
        g.valio.rows["usual-oat"]["stock"] = 0
        more = g.stockout_watch.run()["alerts"]
        assert {a["source"] for a in more} == {"basket:"+open_basket.basket_lookup_id, "quote:"+open_quote.id}
        assert all(a["substitute"]["trade_off"] for a in more) and len(calls) == 3


def test_AC_5_catalog_suggestion_filters_history_categories_constraints_and_accepts():
    with scenario_gateway("scenario_4_proactive_selling") as g:
        buy(g)
        admin = Principal("hospital-approver", kind="buyer_user", role="buyer_admin", organisation_id="hospital", customer_ids=("recipe",))
        g.proposals.put_preferences(ProposalPreferences(categories=["drinks"], channels=["inbox"]), admin, "recipe")
        result = suggest(g, SuggestCampaign(customer_id="recipe", campaign_id="new-catalog"), Principal("supplier-agent", kind="supplier_agent"))
        assert result["delivery_count"] == 1
        proposal = g.proposals.inbox(AGENT)[0]
        assert proposal["products"] == ["new-oat"]
        accepted = g.proposals.action(proposal["id"], "accept", AGENT, AcceptProposal(need=need().model_copy(update={"substitute_for": None})))
        assert accepted["order"].quote_id
        assert g.orders.get_quote(accepted["order"].quote_id).lines[0].product_id == "new-oat"


def test_AC_6_hospital_nutrition_exclusions_and_current_policy_revalidation():
    with scenario_gateway("scenario_5_allergen_safe_substitution") as g:
        lookup = g.find_offers(need("hospital"), "agent:sandbox-agent")
        assert [o.product_id for o in lookup.offers] == ["new-oat"]
        assert any("max_salt_g" in k and "violates" in k for k in lookup.exclusion_counts)
        assert any("max_salt_g" in k and "missing nutrition" in k for k in lookup.exclusion_counts)
        excluded = {row["product_id"]: row["reasons"] for row in lookup.excluded_products}
        assert any("violates" in reason for reason in excluded["salty-oat"])
        assert any("missing nutrition" in reason for reason in excluded["unknown-oat"])
        offer = lookup.offers[0]
        quote = g.orders.create_quote(QuoteRequest(customer_id="hospital", deliver_by=lookup.need.deliver_by, need_ref=lookup.lookup_id,
            lines=[{"product_id": offer.product_id, "quantity_sales_units": offer.units_needed}]), "sandbox-agent")
        policy = g.policy.get()
        policy.customers["hospital"].max_salt_g = .01
        g.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Hospital nutrition rule"))
        assert g.orders.place_order(quote.id, "nutrition-tightened", "sandbox-agent").status == "REJECTED"


def test_AC_7_all_five_real_mcp_demo_scripts_exit_zero_without_network(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    scripts = sorted((backend/"demo").glob("scenario_[1-5]_*.py"))
    assert len(scripts) == 5
    for script in scripts:
        code = "import runpy,socket,sys; socket.socket.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError('network forbidden')); sys.path.insert(0,sys.argv[1]); runpy.run_path(sys.argv[2],run_name='__main__')"
        result = subprocess.run([sys.executable, "-c", code, str(backend/"demo"), str(script)],
            cwd=backend, capture_output=True, text=True, timeout=45)
        (tmp_path/(script.stem+".log")).write_text(result.stdout+result.stderr)
        assert result.returncode == 0, result.stdout+result.stderr
        assert "Human decision:" in result.stdout and "Scenario complete." in result.stdout


def test_AC_8_original_hotel_milk_path_unchanged(gateway, need, upstream, tmp_path):
    demo_catalog(gateway, tmp_path, upstream)
    server = create_server(gateway, "hotel-chain-agent")
    lookup = asyncio.run(server.call_tool("find_offers", {"need": need.model_dump(mode="json")}))
    assert not lookup.is_error
    best = lookup.structured_content["offers"][0]
    assert best["product_id"] == "464306" and best["total"] == 1010
    quote = asyncio.run(server.call_tool("create_quote", {"customer_id": "1", "deliver_by": str(need.deliver_by),
        "need_ref": lookup.structured_content["lookup_id"], "lines": [{"product_id": "464306", "quantity_sales_units": 1000.0}]}))
    assert not quote.is_error
    order = asyncio.run(server.call_tool("place_order", {"quote_id": quote.structured_content["id"], "idempotency_key": "spec46-hotel"}))
    assert not order.is_error and order.structured_content["status"] == "CONFIRMED"
    assert order.structured_content["agent_id"] == "hotel-chain-agent"
