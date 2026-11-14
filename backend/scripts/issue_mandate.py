"""Hotel buyer admin: generate a private issuer key, issue mandates and signed revocations."""
import argparse
import fcntl
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from app import audit
from app.config import BACKEND
from app.mandates import sign_jwt
from app.signing import b64


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keys-dir", type=Path, default=BACKEND / "var/buyer-keys")
    parser.add_argument("--issuer", default="urn:buyer:hotel-chain")
    parser.add_argument("--customer")
    parser.add_argument("--agent")
    parser.add_argument("--order-limit", type=float)
    parser.add_argument("--daily-limit", type=float)
    parser.add_argument("--days", type=float, default=1)
    parser.add_argument("--category", action="append")
    parser.add_argument("--revoke", action="append", help="Mandate jti to revoke; repeat for several IDs")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    import math
    if not args.revoke and (not args.customer or not args.agent or
            any(v is None or not math.isfinite(v) or v < 0 for v in (args.order_limit, args.daily_limit)) or
            not math.isfinite(args.days) or args.days <= 0):
        parser.error("Issuance needs --customer --agent --order-limit --daily-limit and positive --days")
    args.keys_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Issuer-specific filenames and a lock protect first use in parallel admin shells.
    import hashlib
    key_path = args.keys_dir / (hashlib.sha256(args.issuer.encode()).hexdigest() + ".pem")
    with (args.keys_dir / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not key_path.exists():
            key = Ed25519PrivateKey.generate()
            with os.fdopen(os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
                file.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    kid = hashlib.sha256(key.public_key().public_bytes_raw()).hexdigest()[:24]
    jwk = {"kty": "OKP", "crv": "Ed25519", "kid": kid, "alg": "EdDSA", "use": "sig",
           "x": b64(key.public_key().public_bytes_raw())}
    now = int(audit.utcnow().timestamp())
    if args.revoke:
        claims = {"iss": args.issuer, "jti_revoked": args.revoke, "iat": now}
        token = sign_jwt(key, claims, kid=kid, typ="JWT")
    else:
        subject = {"agent_id": args.agent, "customer_id": args.customer, "max_auto_total_eur": args.order_limit,
                   "max_auto_daily_total_eur": args.daily_limit}
        if args.category:
            subject["categories"] = args.category
        claims = {"iss": args.issuer, "sub": args.agent, "jti": str(uuid4()), "nbf": now,
                  "exp": now + int(args.days * 86400), "vc": {"@context": ["https://www.w3.org/ns/credentials/v2"],
                  "type": ["VerifiableCredential", "PurchasingMandate"], "credentialSubject": subject}}
        token = sign_jwt(key, claims, kid=kid)
    args.out.write_text(token + "\n")
    os.chmod(args.out, 0o600)
    print(json.dumps({"issuer": args.issuer, "public_jwk": jwk, "jti": claims.get("jti"), "file": str(args.out)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
