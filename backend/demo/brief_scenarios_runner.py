"""Shared offline MCP client; procurement always uses the real gateway tools."""
import asyncio
from datetime import timedelta
from zoneinfo import ZoneInfo
import json
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile

import httpx
import socket

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import audit
from app.auth import Principal
from app.foundation import PRINCIPAL
from app.mcp_server import create_server
from app.proposals import ProposalPreferences
from app.sandbox import scenario_gateway
from app.webhooks import SubscriptionCreate, verify_webhook


def need(customer="recipe", days=3):
    return {"customer_id": customer, "deliver_by": str(audit.utcnow().astimezone(ZoneInfo("Europe/Helsinki")).date()+timedelta(days=days)),
            "intent": "Oat drink for cooking", "search_terms": ["oat", "drink"],
            "quantity": {"value": 10.0, "unit": "L"},
            "function": {"product_type": "oat drink", "intended_use": "cooking", "preparation": "UHT", "acceptable_units": ["L"]},
            "substitute_for": {"product_id": "usual-oat"}}


def call(server, name, arguments=None):
    result = asyncio.run(server.call_tool(name, arguments or {}))
    if result.is_error:
        raise AssertionError(f"{name}: {result.structured_content}")
    data = result.structured_content
    return data["result"] if isinstance(data, dict) and set(data) == {"result"} else data


def purchase(server, request, key):
    lookup = call(server, "find_offers", {"need": request})
    best = lookup["offers"][0]
    quote = call(server, "create_quote", {"customer_id": request["customer_id"], "deliver_by": request["deliver_by"],
        "need_ref": lookup["lookup_id"], "lines": [{"product_id": best["product_id"], "quantity_sales_units": best["units_needed"]}]})
    order = call(server, "place_order", {"quote_id": quote["id"], "idempotency_key": key})
    return lookup, quote, order


def run(number):
    names = {1: "recipe_order", 2: "last_minute_event", 3: "stockout", 4: "proactive_selling", 5: "allergen_safe_substitution"}
    scenario = f"scenario_{number}_{names[number]}"
    print(f"Agent: running {scenario} over in-process MCP (sandbox, no network).")
    with scenario_gateway(scenario) as g:
        buyer = create_server(g, "sandbox-agent")
        supplier = create_server(g, "supplier-agent")
        report = {"scenario": scenario}
        if number in {1, 2}:
            ingredient = {"name": "Seasonal oat sauce", "quantity": {"value": 10.0, "unit": "L"},
                          "search_terms": ["UHT"], "function": need()["function"],
                          "substitute_for": {"product_id": "usual-oat"}, "substitutions_allowed": True}
            if number == 1:
                ingredient["on_hand"] = {"value": 7.0, "unit": "L"}
            request = {"customer_id": "recipe", "deliver_by": need(days=1)["deliver_by"], "manifest": {
                "recipe_name": "Seasonal lunch for ten", "servings": 10.0,
                "focus_ingredient": ingredient["name"], "ingredients": [ingredient]}}
            basket = call(buyer, "find_basket_offers", {"request": request})
            print("Gateway: recipe need, on-hand inventory, purchasable packs and deadline alternatives checked.")
            if number == 2:
                assert any(o["arrives_in_time"] is False for o in basket["ingredients"][0]["offers"]) or basket["basket"]["lines"][0].get("substitution")
                print("Human decision: approve the function-compatible substitute for the late focus ingredient.")
            else:
                print("Human decision: the recipe and inventory are supplied by the buyer; 10 L needed, 7 L on hand, buy 3 L.")
            quote = call(buyer, "create_quote", {"customer_id": "recipe", "deliver_by": request["deliver_by"],
                "basket_ref": {"basket_lookup_id": basket["basket_lookup_id"]}})
            order = call(buyer, "place_order", {"quote_id": quote["id"], "idempotency_key": scenario})
            report.update(basket=basket, quote=quote, order=order)
        elif number == 3:
            lookup, quote, order = purchase(buyer, need(days=0), scenario)
            assert order["status"] == "CONFIRMED"
            recurring = call(buyer, "create_recurring_order", {"request": {"approved_order_id": order["id"],
                "cadence": {"rrule": "FREQ=WEEKLY", "timezone": "Europe/Helsinki", "start_at": (audit.utcnow()+timedelta(days=3)).isoformat()}}})
            deliveries = []
            def receive(request):
                body = json.loads(request.content)
                if body["type"] == "webhook.challenge":
                    return httpx.Response(200, json={"challenge": body["challenge"]})
                assert verify_webhook(request.content, request.headers, g.signing.jwks())
                deliveries.append(body)
                return httpx.Response(200)
            # Real signed webhook protocol through an in-memory receiver, with no socket calls.
            g.webhooks.client.close()
            g.webhooks.client = httpx.Client(transport=httpx.MockTransport(receive))
            g.webhooks.resolver = lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
            g.webhooks.create(SubscriptionCreate(url="https://buyer.test/hook", customer_ids=["recipe"],
                event_types=["stockout.predicted"]), Principal("sandbox-agent", customer_ids=("recipe",)))
            # Supplier data changes after the approved template, before its next occurrence.
            g.valio.rows["usual-oat"]["stock"] = 0
            watch = call(supplier, "run_stockout_watch")
            inbox = call(buyer, "list_stockout_alerts")
            assert inbox and watch["alert_count"] and inbox[0]["substitute"]["trade_off"] and deliveries
            print("Gateway: predicted stock-out three days ahead; verified signed webhook delivered and buyer inbox updated.")
            print("Human decision: approve the substitute and its price, pack, allergen and delivery trade-offs before the due date.")
            lookup, quote, order = purchase(buyer, need(), scenario+"-replacement")
            report.update(recurring=recurring, watch=watch, inbox=inbox, deliveries=deliveries, lookup=lookup, quote=quote, order=order)
        elif number == 4:
            _, _, order = purchase(buyer, need(), scenario)
            assert order["status"] == "CONFIRMED"
            admin = Principal("hospital-approver", kind="buyer_user", role="buyer_admin", organisation_id="hospital", customer_ids=("recipe",))
            g.proposals.put_preferences(ProposalPreferences(categories=["drinks"], channels=["inbox", "webhook"]), admin, "recipe")
            campaign = call(supplier, "suggest_proposals", {"customer_id": "recipe", "campaign_id": scenario})
            inbox = call(buyer, "list_proposals")
            assert campaign["delivery_count"] == 1 and inbox
            print("Gateway: new products in purchased categories, filtered by constraints, consent and quotas.")
            print("Human decision: buyer consents to campaigns; buyer agent accepts one under its normal purchasing mandate.")
            accepted = call(buyer, "accept_proposal", {"proposal_id": inbox[0]["id"], "need": need()})
            assert accepted["order"]["quote_id"]
            report.update(campaign=campaign, accepted=accepted, order=accepted["order"])
        else:
            lookup, quote, order = purchase(buyer, need("hospital"), scenario)
            assert order["status"] == "PENDING_APPROVAL"
            assert lookup["offers"][0]["trade_off"]
            print("Gateway: excludes milk allergens, excess salt and missing nutrition evidence; returns a cooking-compatible substitute.")
            print("Human decision: hospital buyer approver performs required clinical review; procurement compliance is not a clinical safety claim.")
            report.update(lookup=lookup, quote=quote, order=order)
        order = report.get("order")
        if order and order["status"] == "PENDING_APPROVAL":
            human = Principal("hospital-approver", kind="buyer_user", role="buyer_approver", organisation_id="hospital", customer_ids=(order["customer_id"],))
            token = PRINCIPAL.set(human)
            try:
                g.orders.approve(order["id"], "Sandbox human", evidence_revision=order["evidence_revision"])
            finally:
                PRINCIPAL.reset(token)
            final = call(buyer, "get_order_status", {"order_id": order["id"]})
            assert final["status"] == "CONFIRMED"
            report.update(order=final)
        if report.get("order", {}).get("status") == "CONFIRMED":
            receipt = call(buyer, "get_order_receipt", {"order_id": report["order"]["id"]})
            assert receipt["receipt"]
            report["receipt"] = receipt
        with NamedTemporaryFile(mode="w", prefix=scenario+"-", suffix=".json", delete=False) as artifact:
            json.dump(report, artifact, indent=2, default=str)
        print(f"{artifact.name} — full gateway evidence and human decision transcript data.")
        print("Scenario complete.")
        return report
