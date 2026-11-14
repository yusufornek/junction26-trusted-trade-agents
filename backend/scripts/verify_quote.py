#!/usr/bin/env python3
"""Verify a saved gateway quote without a gateway, using stdlib + cryptography only."""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.signing import quote_payload, verify_jws  # noqa: E402


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Time must include a UTC offset")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("quote", type=Path)
    parser.add_argument("--jwks", required=True, help="Saved JWKS file, or an HTTP(S) JWKS URL")
    parser.add_argument("--customer")
    parser.add_argument("--at", help="Check expiry at this ISO time (default: now)")
    args = parser.parse_args(argv)
    try:
        quote = json.loads(args.quote.read_text())
        if args.jwks.startswith(("https://", "http://")):
            with urlopen(args.jwks, timeout=10) as response:
                jwks = json.load(response)
        else:
            jwks = json.loads(Path(args.jwks).read_text())
        verify_jws(quote["signature"], quote_payload(quote), jwks)
    except Exception as error:
        print(f"signature check failed: {type(error).__name__}", file=sys.stderr)
        return 1
    try:
        at = timestamp(args.at) if args.at else datetime.now(timezone.utc)
        if timestamp(quote["expires_at"]) <= at:
            raise ValueError("Quote expired")
    except (ValueError, KeyError, TypeError) as error:
        print(f"expiry check failed: {error}", file=sys.stderr)
        return 1
    if args.customer is not None and quote["customer_id"] != args.customer:
        print("customer check failed: customer does not match", file=sys.stderr)
        return 1
    print("Valid signature, expiry and customer checks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
