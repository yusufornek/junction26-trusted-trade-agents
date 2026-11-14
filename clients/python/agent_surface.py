"""Offline verification of Luotsi discovery cards using an independent trust root.

The expected origin and JWKS must come from the client's trusted configuration,
never from a copied card or its jwksUrl. No network or gateway package required.
"""
import base64
import copy
import json
import math
from decimal import Decimal
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def canonical(value):
    """RFC 8785 encoding, including ECMAScript number and UTF-16 key ordering."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        value.encode("utf-8", errors="strict")
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        if abs(value) > 9007199254740991:
            raise ValueError("Unsafe JSON integer")
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Non-finite JSON number")
        if value == 0:
            return "0"
        digits = repr(value).lower()
        if 1e-6 <= abs(value) < 1e21:
            text = format(Decimal(digits), "f")
            return text.rstrip("0").rstrip(".") if "." in text else text
        mantissa, exponent = digits.split("e")
        exponent = int(exponent)
        return mantissa.removesuffix(".0") + "e" + ("+" if exponent >= 0 else "-") + str(abs(exponent))
    if isinstance(value, list):
        return "[" + ",".join(canonical(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(canonical(k) + ":" + canonical(value[k]) for k in sorted(value, key=lambda k: k.encode("utf-16-be"))) + "}"
    raise ValueError("Not a JSON value")


def verify_card(card, trusted_jwks, *, expected_origin):
    value = copy.deepcopy(card)
    if "supportedInterfaces" in value:
        signatures = value.pop("signatures", [])
        if len(signatures) != 1:
            raise ValueError("Expected one card signature")
        signature = signatures[0]
        protected, encoded_signature = signature["protected"], signature["signature"]
        extension = next(e for e in value["capabilities"]["extensions"] if e["uri"] == "urn:luotsi:agent-surface:v1")
        origin = extension["params"]["canonicalOrigin"]
        endpoints = [i["url"] for i in value["supportedInterfaces"]]
    else:
        extension = value["_meta"]["fi.valioaimo/gateway"]
        protected, detached, encoded_signature = extension.pop("signature").split(".")
        if detached:
            raise ValueError("Expected detached JWS")
        origin = extension["canonicalOrigin"]
        endpoints = [r["url"] for r in value["remotes"]]
    if origin != expected_origin or any((urlsplit(e).scheme + "://" + urlsplit(e).netloc) != expected_origin for e in endpoints):
        raise ValueError("Card origin or endpoint does not match trusted origin")

    def decode(text):
        return base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)

    header = json.loads(decode(protected))
    if set(header) != {"alg", "kid"} or header["alg"] != "EdDSA":
        raise ValueError("Unsupported card signature")
    keys = [k for k in trusted_jwks["keys"] if k["kid"] == header["kid"]]
    if len(keys) != 1 or keys[0].get("kty") != "OKP" or keys[0].get("crv") != "Ed25519":
        raise ValueError("Unknown card signing key")
    encoded = base64.urlsafe_b64encode(canonical(value).encode()).rstrip(b"=").decode()
    Ed25519PublicKey.from_public_bytes(decode(keys[0]["x"])).verify(decode(encoded_signature), (protected + "." + encoded).encode())
    return value
