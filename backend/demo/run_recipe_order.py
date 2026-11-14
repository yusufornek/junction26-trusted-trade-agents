"""Agent-side lasagne decomposition; the gateway handles procurement and approval."""
import argparse
import json
import os
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

import httpx


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


def main():
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


if __name__ == "__main__":
    main()
