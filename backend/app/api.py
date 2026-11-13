from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from .db import DomainError
from .models import (ApprovalItem, ApprovalRequest, AuditEntry, ErrorResponse, Health,
                     LookupResult, NeedSpec, Order, OrderRequest, Product, Quote, QuoteRequest)
from .service import Gateway


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
                  responses={code: {"model": ErrorResponse} for code in (404, 409, 422, 502, 503)})
    app.add_middleware(CORSMiddleware, allow_origin_regex=r"http://localhost(?::\d+)?", allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(DomainError)
    async def domain_error(request, error):
        return JSONResponse(status_code=error.status, content={"code": error.code, "message": error.message})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # Omit input values from errors, retaining the field location and explanation.
        message = "; ".join(f"{'.'.join(str(v) for v in e['loc'])}: {e['msg']}" for e in error.errors())
        return JSONResponse(status_code=422, content={"code": "VALIDATION_ERROR", "message": message})

    def svc(request):
        return request.app.state.gateway

    @app.get("/health", response_model=Health)
    def health(request: Request):
        count = svc(request).catalog.count()
        return Health(status="ok" if count else "catalog_empty", catalog_rows=count)

    @app.post("/v1/offers", response_model=LookupResult, summary="Shortlist catalog matches, then fetch live offers")
    def offers(need: NeedSpec, request: Request):
        return svc(request).find_offers(need)

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
