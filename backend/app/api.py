import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, Request, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.exceptions import RequestValidationError

from . import audit as audit_log
from .auth import Credentials, Principal, bearer, require_operator, resolve_principal
from .config import Settings
from .db import DomainError
from .models import (ApprovalItem, ApprovalRequest, AuditEntry, CancelRequest, ErrorResponse, Health,
                     LookupResult, NeedSpec, Order, OrderRequest, OrderTimeline, Product, Quote, QuoteRequest)
from .models import PolicyResource, PolicyResetRequest, PolicyUpdateRequest, Stats
from .models import JWKSet, AuditHead, AuditVerification, AuditAnchor, RevocationRequest
from .service import Gateway
from .stats import read_stats

logger = logging.getLogger(__name__)


def create_app(gateway=None, *, settings=None, keys=None):
    settings = settings or (gateway.settings if gateway is not None else Settings.from_env())
    keys = keys or settings.keys or Credentials()
    keys.require_operator()

    @asynccontextmanager
    async def lifespan(app):
        service = gateway or Gateway(settings)
        app.state.gateway = service
        try:
            yield
        finally:
            if gateway is None:
                service.close()

    app = FastAPI(title="Valio Aimo Trusted Trade Agents", version="0.1.0", lifespan=lifespan,
                  description="MVP fixed-price gateway. REST and MCP share one core. Bearer credentials establish agent ownership and operator authority. Commitments are gateway-simulated.",
                  responses={code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 422, 502, 503)})
    app.state.keys = keys
    app.add_middleware(CORSMiddleware, allow_origin_regex=r"http://localhost(?::\d+)?", allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(DomainError)
    async def domain_error(request, error):
        content = {"code": error.code, "message": error.message}
        if error.data:
            content["data"] = error.data
        return JSONResponse(status_code=error.status, content=content,
                            headers={"WWW-Authenticate": "Bearer"} if error.status == 401 else None)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # Omit input values from errors, retaining the field location and explanation.
        message = "; ".join(f"{'.'.join(str(v) for v in e['loc'])}: {e['msg']}" for e in error.errors())
        if getattr(request.scope.get("route"), "path", None) == "/v1/orders/{order_id}/cancel":
            # Invalid JSON can precede dependency resolution. Authenticate before auditing or
            # reporting validation, preserving spec19's 401 for missing/unknown credentials.
            if getattr(request.state, "principal", None) is None:
                try:
                    resolve_principal(request, await bearer(request))
                except DomainError as auth_error:
                    return await domain_error(request, auth_error)
            # Blocking SQLite write: keep it off the event loop, and never let an audit failure replace the 422.
            try:
                await run_in_threadpool(audit_cancel_validation, request, error.body, message)
            except sqlite3.Error:
                logger.exception("Could not audit a refused cancellation")
        code = "UNKNOWN_CODE" if any(e["type"] == "UNKNOWN_CODE" for e in error.errors()) else "VALIDATION_ERROR"
        return JSONResponse(status_code=422, content={"code": code, "message": message})

    def svc(request):
        return request.app.state.gateway

    def audit_cancel_validation(request, raw_body, message):
        """Audit a cancel request refused by validation, with a normalised actor (never raw input)."""
        principal = getattr(request.state, "principal", None)
        if principal is None:
            return
        body = raw_body if isinstance(raw_body, dict) else {}
        name = body.get("cancelled_by")
        name = name.strip()[:200] if isinstance(name, str) else ""
        reason = body.get("reason") if isinstance(body.get("reason"), str) else ""
        order_id = request.path_params["order_id"]
        with svc(request).db.connect(write=True) as conn:
            row = conn.execute("SELECT quote_id,data FROM orders WHERE id=?", (order_id,)).fetchone()
            quote_id = row["quote_id"] if row else None
            inputs = {"order_id": order_id, "reason": reason, **({"quote_id": quote_id} if quote_id else {})}
            if not principal.operator and (row is None or Order.model_validate_json(row["data"]).agent_id != principal.agent_id):
                return
            actor = "human:operator" if principal.operator else f"agent:{principal.agent_id}"
            audit_log.append(conn, actor, "CANCEL_FAILED", message,
                             order_id=order_id, quote_id=quote_id,
                             data={"inputs": inputs, "stated_name": name or "unknown", "error": "VALIDATION_ERROR",
                                   "live_calls": {"price": 0, "availability": 0}})

    @app.get("/health", response_model=Health)
    def health(request: Request):
        count = svc(request).catalog.count()
        return Health(status="ok" if count else "catalog_empty", catalog_rows=count,
                      coverage=svc(request).catalog.metadata("coverage"), anchoring=svc(request).anchoring.health())

    @app.get("/.well-known/jwks.json", response_model=JWKSet,
             summary="Public Ed25519 signing keys for detached quote, receipt and audit-head JWS")
    def jwks(request: Request):
        return svc(request).signing.jwks()

    def presented_mandate(agent_mandate: Annotated[str | None, Header(description="Buyer-signed EdDSA VC-JWT; sub must equal the authenticated agent ID.")] = None):
        return agent_mandate

    @app.post("/v1/mandates/revocations", response_model=dict[str, str | list[str]], summary="Record a buyer-signed mandate revocation")
    def revoke_mandate(body: RevocationRequest, request: Request, principal: Principal = Depends(resolve_principal)):
        return svc(request).mandates.revoke(body.jwt, "human:operator" if principal.operator else f"agent:{principal.agent_id}")

    @app.post("/v1/offers", response_model=LookupResult,
              summary="Rank live offers and substitutes; propose fulfilment plans with caller policy previews")
    def offers(need: NeedSpec, request: Request, principal: Principal = Depends(resolve_principal), mandate=Depends(presented_mandate)):
        return svc(request).find_offers(need, actor=f"agent:{principal.agent_id}", mandate=mandate)

    @app.get("/v1/lookups/{lookup_id}", response_model=LookupResult, summary="Retrieve stored alternatives behind a quote's need_ref")
    def get_lookup(lookup_id: str, request: Request, principal: Principal = Depends(resolve_principal)):
        return svc(request).lookup.get(lookup_id, principal.owner)

    @app.get("/v1/policy", response_model=PolicyResource, summary="Read effective procurement policy and known warning codes")
    def get_policy(request: Request, principal: Principal = Depends(require_operator)):
        return svc(request).policy.get()

    @app.put("/v1/policy", response_model=PolicyResource, summary="Persist policy for subsequent operations and audit changed fields")
    def put_policy(body: PolicyUpdateRequest, request: Request, principal: Principal = Depends(require_operator)):
        return svc(request).policy.update(body, actor="human:operator")

    @app.post("/v1/policy/reset", response_model=PolicyResource, summary="Restore committed policy defaults with a human audit event")
    def reset_policy(body: PolicyResetRequest, request: Request, principal: Principal = Depends(require_operator)):
        return svc(request).policy.update(body, reset=True, actor="human:operator")

    @app.get("/v1/stats", response_model=Stats, summary="Operator metrics from persisted evidence; no upstream calls")
    def stats(request: Request, since: datetime | None = None, principal: Principal = Depends(require_operator)):
        service = svc(request)
        service.orders.expire()
        return read_stats(service.db, since, registered_agents=set(service.policy.get().agents))

    @app.get("/v1/products/{id}", response_model=Product)
    def product(id: str, request: Request, principal: Principal = Depends(resolve_principal)):
        return svc(request).catalog.get(id)

    @app.post("/v1/quotes", response_model=Quote, summary="Recheck selected lines or a plan_ref live and lock fixed prices",
              description="Lookup, quote and order require a customer mandate and enforce agent ownership.")
    def create_quote(body: QuoteRequest, request: Request, principal: Principal = Depends(resolve_principal), mandate=Depends(presented_mandate)):
        return svc(request).orders.create_quote(body, principal.agent_id, operator=principal.operator, mandate=mandate)

    @app.get("/v1/quotes/{id}", response_model=Quote)
    def get_quote(id: str, request: Request, principal: Principal = Depends(resolve_principal)):
        return svc(request).orders.get_quote(id, principal.owner)

    @app.post("/v1/orders", response_model=Order, summary="Accept an unexpired quote under an agent mandate",
              description="Ordering requires a mandate: unregistered agents receive 403 AGENT_NOT_REGISTERED, "
                          "with no order or reservation created. Existing idempotent orders are returned on retry. "
                          "Registered agents are subject to policy and may require human approval.")
    def place_order(body: OrderRequest, request: Request,
                    idempotency_key: Annotated[str, Header(min_length=1, description="Retry key: same quote and agent returns the same order.")],
                    principal: Principal = Depends(resolve_principal), mandate=Depends(presented_mandate)):
        return svc(request).orders.place_order(body.quote_id, idempotency_key, principal.agent_id, operator=principal.operator, mandate=mandate)

    @app.get("/v1/orders", response_model=list[Order])
    def orders(request: Request, principal: Principal = Depends(resolve_principal)):
        return svc(request).orders.list_orders(agent_id=principal.owner)

    @app.get("/v1/orders/{id}", response_model=Order)
    def get_order(id: str, request: Request, principal: Principal = Depends(resolve_principal)):
        return svc(request).orders.get_order(id, principal.owner)

    @app.get("/v1/orders/{id}/ubl", response_class=Response,
             summary="Export UBL 2.1 Order or OrderResponse; agent owns order, operator sees all",
             responses={200: {"content": {"application/xml": {"schema": {"type": "string"}}},
                              "description": "UBL 2.1 XML; party endpoint placeholders need data from Valio Aimo."}})
    def order_ubl(id: str, request: Request, document: Literal["order", "response"] = "order",
                  principal: Principal = Depends(resolve_principal)):
        xml = svc(request).orders.get_order_ubl(id, document, principal.owner)
        return Response(xml, media_type="application/xml")

    @app.get("/v1/orders/{order_id}/timeline", response_model=OrderTimeline,
             summary="Read the full lookup-to-order audit story and call totals without upstream requests")
    def order_timeline(order_id: str, request: Request, principal: Principal = Depends(resolve_principal)):
        return svc(request).orders.timeline(order_id, principal.owner)

    @app.post("/v1/orders/{order_id}/cancel", response_model=Order,
              summary="Cancel a confirmed order before every known cutoff; fixed prices remain unchanged")
    def cancel_order(order_id: str, body: CancelRequest, request: Request, principal: Principal = Depends(resolve_principal)):
        return svc(request).orders.cancel(order_id,
            actor="human:operator" if principal.operator else f"agent:{principal.agent_id}", reason=body.reason,
            stated_name=body.cancelled_by if principal.operator else None)

    @app.get("/v1/approvals", response_model=list[ApprovalItem], summary="Pending approval inbox with quote evidence and all rule hits")
    def approvals(request: Request, principal: Principal = Depends(require_operator)):
        service = svc(request)
        return [ApprovalItem(order=o, quote=service.orders.get_quote(o.quote_id)) for o in service.orders.list_orders(True)]

    @app.post("/v1/approvals/{order_id}/approve", response_model=Order,
              description="Approve only the reviewed evidence. Optionally send evidence_revision; a stale revision "
                          "returns 409 EVIDENCE_CHANGED without upstream calls. Refresh stock and policy; changed decision "
                          "evidence stays pending at a new revision and returns 409 EVIDENCE_CHANGED with "
                          "data {evidence_revision, changes}. Review the refreshed order before approving again. "
                          "Unchanged evidence confirms. A revoked buyer VC returns 403 MANDATE_INVALID (revoked); a removed policy mandate returns 403 AGENT_NOT_REGISTERED "
                          "with rule hits in data; the order remains pending and can be rejected.")
    def approve(order_id: str, body: ApprovalRequest, request: Request, principal: Principal = Depends(require_operator)):
        return svc(request).orders.approve(order_id, body.approver, body.comment, body.evidence_revision, actor="human:operator")

    @app.post("/v1/approvals/{order_id}/reject", response_model=Order)
    def reject(order_id: str, body: ApprovalRequest, request: Request, principal: Principal = Depends(require_operator)):
        return svc(request).orders.reject(order_id, body.approver, body.comment, actor="human:operator")

    @app.get("/v1/audit", response_model=list[AuditEntry])
    def audit(request: Request, order_id: str | None = None, quote_id: str | None = None, principal: Principal = Depends(require_operator)):
        return svc(request).audit(order_id, quote_id)

    @app.get("/v1/audit/head", response_model=AuditHead,
             summary="Signed chain head for an independently held checkpoint; any authenticated principal")
    def audit_head(request: Request, principal: Principal = Depends(resolve_principal)):
        return audit_log.head(svc(request).db, svc(request).signing)

    @app.get("/v1/audit/anchors", response_model=list[AuditAnchor], summary="Public chain checkpoints; hashes and counts only")
    def audit_anchors(request: Request, principal: Principal = Depends(resolve_principal)):
        return svc(request).anchoring.list()

    @app.get("/v1/audit/verify", response_model=AuditVerification,
             summary="Verify the gateway chain and optional external checkpoint; hashes/counts only")
    def audit_verify(request: Request, checkpoint_entries: int | None = Query(default=None, ge=0),
                     checkpoint_hash: str | None = Query(default=None, pattern="^[0-9a-f]{64}$"),
                     anchor: str | None = Query(default=None, pattern="^(latest|0x[0-9a-fA-F]{64})$"),
                     principal: Principal = Depends(resolve_principal)):
        if (checkpoint_entries is None) != (checkpoint_hash is None):
            raise DomainError("INVALID_CHECKPOINT", "Supply checkpoint_entries and checkpoint_hash together", 422)
        result = audit_log.verify(svc(request).db, checkpoint_entries, checkpoint_hash)
        if anchor is not None:
            result.update(svc(request).anchoring.verify(anchor))
        return result

    return app


def __getattr__(name):
    # Uvicorn resolves app lazily; importing the factory for injected tests needs no process credentials.
    if name == "app":
        return create_app()
    raise AttributeError(name)
