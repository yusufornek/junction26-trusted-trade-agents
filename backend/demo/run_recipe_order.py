"""Agent-side recipe decomposition, then one offline basket → quote → order for 200."""
import json
from datetime import timedelta
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile

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


def main():
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


if __name__ == "__main__":
    main()
