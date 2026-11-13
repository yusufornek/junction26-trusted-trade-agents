import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated

from fastapi import FastAPI, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from . import audit as audit_log
from .db import DomainError
from .models import (ApprovalItem, ApprovalRequest, AuditEntry, CancelRequest, ErrorResponse, Health,
                     LookupResult, NeedSpec, Order, OrderRequest, OrderTimeline, Product, Quote, QuoteRequest)
from .models import PolicyResource, PolicyResetRequest, PolicyUpdateRequest, Stats
from .service import Gateway
from .stats import read_stats

logger = logging.getLogger(__name__)


def create_app(gateway=None):
    @asynccontextmanager
    async def lifespan(app):
        service = gateway or Gateway()
        app.state.gateway = service
        try:
            yield
        finally:
            if gateway is None:
                service.close()

    app = FastAPI(title="Valio Aimo Trusted Trade Agents", version="0.1.0", lifespan=lifespan,
                  description="MVP fixed-price gateway. REST and MCP share one core. Local quotes/orders and one human approver role; development only, no authentication.",
                  responses={code: {"model": ErrorResponse} for code in (403, 404, 409, 422, 502, 503)})
    app.add_middleware(CORSMiddleware, allow_origin_regex=r"http://localhost(?::\d+)?", allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(DomainError)
    async def domain_error(request, error):
        return JSONResponse(status_code=error.status, content={"code": error.code, "message": error.message})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # Omit input values from errors, retaining the field location and explanation.
        message = "; ".join(f"{'.'.join(str(v) for v in e['loc'])}: {e['msg']}" for e in error.errors())
        if getattr(request.scope.get("route"), "path", None) == "/v1/orders/{order_id}/cancel":
            # Blocking SQLite write: keep it off the event loop, and never let an audit failure replace the 422.
            try:
                await run_in_threadpool(audit_cancel_validation, request, error.body, message)
            except sqlite3.Error:
                logger.exception("Could not audit a refused cancellation")
        return JSONResponse(status_code=422, content={"code": "VALIDATION_ERROR", "message": message})

    def svc(request):
        return request.app.state.gateway

    def audit_cancel_validation(request, raw_body, message):
        """Audit a cancel request refused by validation, with a normalised actor (never raw input)."""
        body = raw_body if isinstance(raw_body, dict) else {}
        name = body.get("cancelled_by")
        name = name.strip()[:200] if isinstance(name, str) else ""
        reason = body.get("reason") if isinstance(body.get("reason"), str) else ""
        order_id = request.path_params["order_id"]
        with svc(request).db.connect(write=True) as conn:
            row = conn.execute("SELECT quote_id FROM orders WHERE id=?", (order_id,)).fetchone()
            quote_id = row["quote_id"] if row else None
            inputs = {"order_id": order_id, "reason": reason, **({"quote_id": quote_id} if quote_id else {})}
            audit_log.append(conn, f"human:{name or 'unknown'}", "CANCEL_FAILED", message,
                             order_id=order_id, quote_id=quote_id,
                             data={"inputs": inputs, "error": "VALIDATION_ERROR",
                                   "live_calls": {"price": 0, "availability": 0}})

    @app.get("/health", response_model=Health)
    def health(request: Request):
        count = svc(request).catalog.count()
        return Health(status="ok" if count else "catalog_empty", catalog_rows=count)

    @app.post("/v1/offers", response_model=LookupResult, summary="Shortlist catalog matches, then fetch live offers")
    def offers(need: NeedSpec, request: Request, x_agent_id: Annotated[str, Header()] = "external"):
        return svc(request).find_offers(need, actor=f"agent:{x_agent_id}")

    @app.get("/v1/lookups/{lookup_id}", response_model=LookupResult, summary="Retrieve stored alternatives behind a quote's need_ref")
    def get_lookup(lookup_id: str, request: Request):
        return svc(request).lookup.get(lookup_id)

    @app.get("/v1/policy", response_model=PolicyResource, summary="Read effective procurement policy and known warning codes")
    def get_policy(request: Request):
        return svc(request).policy.get()

    @app.put("/v1/policy", response_model=PolicyResource, summary="Persist policy for subsequent operations and audit changed fields")
    def put_policy(body: PolicyUpdateRequest, request: Request):
        return svc(request).policy.update(body)

    @app.post("/v1/policy/reset", response_model=PolicyResource, summary="Restore committed policy defaults with a human audit event")
    def reset_policy(body: PolicyResetRequest, request: Request):
        return svc(request).policy.update(body, reset=True)

    @app.get("/v1/stats", response_model=Stats, summary="Operator metrics from persisted evidence; no upstream calls")
    def stats(request: Request, since: datetime | None = None):
        service = svc(request)
        service.orders.expire()
        return read_stats(service.db, since, registered_agents=set(service.policy.get().agents))

    @app.get("/v1/products/{id}", response_model=Product)
    def product(id: str, request: Request):
        return svc(request).catalog.get(id)

    @app.post("/v1/quotes", response_model=Quote, summary="Recheck live price/stock and lock fixed prices")
    def create_quote(body: QuoteRequest, request: Request, x_agent_id: Annotated[str, Header()] = "external"):
        return svc(request).orders.create_quote(body, x_agent_id)

    @app.get("/v1/quotes/{id}", response_model=Quote)
    def get_quote(id: str, request: Request):
        return svc(request).orders.get_quote(id)

    @app.post("/v1/orders", response_model=Order, summary="Accept an unexpired quote; policy may require approval")
    def place_order(body: OrderRequest, request: Request,
                    idempotency_key: Annotated[str, Header(min_length=1, description="Retry key: same quote and agent returns the same order.")],
                    x_agent_id: Annotated[str, Header()] = "external"):
        return svc(request).orders.place_order(body.quote_id, idempotency_key, x_agent_id)

    @app.get("/v1/orders", response_model=list[Order])
    def orders(request: Request):
        return svc(request).orders.list_orders()

    @app.get("/v1/orders/{id}", response_model=Order)
    def get_order(id: str, request: Request):
        return svc(request).orders.get_order(id)

    @app.get("/v1/orders/{order_id}/timeline", response_model=OrderTimeline,
             summary="Read the full lookup-to-order audit story and call totals without upstream requests")
    def order_timeline(order_id: str, request: Request):
        return svc(request).orders.timeline(order_id)

    @app.post("/v1/orders/{order_id}/cancel", response_model=Order,
              summary="Cancel a confirmed order before every known cutoff; fixed prices remain unchanged")
    def cancel_order(order_id: str, body: CancelRequest, request: Request):
        return svc(request).orders.cancel(order_id, actor=f"human:{body.cancelled_by}", reason=body.reason)

    @app.get("/v1/approvals", response_model=list[ApprovalItem], summary="Pending approval inbox with quote evidence and all rule hits")
    def approvals(request: Request):
        service = svc(request)
        return [ApprovalItem(order=o, quote=service.orders.get_quote(o.quote_id)) for o in service.orders.list_orders(True)]

    @app.post("/v1/approvals/{order_id}/approve", response_model=Order)
    def approve(order_id: str, body: ApprovalRequest, request: Request):
        return svc(request).orders.approve(order_id, body.approver, body.comment)

    @app.post("/v1/approvals/{order_id}/reject", response_model=Order)
    def reject(order_id: str, body: ApprovalRequest, request: Request):
        return svc(request).orders.reject(order_id, body.approver, body.comment)

    @app.get("/v1/audit", response_model=list[AuditEntry])
    def audit(request: Request, order_id: str | None = None, quote_id: str | None = None):
        return svc(request).audit(order_id, quote_id)

    return app


app = create_app()
