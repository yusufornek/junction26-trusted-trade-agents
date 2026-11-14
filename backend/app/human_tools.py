"""Human MCP tools and A2A skills over the same commands as REST.

The tool registry also generates all human tool packs; decisions always pass
through the REST confirmation boundary, including evidence and single-use state.
"""
import json
from types import SimpleNamespace
from typing import Any

from mcp.server.mcpserver import Context
from mcp.types import InputRequiredResult, ElicitRequest, ElicitRequestFormParams
from pydantic import BaseModel, ConfigDict, Field

from .auth import require_reviewer, check_scope
from .db import DomainError
from .foundation import PRINCIPAL
from .tenant_api import confirm_rest, DecisionPreview, AgentCreate, AgentChange
from .models import ConnectionRequest


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrderTarget(Empty):
    order_id: str


class Decision(OrderTarget):
    comment: str = ""
    evidence_revision: int | None = Field(default=None, ge=1)
    confirmation_token: str | None = None


class PolicyChange(Empty):
    approval: dict = {}
    reject: dict = {}
    policy: dict | None = None
    comment: str | None = None
    confirmation_token: str | None = None


class AgentUpdate(AgentChange):
    agent_id: str


class ConnectionTarget(Empty):
    connection_id: str


class ConnectionRotation(ConnectionTarget):
    overlap_hours: float = 0


class GovernanceCommand(Empty):
    operation: str
    arguments: dict = Field(default_factory=dict)
    confirmation_token: str | None = None


# name -> (input model, resource, permission, description, operator only)
TOOLS = {
    "agent_governance": (GovernanceCommand, "governance", "read", "Tenant agent governance: register, effective policy, versions, simulate, change requests, pause, ownership, alerts and signed reports. Mutations require single-use human confirmation.", False),
    "list_orders": (Empty, "orders", "read", "Read orders visible to the consenting human; tenant scoped or all tenants for Valio staff.", False),
    "get_order_evidence": (OrderTarget, "evidence", "read", "Read an order's signed evidence, quote and receipt.", False),
    "list_approvals": (Empty, "approvals", "read", "Read the pending approval inbox with order and quote evidence.", False),
    "approve_order": (Decision, "approvals", "write", "Approve for the consenting human. First returns input_required with evidence; retry with requestState and accepted inputResponses. State is single use, valid five minutes; changed evidence returns EVIDENCE_CHANGED.", False),
    "reject_order": (Decision, "approvals", "write", "Reject for the consenting human after multi round-trip confirmation; records the human user id.", False),
    "cancel_order": (Decision, "orders", "write", "Cancel for the consenting human after confirmation, subject to the same REST cut-off and tenant checks.", False),
    "get_spend": (Empty, "spend", "read", "Read spending against the tenant's budgets.", False),
    "list_agents": (Empty, "agents", "read", "List owned agents; tenant administrators see every agent of their tenant.", False),
    "list_connections": (Empty, "connections", "read", "List visible tenant connections without secrets; members manage their own agents.", False),
    "create_agent": (AgentCreate, "agents", "write", "Create an agent owned by the consenting member or administrator; new agents start with approval.", False),
    "update_agent": (AgentUpdate, "agents", "write", "Rename, disable or re-enable an owned agent; administrators manage every agent of their tenant.", False),
    "create_connection": (ConnectionRequest, "connections", "write", "Create a connection for an owned agent, or any tenant agent as administrator; key returned once, store privately.", False),
    "rotate_connection": (ConnectionRotation, "connections", "write", "Rotate a visible connection key with at most 24 hours overlap.", False),
    "revoke_connection": (ConnectionTarget, "connections", "write", "Revoke a visible tenant connection immediately.", False),
    "get_suggestions": (Empty, "suggestions", "read", "Read tenant purchase-memory replenishment suggestions.", False),
    "get_policy": (Empty, "policy", "read", "Read tenant guardrails or platform policy for Valio staff.", False),
    "set_policy": (PolicyChange, "policy", "write", "Change policy for the consenting administrator, after confirmation. Tenant changes can only tighten platform ceilings.", False),
    "get_exceptions": (Empty, "orders", "read", "Read orders awaiting intervention across all tenants.", True),
    "get_health": (Empty, "health", "read", "Read upstream and supplier health, including rate-limit blocking.", True),
    "get_stats": (Empty, "stats", "read", "Read platform procurement statistics across every tenant.", True),
    "verify_audit": (Empty, "audit", "read", "Verify the global audit hash chain.", True),
    "list_anchors": (Empty, "audit", "read", "Read global audit anchors.", True),
    "list_tenants": (Empty, "tenants", "read", "Read tenant overview with assigned customer numbers.", True),
}


def available(audience):
    tenant_only = {"agent_governance", "list_agents", "create_agent", "update_agent", "get_spend", "get_suggestions"}
    return {name: spec for name, spec in TOOLS.items() if
            (audience == "operator" and name not in tenant_only) or (audience == "tenant" and not spec[4])}


def execute(service, p, audience, name, arguments):
    from .oauth import require_audience
    # The transport verifies the exact OAuth resource. Always enforce the human
    # and role boundary here too, including direct/stdio calls.
    if p.kind == "agent" or not p.user_id:
        raise DomainError("HUMAN_REQUIRED", "A consenting human is required", 403)
    if audience == "operator" and not p.operator or audience == "tenant" and p.operator:
        raise DomainError("OPERATOR_REQUIRED", "Human belongs to another audience", 403)
    if name not in available(audience):
        raise DomainError("TOOL_NOT_FOUND", "Tool is not available for this audience", 404)
    model, resource, permission, _, _ = TOOLS[name]
    body = model.model_validate(arguments)
    action = "decide" if resource == "approvals" and permission == "write" else "cancel" if name == "cancel_order" else name
    check_scope(p, resource, permission, action)
    service.limiter.consume(p.id, "read" if permission == "read" else "mutation")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(gateway=service)), url=SimpleNamespace(path="/v1/"+audience+"/"+resource))
    if name == "agent_governance":
        from .governance_api import invoke
        return invoke(service, p, body.operation, body.arguments, body.confirmation_token)
    if name == "list_orders":
        return service.orders.list_orders()
    if name == "get_order_evidence":
        from .evidence import build_bundle
        # Use the REST endpoint to preserve ownership and evidence bundle checks.
        return build_bundle(service, body.order_id, p)[0]
    if name == "list_approvals":
        require_reviewer(p)
        from .models import ApprovalItem
        from .autonomy import buyer_scope
        return [ApprovalItem(order=o, quote=service.orders._get_quote(o.quote_id)) for o in service.orders.list_orders(True)
                if p.operator or buyer_scope(service.policy.get(), p, o.agent_id, [o.customer_id])]
    if name in {"approve_order", "reject_order", "cancel_order"}:
        require_reviewer(p)
        operation = name.split("_")[0]
        actor = "human:" + ("operator" if p.user_id == "legacy-operator" else p.user_id or p.id)
        payload = {"comment": body.comment, "evidence_revision": body.evidence_revision}
        def apply():
            if operation == "approve":
                return service.orders.approve(body.order_id, p.id, body.comment, body.evidence_revision, actor=actor)
            if operation == "reject":
                return service.orders.reject(body.order_id, p.id, body.comment, actor=actor)
            return service.orders.cancel(body.order_id, actor=actor, reason=body.comment)
        return confirm_rest(service, p, operation, body.order_id, payload, body.confirmation_token, apply, force=True)
    if name == "get_spend":
        return service.buyer.spend(p)
    if name == "list_connections":
        return service.connections.list(p)
    if name == "create_connection":
        return service.connections.create(body, p, service.settings.public_url or "http://localhost:8000", True)
    if name == "rotate_connection":
        return service.connections.rotate(body.connection_id, p, body.overlap_hours)
    if name == "revoke_connection":
        return service.connections.revoke(body.connection_id, p)
    if name == "create_agent":
        from .tenant_api import create_agent
        return create_agent(body, request, p)
    if name == "update_agent":
        from .tenant_api import change_agent
        return change_agent(body.agent_id, AgentChange(**body.model_dump(exclude={"agent_id"}, exclude_unset=True)), request, p)
    if name == "list_agents":
        from .tenant_api import agents
        return agents(request, p)
    if name == "get_suggestions":
        return service.memory.suggestions(p)
    if name == "get_policy":
        if p.operator:
            return service.policy.get()
        from .tenant_api import get_policy
        return get_policy(request, p)
    if name == "set_policy":
        if not p.operator:
            if body.policy is not None:
                raise DomainError("VALIDATION_ERROR", "Tenant policy accepts approval and reject overlays", 422)
            from .tenant_api import Overlay, set_policy
            return set_policy(Overlay(**body.model_dump(exclude={"policy", "comment"})), request, p)
        from .models import PolicyUpdateRequest
        if body.policy is None:
            raise DomainError("VALIDATION_ERROR", "Platform policy requires the policy object from get_policy", 422)
        value = PolicyUpdateRequest(policy=body.policy, updated_by=p.id, comment=body.comment, confirmation_token=body.confirmation_token)
        return confirm_rest(service, p, "policy", "platform", value.model_dump(exclude={"confirmation_token"}), value.confirmation_token,
            lambda: service.policy.update(value, actor="human:" + p.id), force=True)
    if name == "get_exceptions":
        return [o for o in service.orders.list_orders() if o.status in {"PENDING_APPROVAL", "PENDING_EXECUTION", "REJECTED"}]
    if name == "get_health":
        return {"upstream": service.valio.health(), "anchoring": service.anchoring.health(), "suppliers": service.suppliers.list()}
    if name == "get_stats":
        from .stats import read_stats
        return read_stats(service.db, registered_agents=service.policy.get().agents, mandates=service.mandates)
    if name == "verify_audit":
        from . import audit
        return audit.verify(service.db)
    if name == "list_anchors":
        return service.anchoring.list()
    if name == "list_tenants":
        from .accounts import tenants
        return tenants(request, p)


def register_tools(server, audience):
    for name, (model, _, _, description, _) in available(audience).items():
        if name == "agent_governance":
            # The tenant adapter registers its typed MCP confirmation handler.
            continue
        # Register concrete annotated callables so SDK schemas are the source of
        # truth. Inputs are flattened just like the buyer tools.
        namespace = {"invoke": lambda args, n=name: execute(server.gateway, PRINCIPAL.get(), audience, n, args),
                     "Any": Any}
        fields = model.model_fields
        signature = ", ".join(f"{key}: Any" + (" = None" if not field.is_required() else "") for key, field in fields.items())
        args = ", ".join(f"'{key}': {key}" for key in fields)
        exec(f"def tool({'*, ' if signature else ''}{signature}) -> Any:\n    return invoke({{{args}}})", namespace)
        server.tool(name=name, description=description, structured_output=False)(namespace["tool"])
        # Replace the schema with the validated model rather than Any annotations.
        server._tool_manager._tools[name].parameters = model.model_json_schema()


async def confirm_mcp(server, name, arguments, context):
    """SDK requestState/inputResponses carry the REST single-use confirmation."""
    args = dict(arguments)
    state = context.request_state if context else None
    responses = context.input_responses if context else None
    if state:
        answer = (responses or {}).get("decision")
        if not answer or answer.action != "accept" or (answer.content or {}).get("confirm") is not True:
            if answer and answer.action in {"decline", "cancel"}:
                from .accounts import digest
                from . import audit
                p = PRINCIPAL.get()
                with server.gateway.db.connect(write=True) as conn:
                    conn.execute("UPDATE decision_confirmations SET used_at=? WHERE digest=? AND user_id=? AND used_at IS NULL",
                                 (audit.utcnow().isoformat(), digest(state), p.user_id))
            raise DomainError("CONFIRMATION_DECLINED", "Human decision was cancelled", 409)
        args["confirmation_token"] = state
    elif args.get("confirmation_token"):
        raise DomainError("CONFIRMATION_INVALID", "MCP decisions require requestState and the user's answer", 409)
    result = execute(server.gateway, PRINCIPAL.get(), server.audience, name, args)
    if isinstance(result, DecisionPreview):
        return InputRequiredResult(request_state=result.confirmation_token, input_requests={"decision": ElicitRequest(params=ElicitRequestFormParams(
            message="Confirm this decision after reviewing the order and evidence: " + json.dumps(result.summary, default=str),
            requestedSchema={"type": "object", "properties": {"confirm": {"type": "boolean", "title": "Confirm decision"}}, "required": ["confirm"]}))})
    return result
