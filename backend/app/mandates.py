"""Buyer-issued Ed25519 VC-JWT authority, shared by REST, MCP and approvals."""
import hashlib
import json
import math
from datetime import datetime, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import audit
from .db import DomainError
from .models import AgentMandate, MandateRecord
from .signing import b64, unb64


def sign_jwt(private, claims, *, kid, typ="vc+jwt"):
    header = b64(json.dumps({"alg": "EdDSA", "typ": typ, "kid": kid}, separators=(",", ":")).encode())
    payload = b64(json.dumps(claims, separators=(",", ":"), allow_nan=False).encode())
    signing_input = (header + "." + payload).encode("ascii")
    return header + "." + payload + "." + b64(private.sign(signing_input))


def invalid(check):
    raise DomainError("MANDATE_INVALID", f"Mandate check failed: {check}", 403, {"check": check})


def decode(token):
    try:
        h, p, s = token.split(".")
        header, claims = json.loads(unb64(h)), json.loads(unb64(p))
        if not isinstance(header, dict) or not isinstance(claims, dict):
            invalid("format")
        if header.get("alg") != "EdDSA" or not isinstance(header.get("kid"), str):
            invalid("signature")
        return header, claims, (h + "." + p).encode("ascii"), unb64(s)
    except DomainError:
        raise
    except Exception:
        invalid("format")


def check_signature(header, message, signature, keys):
    matches = [key for key in keys if key.kid == header["kid"]]
    if len(matches) != 1:
        invalid("signature")
    try:
        Ed25519PublicKey.from_public_bytes(unb64(matches[0].x)).verify(signature, message)
    except Exception:
        invalid("signature")


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class Mandates:
    def __init__(self, db, policy, catalog):
        self.db, self.policy, self.catalog = db, policy, catalog

    def stored(self, conn, kind, object_id):
        row = conn.execute("SELECT jwt FROM object_mandates JOIN mandate_credentials USING(digest) "
                           "WHERE kind=? AND object_id=?", (kind, object_id)).fetchone()
        return row[0] if row else None

    def bind(self, conn, kind, object_id, token):
        if token is None:
            return
        digest = hashlib.sha256(token.encode()).hexdigest()
        conn.execute("INSERT OR IGNORE INTO mandate_credentials VALUES(?,?)", (digest, token))
        conn.execute("INSERT INTO object_mandates VALUES(?,?,?)", (kind, object_id, digest))

    def authorize(self, conn, token, agent_id, customer_id, *, products=(), bound=None, policy=None):
        policy = policy or self.policy.get()
        if bound:
            original = self.stored(conn, *bound)
            if original is not None:
                # Authority attached to a quote/order cannot be swapped to bypass revocation.
                if token is not None and token != original:
                    self.verify(conn, token, agent_id, customer_id, products=products, policy=policy)
                    invalid("binding")
                token = original
        if token is None:
            self.policy.check_customer(agent_id, customer_id, policy)
            return policy, None, None
        record, subject = self.verify(conn, token, agent_id, customer_id, products=products, policy=policy)
        effective = policy.model_copy(deep=True)
        row = effective.agents.get(agent_id)
        if row is not None:
            self.policy.check_customer(agent_id, customer_id, policy)
        order_limit = min(subject["max_auto_total_eur"], policy.approval.max_auto_total_eur,
                          row.max_auto_total_eur if row and row.max_auto_total_eur is not None else float("inf"))
        daily_limit = min(subject["max_auto_daily_total_eur"], row.max_auto_daily_total_eur if row else float("inf"))
        effective.agents[agent_id] = AgentMandate(customer_ids=[customer_id], max_auto_total_eur=order_limit,
                                                 max_auto_daily_total_eur=daily_limit)
        record.limits = {"max_auto_total_eur": order_limit, "max_auto_daily_total_eur": daily_limit}
        return effective, record, token

    def verify(self, conn, token, agent_id, customer_id, *, products=(), policy=None):
        header, claims, message, signature = decode(token)
        if header.get("typ") != "vc+jwt":
            invalid("format")
        try:
            subject = claims["vc"]["credentialSubject"]
            if (claims["vc"]["@context"] != ["https://www.w3.org/ns/credentials/v2"] or
                    claims["vc"]["type"] != ["VerifiableCredential", "PurchasingMandate"] or
                    not isinstance(subject, dict) or not all(isinstance(claims.get(k), str) and claims[k]
                                                            for k in ("iss", "sub", "jti")) or
                    not isinstance(subject.get("customer_id"), str) or
                    not all(number(subject.get(k)) and subject[k] >= 0
                            for k in ("max_auto_total_eur", "max_auto_daily_total_eur"))):
                invalid("format")
        except (KeyError, TypeError):
            invalid("format")
        customer = (policy or self.policy.get()).customers.get(subject["customer_id"])
        keys = customer.mandate_issuers.get(claims["iss"], []) if customer else []
        if not keys:
            invalid("issuer")
        check_signature(header, message, signature, keys)
        now = audit.utcnow().timestamp()
        if not number(claims.get("nbf")) or not number(claims.get("exp")):
            invalid("validity")
        if now < claims["nbf"]:
            invalid("nbf")
        if now >= claims["exp"]:
            invalid("exp")
        if conn.execute("SELECT 1 FROM mandate_revocations WHERE issuer=? AND mandate_id=?",
                        (claims["iss"], claims["jti"])).fetchone():
            invalid("revoked")
        if claims["sub"] != agent_id or subject.get("agent_id") != agent_id:
            invalid("sub")
        if subject["customer_id"] != customer_id:
            invalid("customer")
        categories = subject.get("categories")
        if categories is not None:
            if not isinstance(categories, list) or not categories or any(not isinstance(c, str) or not c for c in categories):
                invalid("category")
            if any(self.catalog.get(pid).category not in categories for pid in products):
                invalid("category")
        try:
            valid_until = datetime.fromtimestamp(claims["exp"], timezone.utc)
        except (ValueError, OverflowError, OSError):
            invalid("exp")
        return MandateRecord(id=claims["jti"], issuer=claims["iss"], limits={}, valid_until=valid_until), subject

    def search(self, query, required_claims, exclude_allergens, limit, *, token, agent_id):
        if token is None:
            return self.catalog.search(query, required_claims, exclude_allergens, limit)[0]
        actor = f"agent:{agent_id}"
        try:
            claims = decode(token)[1]
            try:
                customer_id = claims["vc"]["credentialSubject"]["customer_id"]
            except (KeyError, TypeError):
                invalid("format")
            with self.db.connect(write=True) as conn:
                _, record, _ = self.authorize(conn, token, agent_id, customer_id)
                products = self.catalog.search(query, required_claims, exclude_allergens, limit)[0]
                categories = claims["vc"]["credentialSubject"].get("categories")
                if categories is not None:
                    products = [p for p in products if p.category in categories]
                self.verify(conn, token, agent_id, customer_id, products=[p.id for p in products])
                audit.append(conn, actor, "LOOKUP_SEARCH", "Searched the local catalog under buyer authority.",
                             data={**evidence(record), "products": [p.id for p in products],
                                   "live_calls": {"price": 0, "availability": 0}})
            return products
        except DomainError as error:
            with self.db.connect(write=True) as conn:
                audit.append(conn, actor, "LOOKUP_SEARCH_FAILED", error.message,
                             data={"error": error.code, **error.data, "live_calls": {"price": 0, "availability": 0}})
            raise

    def revoke(self, token, actor):
        try:
            header, claims, message, signature = decode(token)
            issuer, ids, issued_at = claims.get("iss"), claims.get("jti_revoked"), claims.get("iat")
            if (not isinstance(issuer, str) or not issuer or not isinstance(ids, list) or not ids or
                    any(not isinstance(item, str) or not item for item in ids) or
                    not number(issued_at) or issued_at > audit.utcnow().timestamp()):
                invalid("revocation")
            with self.db.connect(write=True) as conn:
                keys = [key for c in self.policy.get().customers.values() for key in c.mandate_issuers.get(issuer, [])]
                # An issuer can be registered for several customers with the same key.
                keys = list({(key.kid, key.x): key for key in keys}.values())
                check_signature(header, message, signature, keys)
                for mandate_id in ids:
                    conn.execute("INSERT OR IGNORE INTO mandate_revocations VALUES(?,?,?)",
                                 (issuer, mandate_id, audit.utcnow().isoformat()))
                audit.append(conn, actor, "MANDATE_REVOKED", "Buyer revoked purchasing authority.",
                             data={"issuer": issuer, "jti_revoked": ids})
            return {"issuer": issuer, "jti_revoked": ids}
        except DomainError:
            raise DomainError("REVOCATION_INVALID", "Revocation signature or claims are invalid", 422) from None


def evidence(record):
    return {"mandate": record.model_dump(mode="json")} if record else {}
