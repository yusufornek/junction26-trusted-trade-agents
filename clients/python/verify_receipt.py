"""Verify a saved gateway-simulated receipt without connecting to the gateway."""
import argparse
import json
from pathlib import Path

from reference_buyer import verify_receipt

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("order", type=Path)
    parser.add_argument("--jwks", type=Path, required=True)
    parser.add_argument("--customer", required=True)
    parser.add_argument("--agent", required=True)
    args = parser.parse_args()
    verify_receipt(json.loads(args.order.read_text()), json.loads(args.jwks.read_text()),
                   customer_id=args.customer, agent_id=args.agent)
    print("Receipt verified (gateway-simulated)")
