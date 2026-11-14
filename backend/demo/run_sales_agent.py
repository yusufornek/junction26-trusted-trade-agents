"""Send supplier content to the hotel chain; buyer consent and acceptance are separate steps.

Run from backend: SUPPLIER_AGENT_KEY=... .venv/bin/python demo/run_sales_agent.py
The key must map to a policy identity of kind/role supplier_agent. Customer 1 must
opt in through its buyer_admin before this campaign can reach the inbox.
"""
import argparse
import json
import os
from datetime import datetime, timedelta, timezone

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--customer", default="1")
    parser.add_argument("--product", default="464306", help="Valio Arki Eila lactose-free 1 L milk")
    parser.add_argument("--unit-price", type=float, default=1.01, help="Advertised EUR price; buyer acceptance revalidates it")
    parser.add_argument("--campaign", default="hotel-lactose-free-breakfast")
    args = parser.parse_args()
    key = os.environ.get("SUPPLIER_AGENT_KEY")
    if not key:
        parser.error("Set SUPPLIER_AGENT_KEY to the supplier agent's credential")
    now = datetime.now(timezone.utc)
    proposal = {"campaign_id": args.campaign, "products": [args.product],
        "rationale": "Lactose-free breakfast milk for the hotel chain; fixed advertised price, subject to buyer validation.",
        "price_evidence": [{"product_id": args.product, "unit_price": args.unit_price, "currency": "EUR", "fetched_at": now.isoformat()}],
        "valid_until": (now + timedelta(days=2)).isoformat(), "target": {"customer_ids": [args.customer]}}
    with httpx.Client(base_url=args.base_url, timeout=30) as client:
        response = client.post("/v1/proposals", json=proposal, headers={"Authorization": f"Bearer {key}"})
        response.raise_for_status()
        print(json.dumps(response.json(), indent=2))


if __name__ == "__main__":
    main()
