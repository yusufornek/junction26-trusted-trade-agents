"""Detached, base64url-encoded-payload JWS (RFC 7515), using Ed25519."""
import base64
import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .canonical import canonical_json, decimal_string


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def unb64(value):
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def quote_payload(quote):
    """Reconstruct from the JSON resource, also used by the standalone verifier."""
    q = quote.model_dump(mode="json") if hasattr(quote, "model_dump") else quote
    labels = {k: q[k] for k in ("supplier_id", "synthetic", "environment") if k in q and (q[k] not in ("valio", "live", False))}
    return {**labels, "id": q["id"], "customer_id": q["customer_id"], "deliver_by": q["deliver_by"],
            "lines": [{"product_id": line["product_id"], "units": decimal_string(line["units_needed"]),
                       "unit_price": decimal_string(line["unit_price"]), "currency": line["currency"],
                       "gtin": line.get("gtin"), "provenance": line["provenance"]} for line in q["lines"]],
            "total": decimal_string(q["total"]), "expires_at": q["expires_at"],
            "price_commitment": q["price_commitment"]}


def quote_integrity(quote):
    """Immutable resource evidence, separate from the spec 10 promise payload."""
    q = quote.model_dump(mode="json") if hasattr(quote, "model_dump") else quote
    def immutable(value):
        if isinstance(value, dict):
            return {k: immutable(v) for k, v in value.items()}
        if isinstance(value, list):
            return [immutable(v) for v in value]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return decimal_string(value)
        return value
    evidence = {k: v for k, v in q.items() if k not in {"signature", "status", "live_calls", "expiration_reason"}}
    if "links" in evidence:
        # Original link identifiers stay signed; read-time forward navigation is mutable.
        evidence["links"] = {k: v for k, v in evidence["links"].items()
                             if k not in {"quote_ids", "order_ids", "event_ids", "audit_checkpoints", "trace_ids", "task_ids", "connection_contract"}}
    return immutable(evidence)


def verify_quote(quote, jwks):
    """Verify versioned evidence and both previously issued, unversioned formats."""
    q = quote.model_dump(mode="json") if hasattr(quote, "model_dump") else quote
    signature = q["signature"]
    header = json.loads(unb64(signature.split(".")[0]))
    payload = quote_payload(q)
    if "quote_version" in header:
        verify_jws(signature, payload, jwks)
        expected = hashlib.sha256(canonical_json(quote_integrity(q))).hexdigest()
        if header["quote_integrity"] != expected:
            raise ValueError("Quote evidence does not match its protected digest")
    else:
        try:
            verify_jws(signature, payload, jwks)
        except InvalidSignature:
            # Earlier builds added this member without a version. Verify the
            # original bytes; never re-sign or silently invalidate a live promise.
            verify_jws(signature, {**payload, "integrity": quote_integrity(q)}, jwks)
    return True


def receipt_payload(order):
    o = order.model_dump(mode="json") if hasattr(order, "model_dump") else order
    labels = {k: o[k] for k in ("supplier_id", "synthetic", "environment", "execution") if k in o and o[k] not in ("valio", "live", False)}
    return {**labels, "order_id": o["id"], **{k: o[k] for k in (
        "quote_id", "quote_signature_digest", "customer_id", "agent_id", "status", "currency",
        "decided_at", "decided_by_kind", "commitment")}, "total": decimal_string(o["total"])}


def verify_jws(jws, payload, jwks):
    protected, detached, signature = jws.split(".")
    header = json.loads(unb64(protected))
    legacy = set(header) == {"alg", "kid"}
    versioned_quote = (set(header) == {"alg", "kid", "quote_version", "quote_integrity"}
                       and header["quote_version"] == 2 and isinstance(header["quote_integrity"], str))
    if detached or not (legacy or versioned_quote) or header["alg"] != "EdDSA":
        raise ValueError("Unsupported detached JWS header")
    matches = [k for k in jwks["keys"] if k.get("kid") == header["kid"]]
    if len(matches) != 1:
        raise ValueError("Unknown or ambiguous kid")
    key = matches[0]
    if key.get("kty") != "OKP" or key.get("crv") != "Ed25519" or key.get("alg", "EdDSA") != "EdDSA":
        raise ValueError("Unsupported JWK")
    Ed25519PublicKey.from_public_bytes(unb64(key["x"])).verify(
        unb64(signature), (protected + "." + b64(canonical_json(payload))).encode("ascii"))
    return True


class SigningKeys:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._lock():
            if not self._registry_path.exists():
                self._rotate({"active": None, "keys": []}, datetime.now(timezone.utc))

    @property
    def _registry_path(self):
        return self.directory / "keys.json"

    @contextmanager
    def _lock(self):
        with (self.directory / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _registry(self):
        return json.loads(self._registry_path.read_text())

    def _rotate(self, registry, now):
        for entry in registry["keys"]:
            if entry["kid"] == registry["active"]:
                entry["publish_until"] = (now + timedelta(days=7)).isoformat()
        private = Ed25519PrivateKey.generate()
        kid = str(uuid4())
        pem = self.directory / (kid + ".pem")
        with os.fdopen(os.open(pem, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
            file.write(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))
        registry["keys"].append({"kid": kid, "kty": "OKP", "crv": "Ed25519", "alg": "EdDSA", "use": "sig",
                                 "x": b64(private.public_key().public_bytes_raw())})
        registry["active"] = kid
        temporary = self.directory / ("keys-" + kid + ".tmp")
        temporary.write_text(json.dumps(registry))
        os.chmod(temporary, 0o600)
        os.replace(temporary, self._registry_path)
        return kid

    def rotate(self, now=None):
        with self._lock():
            return self._rotate(self._registry(), now or datetime.now(timezone.utc))

    def jwks(self, *, include_retired=False):
        registry = self._registry()
        now = datetime.now(timezone.utc)
        # The historical endpoint retains verification keys indefinitely; the current
        # quote key set preserves the spec 10 seven-day publication window.
        return {"keys": [{k: v for k, v in entry.items() if k != "publish_until"}
                         for entry in registry["keys"] if include_retired or entry["kid"] == registry["active"] or
                         datetime.fromisoformat(entry["publish_until"]) > now]}

    def sign_quote(self, quote):
        # A standard detached JWS verifier still uses exactly the spec payload.
        # The protected extension authenticates the additional decision evidence.
        return self.sign(quote_payload(quote), protected_fields={
            "quote_version": 2,
            "quote_integrity": hashlib.sha256(canonical_json(quote_integrity(quote))).hexdigest()})

    def sign(self, payload, *, protected_fields=None):
        registry = self._registry()
        kid = registry["active"]
        private = serialization.load_pem_private_key((self.directory / (kid + ".pem")).read_bytes(), password=None)
        protected = b64(canonical_json({"alg": "EdDSA", "kid": kid, **(protected_fields or {})}))
        signature = private.sign((protected + "." + b64(canonical_json(payload))).encode("ascii"))
        return protected + ".." + b64(signature)


def signature_digest(signature):
    return hashlib.sha256(signature.encode("ascii")).hexdigest()
