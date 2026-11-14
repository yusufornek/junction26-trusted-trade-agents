"""REST and MCP doors for the same purchase-intent commands."""
from fastapi import APIRouter, Depends, Header, Request

from .auth import Principal, resolve_principal, require_operator, require_reviewer, check_scope
from .db import DomainError
from .models import QuoteRequest
from .purchase_intent_integrity import IntentRequest, Amendment, Revision, Transition, SupplierEvent, Faults, IntentReview

router = APIRouter(prefix="/v1/intents", tags=["Purchase intents"])


def authority(principal, write=True):
    check_scope(principal, "orders", "write" if write else "read", "purchase_intent")
    if principal.kind in {"supplier_agent", "buyer_user", "tenant_user"}:
        raise DomainError("BUYER_AGENT_REQUIRED", "Use a buyer agent's mandate for purchase intent commands", 403)


@router.post("", response_model=dict)
def create(body: IntentRequest, request: Request, principal: Principal = Depends(resolve_principal),
           mandate: str | None = Header(default=None, alias="Agent-Mandate")):
    authority(principal)
    return request.app.state.gateway.intents.create(body, principal.id, mandate)


@router.get("/{id}", response_model=dict)
def get(id: str, request: Request, principal: Principal = Depends(resolve_principal),
        mandate: str | None = Header(default=None, alias="Agent-Mandate")):
    authority(principal, False)
    return request.app.state.gateway.intents.get(id, principal.id, mandate)


@router.post("/{id}/amendments", response_model=dict)
def amend(id: str, body: Amendment, request: Request, principal: Principal = Depends(resolve_principal),
          mandate: str | None = Header(default=None, alias="Agent-Mandate")):
    authority(principal)
    return request.app.state.gateway.intents.amend(id, body, principal.id, mandate)


@router.post("/{id}/resume", response_model=dict)
def resume(id: str, body: Revision, request: Request, principal: Principal = Depends(resolve_principal),
           mandate: str | None = Header(default=None, alias="Agent-Mandate")):
    authority(principal)
    return request.app.state.gateway.intents.resume(id, body.expected_version, principal.id, mandate)


@router.post("/{id}/quote", response_model=dict)
def quote(id: str, body: QuoteRequest, request: Request, principal: Principal = Depends(resolve_principal),
          mandate: str | None = Header(default=None, alias="Agent-Mandate")):
    authority(principal)
    if body.intent_id != id:
        raise DomainError("NEED_MISMATCH", "Quote intent_id must match the URL", 422)
    return request.app.state.gateway.orders.create_quote(body, principal.id, mandate=mandate).model_dump(mode="json")


@router.post("/{id}/submit", response_model=dict)
def submit(id: str, body: Revision, request: Request, principal: Principal = Depends(resolve_principal),
           mandate: str | None = Header(default=None, alias="Agent-Mandate")):
    authority(principal)
    service = request.app.state.gateway
    with service.db.connect() as conn:
        obj = service.intents.load(conn, id)
        service.intents.authority(conn, obj, principal.id, mandate)
        if not obj.get("order_id"):
            service.intents.check_version(obj, body.expected_version)
            if not obj.get("quote_id"):
                raise DomainError("NEED_REF_REQUIRED", "Quote the intent first", 422)
    if not obj.get("order_id"):
        service.orders.place_order(obj["quote_id"], "intent:" + id, principal.id, mandate=mandate)
    return service.intents.recover(id, principal.id, mandate)


@router.post("/{id}/transitions", response_model=dict)
def transition(id: str, body: Transition, request: Request, principal: Principal = Depends(resolve_principal),
               mandate: str | None = Header(default=None, alias="Agent-Mandate")):
    authority(principal)
    return request.app.state.gateway.intents.transition(id, body, principal.id, mandate)


@router.post("/{id}/supplier-events", response_model=dict)
def event(id: str, body: SupplierEvent, request: Request, principal: Principal = Depends(require_operator)):
    if body.submission_reference is None:
        raise DomainError("VALIDATION_ERROR", "Supplier events must identify their submission_reference", 422)
    return request.app.state.gateway.intents.event(id, body)


@router.post("/{id}/amendments/approve", response_model=dict)
def approve_change(id: str, body: IntentReview, request: Request, principal: Principal = Depends(require_reviewer)):
    return review_intent(id, body, request, principal, partial=False)


@router.post("/{id}/partial-confirmation/approve", response_model=dict)
def approve_partial(id: str, body: IntentReview, request: Request, principal: Principal = Depends(require_reviewer)):
    return review_intent(id, body, request, principal, partial=True)


def review_intent(id, body, request, principal, *, partial):
    from .tenant_api import confirm_rest
    service = request.app.state.gateway
    with service.db.connect() as conn:
        obj = service.intents.load(conn, id)
        if not obj.get("order_id"):
            raise DomainError("INVALID_TRANSITION", "There is no order to review", 409)
        from .autonomy import authorize_review
        authorize_review(service.policy.get(), principal, service.orders._order(conn, obj["order_id"]))
        evidence = {"intent_id": id, "intent_review": "partial" if partial else "amendment",
                    "request": obj["request"], "supplier_confirmed_lines": obj.get("supplier_confirmed_lines"),
                    "replacement_quote": service.orders._quote(conn, body.quote_id).model_dump(mode="json") if body.quote_id else None}
    # Reuse the order review boundary so its evidence revision is part of the
    # human confirmation, alongside the exact intent change and new quote.
    result = confirm_rest(service, principal, "approve", obj["order_id"],
        {**body.model_dump(exclude={"confirmation_token"}), **evidence}, body.confirmation_token,
        lambda: service.intents.review(id, body, principal, partial=partial))
    return result.model_dump(mode="json") if hasattr(result, "model_dump") else result


@router.put("/{id}/sandbox-faults", response_model=dict)
def faults(id: str, body: Faults, request: Request, principal: Principal = Depends(require_operator)):
    return request.app.state.gateway.intents.set_faults(id, body)


def register_tools(server, gateway, current_agent, presented_mandate):
    from .foundation import PRINCIPAL
    @server.tool(description="Create a tenant buying intent. Repeating client_intent_id returns its current quote/order across REST, MCP and A2A; changed payload requires an amendment. Overlapping demand requires supersedes or approved additional demand. Quote using intent_id and intent_version, then place once.")
    def create_intent(request: IntentRequest) -> dict:
        authority(PRINCIPAL.get())
        return gateway.intents.create(request, current_agent(), presented_mandate.get())

    @server.tool(description="Get an intent and its current quote/order, revision, remainder and structured next actions under your own mandate.")
    def get_intent(intent_id: str) -> dict:
        authority(PRINCIPAL.get(), False)
        return gateway.intents.get(intent_id, current_agent(), presented_mandate.get())

    @server.tool(description="Amend a buying intent at expected_version. Changes after placement require human approval and the normal order change/cutoff rules; the original commitment remains until approved.")
    def amend_intent(intent_id: str, request: IntentRequest, expected_version: int) -> dict:
        authority(PRINCIPAL.get())
        return gateway.intents.amend(intent_id, Amendment(request=request, expected_version=expected_version), current_agent(), presented_mandate.get())

    @server.tool(description="Continue a same-tenant intent under your own current mandate. Rechecks categories and spending authority, preserves pending human approvals and returns next actions without another order.")
    def resume_intent(intent_id: str, expected_version: int) -> dict:
        authority(PRINCIPAL.get())
        return gateway.intents.resume(intent_id, expected_version, current_agent(), presented_mandate.get())
