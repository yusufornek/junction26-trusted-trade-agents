import logging
import sqlite3
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, Request, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from . import audit as audit_log
from .auth import Credentials, Principal, bearer, require_operator, require_reviewer, resolve_principal
from .config import Settings
from .db import DomainError
from .models import (ApprovalItem, ApprovalRequest, AuditEntry, CancelRequest, ErrorResponse, Health,
                     LookupResult, NeedSpec, Order, OrderRequest, OrderTimeline, Product, Quote, QuoteRequest)
from .models import PolicyResource, PolicyResetRequest, PolicyUpdateRequest, Stats
from .models import JWKSet, AuditHead, AuditVerification, AuditAnchor, RevocationRequest
from .models import AgentTrackRecord, AutonomyChange, AutonomyChangeResult
from .service import Gateway
from .stats import read_stats
from .foundation import CORRELATION, PRINCIPAL, correlation_id, manifest
from .recovery import problem, validation_problem
from .agent_context import AgentContext, agent_context
from .discovery import MCPTransport, install_routes as install_discovery
from starlette.routing import Route

logger = logging.getLogger(__name__)


def create_app(gateway=None, *, settings=None, keys=None, serve_mcp=True):
    settings = settings or (gateway.settings if gateway is not None else Settings.from_env())
    keys = keys or settings.keys or Credentials()
    keys.require_operator()

    @asynccontextmanager
    async def lifespan(app):
        service = gateway or Gateway(settings)
        app.state.gateway = service
        stop = asyncio.Event()
        async def deliver_notifications():
            while not stop.is_set():
                await run_in_threadpool(service.outbox.dispatch)
                await run_in_threadpool(service.webhooks.dispatch)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=.5)
                except asyncio.TimeoutError:
                    pass
            # A request can commit just after the worker's last poll. Drain local
            # consumers once before shutdown; signed HTTP deliveries remain durable.
            await run_in_threadpool(service.outbox.dispatch)
        if not serve_mcp:
            worker = asyncio.create_task(deliver_notifications())
            try:
                yield
            finally:
                stop.set()
                await worker
                if gateway is None:
                    service.close()
            return
        from .mcp_server import create_server
        app.state.mcp_server = create_server(service, agent_id=None)
        app.state.mcp_server.keys = keys
        from mcp.server.transport_security import TransportSecuritySettings
        from urllib.parse import urlsplit
        public = service.settings.public_url
        security = TransportSecuritySettings(allowed_hosts=["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "[::1]", "[::1]:*"] +
            ([urlsplit(public).netloc] if public else []),
            allowed_origins=[f"{scheme}://{host}{port}" for scheme in ("http", "https")
                for host in ("localhost", "127.0.0.1", "[::1]") for port in ("", ":*")] + ([public] if public else []))
        mcp_http = app.state.mcp_server.streamable_http_app(stateless_http=True, json_response=True, transport_security=security)
        app.state.mcp_http = mcp_http
        worker = asyncio.create_task(deliver_notifications())
        try:
            async with mcp_http.router.lifespan_context(mcp_http):
                yield
        finally:
            stop.set()
            await worker
            if gateway is None:
                service.close()

    app = FastAPI(title="Valio Aimo Trusted Trade Agents", version="0.1.0", lifespan=lifespan,
                  description="Fixed-price gateway. REST, MCP and A2A 1.0 share one core. Signed webhooks deliver decisions independently of purchasing state. Bearer credentials establish agent ownership and operator authority. Keyed writes bind the principal, command route including all targets, and payload. Every exact replay rechecks current authorization and scope before returning its saved result. Policy changes commit with their audit and command result; runtime YAML is published only after commit, with durable recovery from SQLite. Commitments are gateway-simulated.",
                  responses={code: {"model": ErrorResponse, "content": {"application/problem+json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}}
                             for code in (401, 403, 404, 409, 422, 429, 500, 502, 503)})
    app.state.keys = keys
    app.state.mcp_served = serve_mcp
    from .connections import router as connections_router
    app.include_router(connections_router)
    from .oauth import install as install_oauth
    install_oauth(app)
    from .webhooks import router as webhook_router
    from .a2a import install_routes
    app.include_router(webhook_router)
    install_routes(app)
    from .buyer import router as buyer_router
    from .evidence import router as evidence_router
    app.include_router(buyer_router)
    from .baskets import router as baskets_router
    app.include_router(baskets_router)
    app.include_router(evidence_router)
    install_discovery(app)
    from .developer_docs import install_routes as install_developer_docs
    install_developer_docs(app)
    if serve_mcp:
        app.router.routes.append(Route("/mcp", endpoint=MCPTransport(), methods=["GET", "POST", "DELETE"]))
    app.add_middleware(CORSMiddleware, allow_origin_regex=r"http://localhost(?::\d+)?", allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def correlation(request, call_next):
        token = CORRELATION.set(request.headers.get("X-Correlation-Id") or correlation_id())
        request.state.correlation_id = CORRELATION.get()
        principal_token = PRINCIPAL.set(None)
        try:
            if request.url.path == "/a2a":
                try:
                    await resolve_principal(request, await bearer(request))
                except DomainError as error:
                    response = await domain_error(request, error)
                    response.headers["X-Correlation-Id"] = CORRELATION.get()
                    return response
            public = request.url.path.startswith("/.well-known/oauth-") or request.url.path in {"/mcp/server-card", "/.well-known/ai-catalog.json", "/.well-known/agent-card.json",
                "/.well-known/jwks.json", "/openapi.json", "/v1/meta", "/developers", "/developers/tools.json", "/llms.txt"}
            response = Response(status_code=204) if public and request.method == "OPTIONS" else await call_next(request)
            if public:
                response.headers.update({"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type, If-None-Match", "Access-Control-Expose-Headers": "ETag"})
            response.headers["X-Correlation-Id"] = CORRELATION.get()
            return response
        finally:
            PRINCIPAL.reset(principal_token)
            CORRELATION.reset(token)

    @app.exception_handler(DomainError)
    async def domain_error(request, error):
        content = problem(error, request.url.path)
        headers = {"WWW-Authenticate": "Bearer"} if error.status == 401 else {}
        if content["retry_after_seconds"] is not None:
            headers["Retry-After"] = str(content["retry_after_seconds"])
        return JSONResponse(status_code=error.status, content=content, media_type="application/problem+json", headers=headers)

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        if error.status_code == 400:
            refused = await parsing_auth_error(request)
            if refused is not None:
                return refused
        return await domain_error(request, DomainError("HTTP_ERROR", "Request could not be completed", error.status_code))

    @app.exception_handler(Exception)
    async def unexpected_error(request, error):
        token = CORRELATION.set(getattr(request.state, "correlation_id", None) or correlation_id())
        try:
            response = await domain_error(request, DomainError("INTERNAL_ERROR", "The request could not be completed", 500))
            response.headers["X-Correlation-Id"] = CORRELATION.get()
            return response
        finally:
            CORRELATION.reset(token)

    async def parsing_auth_error(request):
        # JSON decoding can fail before FastAPI resolves dependencies. Apply the matched route's
        # existing credential/operator guards first, without duplicating its authorization policy.
        route = request.scope.get("route")
        dependencies = [getattr(route, "dependant", None)]
        guards = set()
        while dependencies:
            dependency = dependencies.pop()
            if dependency is not None:
                if dependency.call in (resolve_principal, require_operator, require_reviewer):
                    guards.add(dependency.call)
                dependencies.extend(dependency.dependencies)
        if guards:
            try:
                principal = getattr(request.state, "principal", None)
                if principal is None:
                    principal = await resolve_principal(request, await bearer(request))
                if require_operator in guards:
                    require_operator(principal)
                if require_reviewer in guards:
                    require_reviewer(principal)
            except DomainError as auth_error:
                return await domain_error(request, auth_error)
        return None

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        refused = await parsing_auth_error(request)
        if refused is not None:
            return refused
        route = request.scope.get("route")
        # Omit input values from errors, retaining the field location and explanation.
        message = "; ".join(f"{'.'.join(str(v) for v in e['loc'])}: {e['msg']}" for e in error.errors())
        if getattr(route, "path", None) == "/v1/orders/{order_id}/cancel":
            # Blocking SQLite write: keep it off the event loop, and never let an audit failure replace the 422.
            try:
                await run_in_threadpool(audit_cancel_validation, request, error.body, message)
            except sqlite3.Error:
                logger.exception("Could not audit a refused cancellation")
        return await domain_error(request, validation_problem(error))

    def svc(request):
        return request.app.state.gateway

    @app.get("/v1/meta", response_model=dict, summary="Public pinned standards and installed packages; limiter scope")
    def meta():
        return manifest()

    @app.get("/v1/me", response_model=AgentContext, summary="Caller scope, mandate, effective limits, budget, environment and operations")
    def me(request: Request, principal: Principal = Depends(resolve_principal),
           agent_mandate: Annotated[str | None, Header()] = None):
        service = svc(request)
        service.limiter.consume(principal.id, "read")
        return agent_context(service, principal, agent_mandate)

    @app.get("/v1/agents/{agent_id}/track-record", response_model=AgentTrackRecord,
             description="Audit-derived record for orders placed in window_days (default 30). Operator, the agent and its buyer organisation may read it. Readiness is advisory; no automatic promotion.")
    def track_record(agent_id: str, request: Request, window_days: int = Query(default=30, ge=1, le=3650),
                     principal: Principal = Depends(resolve_principal)):
        return svc(request).autonomy.track_record(agent_id, principal, window_days)

    @app.post("/v1/agents/{agent_id}/autonomy", response_model=AutonomyChangeResult,
              description="Operator or this agent's buyer_admin grants or removes autonomy. Requires a reason; audits AUTONOMY_CHANGED with a track-record snapshot. Optional Idempotency-Key binds this target and payload, rechecks current target scope, and replays the first result without changing authority again; changed target or payload returns 409 IDEMPOTENCY_CONFLICT. Runtime policy publication follows the enclosing command commit. Readiness never grants authority automatically. A stricter signed buyer mandate still wins.")
    def change_autonomy(agent_id: str, body: AutonomyChange, request: Request,
                        principal: Principal = Depends(resolve_principal),
                        idempotency_key: Annotated[str | None, Header(min_length=1)] = None):
        return svc(request).autonomy.change(agent_id, body, principal, command_key=idempotency_key)

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
                      upstream=svc(request).valio.health(),
                      coverage=svc(request).catalog.metadata("coverage"), anchoring=svc(request).anchoring.health())

    @app.get("/.well-known/jwks.json", response_model=JWKSet,
             summary="Public Ed25519 keys for quote, receipt, audit-head JWS and Standard Webhooks v1a")
    def jwks(request: Request):
        return svc(request).signing.jwks()

    def presented_mandate(agent_mandate: Annotated[str | None, Header(description="Buyer-signed EdDSA VC-JWT; sub must equal the authenticated agent ID.")] = None):
        return agent_mandate

    @app.post("/v1/mandates/revocations", response_model=dict[str, str | list[str]], summary="Record a buyer-signed mandate revocation")
    def revoke_mandate(body: RevocationRequest, request: Request, principal: Principal = Depends(resolve_principal),
                      idempotency_key: Annotated[str | None, Header(min_length=1)] = None):
        return svc(request).mandates.revoke(body.jwt, "human:operator" if principal.operator else f"agent:{principal.agent_id}", command_key=idempotency_key)

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
    def put_policy(body: PolicyUpdateRequest, request: Request, principal: Principal = Depends(require_operator),
                   idempotency_key: Annotated[str | None, Header(min_length=1)] = None):
        return svc(request).policy.update(body, actor="human:operator", command_key=idempotency_key)

    @app.post("/v1/policy/reset", response_model=PolicyResource, summary="Restore committed policy defaults with a human audit event")
    def reset_policy(body: PolicyResetRequest, request: Request, principal: Principal = Depends(require_operator),
                     idempotency_key: Annotated[str | None, Header(min_length=1)] = None):
        return svc(request).policy.update(body, reset=True, actor="human:operator", command_key=idempotency_key)

    @app.get("/v1/stats", response_model=Stats, summary="Operator metrics from persisted evidence; no upstream calls")
    def stats(request: Request, since: datetime | None = None, principal: Principal = Depends(require_operator)):
        service = svc(request)
        service.orders.expire()
        return read_stats(service.db, since, registered_agents=set(service.policy.get().agents), mandates=service.mandates)

    @app.get("/v1/products/{id}", response_model=Product)
    def product(id: str, request: Request, principal: Principal = Depends(resolve_principal)):
        return svc(request).catalog.get(id)

    @app.post("/v1/quotes", response_model=Quote, summary="Recheck selected lines or a plan_ref live and lock fixed prices",
              description="Lookup, quote and order require a customer mandate and enforce agent ownership.")
    def create_quote(body: QuoteRequest, request: Request, principal: Principal = Depends(resolve_principal), mandate=Depends(presented_mandate),
                     idempotency_key: Annotated[str | None, Header(min_length=1)] = None):
        return svc(request).orders.create_quote(body, principal.agent_id, operator=principal.operator, mandate=mandate, command_key=idempotency_key)

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
    def cancel_order(order_id: str, body: CancelRequest, request: Request, principal: Principal = Depends(resolve_principal),
                     idempotency_key: Annotated[str | None, Header(min_length=1)] = None, mandate=Depends(presented_mandate)):
        return svc(request).orders.cancel(order_id,
            actor="human:operator" if principal.operator else f"agent:{principal.agent_id}", reason=body.reason,
            stated_name=body.cancelled_by if principal.operator else None, mandate=mandate, command_key=idempotency_key)

    @app.get("/v1/approvals", response_model=list[ApprovalItem], summary="Pending approval inbox with quote evidence and all rule hits")
    def approvals(request: Request, principal: Principal = Depends(require_reviewer)):
        service = svc(request)
        from .autonomy import buyer_scope
        return [ApprovalItem(order=o, quote=service.orders.get_quote(o.quote_id)) for o in service.orders.list_orders(True)
                if principal.operator or buyer_scope(service.policy.get(), principal, o.agent_id, [o.customer_id])]

    @app.post("/v1/approvals/{order_id}/approve", response_model=Order,
              description="Approve only the reviewed evidence. Optionally send evidence_revision; a stale revision "
                          "returns 409 EVIDENCE_CHANGED without upstream calls. Refresh stock and policy; changed decision "
                          "evidence stays pending at a new revision and returns 409 EVIDENCE_CHANGED with "
                          "data {evidence_revision, changes}. Review the refreshed order before approving again. "
                          "Unchanged evidence confirms. A revoked buyer VC returns 403 MANDATE_INVALID (revoked); a removed policy mandate returns 403 AGENT_NOT_REGISTERED "
                          "with rule hits in data; the order remains pending and can be rejected. Cached approval responses recheck the reviewer's current organisation and customer scope.")
    def approve(order_id: str, body: ApprovalRequest, request: Request, principal: Principal = Depends(require_reviewer),
                idempotency_key: Annotated[str | None, Header(min_length=1)] = None):
        return svc(request).orders.approve(order_id, body.approver, body.comment, body.evidence_revision,
            actor="human:operator" if principal.operator else f"human:{principal.id}", command_key=idempotency_key)

    @app.post("/v1/approvals/{order_id}/reject", response_model=Order,
              description="Reject a pending order. Optional Idempotency-Key replays the first result; cached responses recheck the reviewer's current organisation and customer scope.")
    def reject(order_id: str, body: ApprovalRequest, request: Request, principal: Principal = Depends(require_reviewer),
               idempotency_key: Annotated[str | None, Header(min_length=1)] = None):
        return svc(request).orders.reject(order_id, body.approver, body.comment,
            actor="human:operator" if principal.operator else f"human:{principal.id}", command_key=idempotency_key)

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

    original_openapi = app.openapi
    def problem_openapi():
        schema = original_openapi()
        for path in schema["paths"].values():
            for operation in path.values():
                if not isinstance(operation, dict):
                    continue
                for status, response in operation.get("responses", {}).items():
                    response.setdefault("headers", {})["X-Correlation-Id"] = {"schema": {"type": "string"}, "description": "Accepted or generated request correlation identifier."}
                    if status.isdigit() and int(status) >= 400:
                        response.get("content", {}).pop("application/json", None)
                    if status in {"429", "502", "503"}:
                        response.setdefault("headers", {})["Retry-After"] = {"schema": {"type": "integer", "minimum": 1}, "description": "Seconds before retrying the same command, when retryable."}
        return schema
    app.openapi = problem_openapi
    return app


def __getattr__(name):
    # Uvicorn resolves app lazily; importing the factory for injected tests needs no process credentials.
    if name == "app":
        return create_app()
    raise AttributeError(name)
