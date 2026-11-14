"""Portable, signed transaction evidence with hash-only cross-customer audit links."""
import io
import json
from hashlib import sha256
from typing import Literal
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from . import audit, ubl
from .auth import Principal, resolve_principal
from .canonical import canonical_json
from .db import DomainError
from .models import Order, Quote


class EvidenceFile(BaseModel):
    name: str
    sha256: str
    href: str


class EvidenceManifest(BaseModel):
    schema_version: int
    order_id: str
    quote_id: str
    customer_id: str
    environment: str
    supplier_acceptance: Literal["simulated"]
    bundle_url: str
    files: list[EvidenceFile]
    signature: str

VERIFY = """# Verify this purchase

Obtain trusted gateway JWKS independently; bundled jwks.json is not a trust root.
From backend/ (Python environment with requirements.txt installed):

```sh
.venv/bin/python scripts/verify_evidence.py purchase.zip --trusted-jwks /trusted/gateway-jwks.json
.venv/bin/python scripts/verify_evidence.py purchase.zip --trusted-jwks https://gateway.example/.well-known/jwks-history.json --rpc-url https://polygon-rpc.example --xsd-dir vendor/ubl-2.1
```

The first command is offline. URL trust sources and RPC use the network and are
reported separately. Omit --rpc-url to report anchor_status=not_checked. UBL uses
the vendored official schemas by default, with all schema access restricted to local files.
Retain independently obtained historical JWKS for retired signing keys.
Hash-only audit links reveal no other customer's payload; the signed manifest
binds the disclosed subset and all artifacts. A matching anchor proves a recorded
checkpoint, not supplier acceptance. gateway-simulated means no supplier order was sent.
"""


def authorize_order(gateway, order_id, principal):
    with gateway.db.connect() as conn:
        row = conn.execute("SELECT data FROM orders WHERE id=?", (order_id,)).fetchone()
        order = Order.model_validate_json(row[0]) if row else None
        buyer = principal.kind == "buyer_user" and order and gateway.buyer.visible(
            principal, order.agent_id, [order.customer_id])
        if order is None or not (principal.operator or buyer or (
                principal.kind == "agent" and order.agent_id == principal.id)):
            raise DomainError("ORDER_NOT_FOUND", "Order was not found", 404)
    return order


def build_bundle(gateway, order_id, principal):
    authorize_order(gateway, order_id, principal)
    gateway.orders.expire()
    gateway.limiter.consume(principal.id, "read")
    with gateway.db.connect() as conn:
        conn.execute("BEGIN")
        row = conn.execute("SELECT data FROM orders WHERE id=?", (order_id,)).fetchone()
        order = Order.model_validate_json(row[0])
        raw_order = json.loads(row[0])
        raw_quote = json.loads(conn.execute("SELECT data FROM quotes WHERE id=?", (order.quote_id,)).fetchone()[0])
        quote = Quote.model_validate(raw_quote)
        links = []
        for row in conn.execute("SELECT * FROM audit ORDER BY id"):
            data = json.loads(row["data"])
            related = (row["order_id"] == order.id or row["quote_id"] == quote.id or
                       (row["action"] == "LOOKUP" and quote.need_ref and data.get("lookup_id") == quote.need_ref))
            link = {"seq": row["id"], "prev_hash": row["prev_hash"], "hash": row["hash"]}
            if related:
                link["entry"] = audit.entry_payload(row)
            links.append(link)
        checkpoint = {"entries": len(links), "head_hash": links[-1]["hash"] if links else audit.GENESIS,
                      "at": audit.utcnow().isoformat()}
        checkpoint["jws"] = gateway.signing.sign(checkpoint)
        # Keep even a mismatching checkpoint: omitting it could hide a rewritten
        # or truncated local chain from the independent verifier.
        anchors = [dict(r) for r in conn.execute(
            "SELECT entries,head_hash,chain_id,tx_hash,block_number,status,explorer_url,at FROM audit_anchors ORDER BY id DESC")]
    files = {"quote.json": canonical_json(raw_quote), "receipt.json": canonical_json(raw_order),
             "audit.json": canonical_json(links), "checkpoint.json": canonical_json(checkpoint),
             "order.xml": ubl.export(order, quote).encode(),
             "order-response.xml": ubl.export(order, quote, "response").encode(),
             "jwks.json": canonical_json(gateway.signing.jwks(include_retired=True)), "VERIFY.md": VERIFY.encode()}
    if anchors:
        anchor = anchors[0]
        files["anchor.json"] = canonical_json({**anchor, "checkpoint_digest": anchor["head_hash"],
                                               "confirmation_status": anchor["status"]})
    base = f"/v1/orders/{order_id}/evidence"
    snapshot = sha256(b"".join(sha256(body).digest() for _, body in sorted(files.items()))).hexdigest()
    manifest = {"schema_version": 1, "order_id": order.id, "quote_id": quote.id, "customer_id": order.customer_id,
                "environment": (order.context or {}).get("environment", "live"),
                "supplier_acceptance": "simulated", "bundle_url": base + "?snapshot=" + snapshot,
                "files": [{"name": name, "sha256": sha256(body).hexdigest(),
                           "href": base + "?snapshot=" + snapshot + "&artifact=" + name}
                          for name, body in sorted(files.items())]}
    manifest["signature"] = gateway.signing.sign(manifest)
    files["manifest.json"] = canonical_json(manifest)
    with gateway.db.connect(write=True) as conn:
        conn.execute("INSERT OR IGNORE INTO evidence_snapshots VALUES(?,?,?)", (snapshot, order_id, zip_bundle(files)))
    return manifest, files


def zip_bundle(files):
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return buffer.getvalue()


router = APIRouter(tags=["Evidence"])


@router.get("/.well-known/jwks-history.json", response_model=dict,
            summary="All public verification keys, including retired keys for historical evidence")
def historical_keys(request: Request):
    return request.app.state.gateway.signing.jwks(include_retired=True)


@router.get("/v1/orders/{order_id}/evidence", response_model=None,
            summary="Download signed purchase evidence ZIP, or its manifest and authenticated artifact links",
            description="Order agent, scoped buyer user or operator. Other orders return 404. Includes hash-only audit links for other customers. Independently trusted JWKS are required for verification.",
            responses={200: {"content": {"application/zip": {"schema": {"type": "string", "format": "binary"}},
                                         "application/json": {"schema": {"anyOf": [{"type": "object"}, {"type": "array"}]},
                                                              "description": "Signed manifest, or the JSON artifact selected by filename"},
                                         "application/xml": {"schema": {"type": "string"}},
                                         "text/markdown": {"schema": {"type": "string"}}}}})
def evidence(order_id: str, request: Request, format: Literal["zip", "json"] = "zip",
             artifact: str | None = Query(default=None, description="Artifact filename from the manifest; same access checks apply"),
             snapshot: str | None = Query(default=None, pattern="^[0-9a-f]{64}$", description="Immutable evidence snapshot referenced by a manifest"),
             principal: Principal = Depends(resolve_principal)):
    gateway = request.app.state.gateway
    if snapshot is None:
        manifest, files = build_bundle(gateway, order_id, principal)
    else:
        authorize_order(gateway, order_id, principal)
        gateway.limiter.consume(principal.id, "read")
        with gateway.db.connect() as conn:
            stored = conn.execute("SELECT bundle FROM evidence_snapshots WHERE id=? AND order_id=?", (snapshot, order_id)).fetchone()
        if stored is None:
            raise DomainError("ORDER_NOT_FOUND", "Evidence snapshot was not found", 404)
        with ZipFile(io.BytesIO(stored[0])) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        manifest = json.loads(files["manifest.json"])
    if artifact is not None:
        if artifact not in files:
            raise DomainError("ORDER_NOT_FOUND", "Artifact was not found", 404)
        media = "application/json" if artifact.endswith(".json") else "application/xml" if artifact.endswith(".xml") else "text/markdown"
        return Response(files[artifact], media_type=media)
    if format == "json":
        return JSONResponse(manifest)
    return Response(zip_bundle(files), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{order_id}-evidence.zip"'})
