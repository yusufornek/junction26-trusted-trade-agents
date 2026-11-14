#!/usr/bin/env python3
"""Verify portable purchase evidence against independently obtained gateway keys."""
import argparse
import json
import sys
from hashlib import sha256
from pathlib import Path
from zipfile import ZipFile

import httpx
import xmlschema

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.audit import GENESIS, entry_hash
from app.anchoring import PREFIX
from app.signing import receipt_payload, signature_digest, unb64, verify_jws, verify_quote


def verify_bundle(bundle, trusted_jwks, *, rpc_url=None, xsd_dir=None):
    report = {"signature_valid": False, "trusted_issuer_match": False, "audit_consistent": False,
              "anchor_status": "not_checked", "ubl_schema_valid": False, "supplier_acceptance": "simulated",
              "artifact_integrity": False, "binding_valid": False, "offline_checks": [], "network_checks": [],
              "errors": []}
    def error(check, artifact, detail):
        report["errors"].append({"check": check, "artifact": artifact, "detail": detail})

    try:
        with ZipFile(bundle) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or sum(i.file_size for i in archive.infolist()) > 50_000_000:
                raise ValueError("Duplicate artifacts or oversized bundle")
            files = {name: archive.read(name) for name in names}
        manifest, quote, receipt, bundled, checkpoint, links = [json.loads(files[name]) for name in
            ("manifest.json", "quote.json", "receipt.json", "jwks.json", "checkpoint.json", "audit.json")]
        if not all(isinstance(value, dict) for value in (manifest, quote, receipt, bundled, checkpoint)) or not isinstance(links, list):
            raise ValueError("Unexpected artifact structure")
    except Exception as exc:
        error("bundle", "bundle.zip", type(exc).__name__)
        return report, 1

    report["offline_checks"] += ["signature_valid", "audit_consistent", "artifact_integrity", "binding_valid",
                                 "ubl_schema_valid", "supplier_acceptance"]
    signatures = [("quote.json", quote.get("signature"), lambda keys: verify_quote(quote, keys)),
                  ("receipt.json", receipt.get("receipt"), lambda keys: verify_jws(receipt["receipt"], receipt_payload(receipt), keys)),
                  ("checkpoint.json", checkpoint.get("jws"), lambda keys: verify_jws(checkpoint["jws"],
                    {k: v for k, v in checkpoint.items() if k != "jws"}, keys)),
                  ("manifest.json", manifest.get("signature"), lambda keys: verify_jws(manifest["signature"],
                    {k: v for k, v in manifest.items() if k != "signature"}, keys))]
    trusted = None
    try:
        is_url = isinstance(trusted_jwks, str) and trusted_jwks.startswith(("http://", "https://"))
        report["network_checks" if is_url else "offline_checks"].append("trusted_issuer_match")
        if isinstance(trusted_jwks, dict):
            trusted = trusted_jwks
        elif is_url:
            with httpx.Client(timeout=10, trust_env=False) as client:
                response = client.get(trusted_jwks)
                response.raise_for_status()
                trusted = response.json()
        else:
            trusted = json.loads(Path(trusted_jwks).read_text())
    except Exception as exc:
        error("trusted_issuer_match", "trusted JWKS", type(exc).__name__)
    report["signature_valid"] = trusted is not None
    report["trusted_issuer_match"] = trusted is not None
    for name, signature, verify in signatures:
        try:
            kid = json.loads(unb64(signature.split(".")[0]))["kid"]
            def material(keys):
                matches = [k for k in keys["keys"] if k.get("kid") == kid]
                if len(matches) != 1:
                    raise ValueError("Unknown or ambiguous trusted key")
                return {**{k: matches[0].get(k) for k in ("kid", "kty", "crv", "x")},
                        "alg": matches[0].get("alg", "EdDSA")}
            if material(trusted) != material(bundled):
                raise ValueError("Bundled key differs from independently trusted key")
        except Exception as exc:
            report["trusted_issuer_match"] = False
            error("trusted_issuer_match", name, str(exc) or type(exc).__name__)
        try:
            # Bundled keys are only compared, never used as a verification key source.
            verify(trusted)
        except Exception as exc:
            report["signature_valid"] = False
            error("signature_valid", name, type(exc).__name__)

    try:
        listed = manifest["files"]
        if len({f["name"] for f in listed}) != len(listed) or {f["name"] for f in listed} != set(files) - {"manifest.json"}:
            raise ValueError("Manifest inventory differs from archive")
        report["artifact_integrity"] = True
        for item in listed:
            if sha256(files[item["name"]]).hexdigest() != item["sha256"]:
                report["artifact_integrity"] = False
                error("artifact_integrity", item["name"], "SHA-256 mismatch")
    except Exception as exc:
        error("artifact_integrity", "manifest.json", str(exc))

    try:
        previous = GENESIS
        for seq, link in enumerate(links, 1):
            if link["seq"] != seq or link["prev_hash"] != previous:
                raise ValueError(f"entry {seq}: sequence or previous hash mismatch")
            if "entry" in link and (link["entry"]["seq"] != seq or entry_hash(previous, link["entry"]) != link["hash"]):
                raise ValueError(f"entry {seq}: payload hash mismatch")
            if len(link["hash"]) != 64 or any(c not in "0123456789abcdef" for c in link["hash"]):
                raise ValueError(f"entry {seq}: invalid hash")
            previous = link["hash"]
        if len(links) != checkpoint["entries"] or previous != checkpoint["head_hash"]:
            raise ValueError("Signed checkpoint does not match audit links")
        if "anchor.json" in files:
            anchor = json.loads(files["anchor.json"])
            n = anchor["entries"]
            if not 0 <= n <= len(links) or anchor["head_hash"] != (links[n - 1]["hash"] if n else GENESIS):
                raise ValueError("Anchored checkpoint is inconsistent with the audit links")
        report["audit_consistent"] = True
    except Exception as exc:
        error("audit_consistent", "audit.json", str(exc))

    try:
        if not (manifest["schema_version"] == 1 and manifest["order_id"] == receipt["id"] and
                manifest["quote_id"] == receipt["quote_id"] == quote["id"] and
                manifest["customer_id"] == receipt["customer_id"] == quote["customer_id"] and
                manifest["environment"] == (receipt.get("context") or {}).get("environment", "live") ==
                (quote.get("context") or {}).get("environment", "live") and
                receipt["quote_signature_digest"] == signature_digest(quote["signature"])):
            raise ValueError("Order/customer/environment/quote binding mismatch")
        report["binding_valid"] = True
        if receipt["commitment"] != "gateway-simulated" or manifest["supplier_acceptance"] != "simulated":
            raise ValueError("No authenticated supplier acknowledgement")
    except Exception as exc:
        error("binding_valid", "manifest.json", str(exc))

    try:
        directory = Path(xsd_dir) if xsd_dir else Path(__file__).resolve().parents[1] / "vendor/ubl-2.1"
        for name, kind in (("order.xml", "Order"), ("order-response.xml", "OrderResponse")):
            schema = xmlschema.XMLSchema(directory / f"xsd/maindoc/UBL-{kind}-2.1.xsd", allow="local")
            schema.validate(files[name], use_location_hints=False)
        report["ubl_schema_valid"] = True
    except Exception as exc:
        error("ubl_schema_valid", "order.xml / order-response.xml", type(exc).__name__)

    if rpc_url and "anchor.json" in files:
        report["network_checks"].append("anchor_status")
        report["anchor_status"] = "pending"
        try:
            anchor = json.loads(files["anchor.json"])
            n = anchor["entries"]
            digest = links[n - 1]["hash"] if n > 0 else GENESIS
            if not 0 <= n <= len(links) or anchor["head_hash"] != digest or anchor["checkpoint_digest"] != digest:
                raise ValueError("Anchor checkpoint mismatch")
            with httpx.Client(timeout=10, trust_env=False) as client:
                def rpc(method, params):
                    response = client.post(rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
                    response.raise_for_status()
                    body = response.json()
                    if body.get("error"):
                        raise ValueError("RPC error")
                    return body["result"]
                if int(rpc("eth_chainId", []), 16) != anchor["chain_id"]:
                    raise ValueError("RPC chain mismatch")
                transaction = rpc("eth_getTransactionByHash", [anchor["tx_hash"]])
                confirmation = rpc("eth_getTransactionReceipt", [anchor["tx_hash"]])
            if transaction is not None and confirmation is not None:
                expected = "0x" + f"{PREFIX}{n}:{digest}".encode().hex()
                if (transaction.get("hash", "").lower() != anchor["tx_hash"].lower() or
                        transaction.get("input", "").lower() != expected.lower() or
                        confirmation.get("transactionHash", "").lower() != anchor["tx_hash"].lower() or
                        int(confirmation["status"], 16) != 1 or confirmation.get("blockNumber") is None or
                        transaction.get("blockNumber") != confirmation["blockNumber"]):
                    raise ValueError("Transaction or confirmation mismatch")
                report["anchor_status"] = "matched"
            else:
                error("anchor_status", "anchor.json", "Transaction has not been confirmed")
        except Exception as exc:
            error("anchor_status", "anchor.json", str(exc))
    report["anchor_note"] = "RPC omitted" if not rpc_url else "No anchor metadata" if "anchor.json" not in files else "RPC executed"
    passed = all(report[name] for name in ("signature_valid", "trusted_issuer_match", "audit_consistent",
                 "ubl_schema_valid", "artifact_integrity", "binding_valid")) and not report["errors"]
    return report, 0 if passed else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--trusted-jwks", required=True, help="Independently trusted JWKS file or HTTP(S) URL")
    parser.add_argument("--rpc-url")
    parser.add_argument("--xsd-dir", type=Path)
    args = parser.parse_args(argv)
    report, code = verify_bundle(args.bundle, args.trusted_jwks, rpc_url=args.rpc_url, xsd_dir=args.xsd_dir)
    print(json.dumps(report, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
