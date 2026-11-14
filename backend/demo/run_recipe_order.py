"""Agent-side recipe decomposition: offline rehearsal or an authenticated remote journey."""
import argparse
import json
import os
from datetime import date, timedelta
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile
from uuid import uuid4

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import audit
from app.models import BasketLookupRequest, QuoteRequest
from app.sandbox import scenario_gateway
from app.signing import verify_quote

# This manifest is supplied by the buyer agent, not invented by the procurement gateway.
MANIFEST = {"version": 1, "recipe_name": "Plant lasagne", "servings": 100,
    "ingredients": [
        {"name": "Lasagne pasta", "search_terms": ["pasta"], "quantity": {"value": 8, "unit": "KG"}, "substitutions_allowed": True},
        {"name": "Tomato sauce", "search_terms": ["tomato"], "quantity": {"value": 15, "unit": "KG"}, "substitutions_allowed": True},
        {"name": "Plant mince", "search_terms": ["mince"], "quantity": {"value": 8, "unit": "KG"}, "substitutions_allowed": True},
        {"name": "Oat drink", "search_terms": ["oat"], "quantity": {"value": 10, "unit": "L"}, "substitutions_allowed": True},
        {"name": "Olive oil", "search_terms": ["oil"], "quantity": {"value": 1, "unit": "L"}, "substitutions_allowed": False}],
    "yield_note": "Agent-supplied demonstration recipe, scaled by the gateway to 200 servings."}


def sandbox_main():
    with scenario_gateway("full_stock") as g:
        result = g.baskets.find_basket_offers(BasketLookupRequest(customer_id="recipe",
            deliver_by=audit.utcnow().date() + timedelta(days=3), manifest=MANIFEST, servings=200), "agent:sandbox-agent")
        assert result.live_calls.price == result.live_calls.availability == 1
        assert not result.basket.uncovered
        quote = g.orders.create_quote(QuoteRequest(customer_id="recipe", deliver_by=result.deliver_by,
            basket_ref={"basket_lookup_id": result.basket_lookup_id}), "sandbox-agent")
        verify_quote(quote, g.signing.jwks())
        order = g.orders.place_order(quote.id, "recipe-demo", "sandbox-agent")
        assert order.status == "CONFIRMED"
        with g.db.connect() as conn:
            assert conn.execute("SELECT count(*) FROM reservations WHERE order_id=?", (order.id,)).fetchone()[0] == 5
        report = {"environment": "sandbox", "scenario": "full_stock", "basket": result.model_dump(mode="json"),
                  "quote": quote.model_dump(mode="json"), "order": order.model_dump(mode="json")}
        with NamedTemporaryFile(mode="w", prefix="recipe-order-", suffix=".json", delete=False) as file:
            json.dump(report, file, indent=2)
        print(f"{file.name} — lasagne for 200: one basket, one signed quote, one atomic simulated order.")


LASAGNE = {
    "version": 1, "recipe_name": "Lasagne", "servings": 200, "base_servings": 1,
    "yield_note": "Illustrative agent-supplied ingredient quantities per serving; buyer reviews suitability.",
    "ingredients": [
        {"name": name, "search_terms": terms, "quantity": {"value": amount, "unit": unit}, "substitutions_allowed": False}
        for name, terms, amount, unit in [
            ("Lasagne sheets", ["lasagne"], .08, "KG"), ("Minced beef", ["minced", "beef"], .1, "KG"),
            ("Tomatoes", ["tomato"], .12, "KG"), ("Milk", ["milk"], .1, "L"), ("Cheese", ["cheese"], .04, "KG")]]
}


def journey(client, request):
    def post(path, body, key=None):
        response = client.post(path, json=body, headers={"Idempotency-Key": key} if key else {})
        response.raise_for_status()
        return response.json()
    result = post("/v1/basket-lookups", request)
    if not result["basket"]["lines"]:
        return result
    quote = post("/v1/quotes", {"customer_id": request["customer_id"], "deliver_by": request["deliver_by"],
        "basket_ref": {"basket_lookup_id": result["basket_lookup_id"]}}, uuid4().hex)
    return post("/v1/orders", {"quote_id": quote["id"]}, uuid4().hex)


def remote_main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--customer", default="1")
    parser.add_argument("--deliver-by", default=str(date.today() + timedelta(days=3)))
    parser.add_argument("--manifest", type=Path, help="Agent-supplied manifest JSON; defaults to lasagne for 200")
    parser.add_argument("--credential-env", default="AGENT_TOKEN", help="Environment variable holding the issued agent key")
    args = parser.parse_args()
    token = os.environ.get(args.credential_env)
    if not token:
        parser.error(f"Set {args.credential_env} to an issued agent key")
    request = {"customer_id": args.customer, "deliver_by": args.deliver_by,
               "manifest": json.loads(args.manifest.read_text()) if args.manifest else LASAGNE}
    with httpx.Client(base_url=args.base_url, headers={"Authorization": "Bearer " + token}, timeout=60) as client:
        print(json.dumps(journey(client, request), indent=2))


def main():
    # No arguments keeps the offline rehearsal; existing remote CLI flags also work.
    if len(sys.argv) > 1:
        if "--remote" in sys.argv:
            sys.argv.remove("--remote")
        remote_main()
    else:
        sandbox_main()


if __name__ == "__main__":
    main()
