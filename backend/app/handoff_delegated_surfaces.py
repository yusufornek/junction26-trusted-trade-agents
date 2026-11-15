"""REST, native MCP SDK tools/resources and A2A SDK task adapters for spec 64."""
import json
from hashlib import sha256
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, Request

from .auth import Principal, resolve_principal
from .db import DomainError
from .foundation import PRINCIPAL, correlation_id
from .handoff_delegated_authority import Handoff, Delegation, Confirmation

router = APIRouter(tags=["Handoff and delegated authority"])


def scope_path(path):
    if path.startswith(("/v1/tenant/handoffs", "/v1/tenant/delegations", "/v1/tenant/intents/")):
        return "/v1/tenant/agents"
    if path.startswith(("/v1/handoffs", "/v1/delegations")):
        return "/v1/intents"
    return path


def operation(name):
    if name in {"lookup", "find_offers", "find_basket_offers", "search_products"}:
        return "lookup"
    if name in {"get_product", "product", "product_detail"} or name.startswith("get_product_"):
        return "product_detail"
    if name in {"select_plan", "create_quote", "quote_intent"}:
        return "quote"
    if name in {"place_order", "submit_intent", "cancel", "cancel_order"}:
        return "order"
    if name in {"get_intent", "get_intent_checkpoint", "list_open_intents", "list_tasks", "get_task", "get_rfq"}:
        return "rfq_read"
    return name


def a2a_principal(gateway, p, params, headers, meta):
    from .a2a import t, metadata
    p = gateway.handoff_authority.declared(p, meta.get("on_behalf_of") or headers.get("on-behalf-of"))
    token = PRINCIPAL.set(p)
    try:
        if p.delegation_id and not isinstance(params, t.SendMessageRequest):
            id = None
            if isinstance(params, t.GetTaskRequest):
                with gateway.db.connect() as conn:
                    id = metadata(gateway.a2a._load(conn, params.id, p)).get("intent_id") or "outside"
            gateway.handoff_authority.guard("rfq_read", id)
        gateway.handoff_authority.record_call(p, params.DESCRIPTOR.name if params is not None else "a2a")
    finally:
        PRINCIPAL.reset(token)
    return p


def filter_tasks(gateway, p, tasks):
    if not p.delegation_id:
        return tasks
    from .a2a import metadata
    with gateway.db.connect() as conn:
        grant = gateway.handoff_authority.valid_grant(conn, p.delegation_id, p.tenant_id)
    return [task for task in tasks if metadata(task).get("intent_id") == grant["intent_id"]]


def rest_principal(service, p, request):
    p = service.handoff_authority.declared(p, request.headers.get("on-behalf-of") or request.headers.get("on_behalf_of"))
    path = request.url.path
    # Public intent routes and the original aliases share authority and audit handling.
    if path.startswith(("/v1/intents/handoffs/", "/v1/intents/delegations")):
        path = path.replace("/v1/intents/", "/v1/", 1)
    token = PRINCIPAL.set(p)
    try:
        if p.delegation_id:
            if path.startswith("/v1/products/") and request.method == "GET":
                action = "product_detail"
                with service.db.connect() as conn:
                    grant = service.handoff_authority.valid_grant(conn, p.delegation_id, p.tenant_id)
                    obj = service.intents.load(conn, grant["intent_id"])
                    if request.query_params.get("supplier_id", "valio") != obj["request"]["supplier_id"]:
                        raise DomainError("OUTSIDE_DELEGATION", "Product research must stay inside the intent supplier", 403)
                    product = service.suppliers.get(request.query_params.get("supplier_id", "valio")).catalog.get(request.path_params.get("id", path.split("/")[-1]))
                    service.governance.check_operation(conn, p.id, "lookup", obj["request"]["customer_id"], products=[product])
            elif path.startswith("/v1/intents") and request.method == "GET":
                action = "rfq_read"
            elif path == "/v1/delegations" and request.method == "POST":
                action = None  # The child-grant handler checks depth and scope together.
            elif path == "/v1/offers" and request.method == "POST":
                action = "lookup"
            elif path == "/v1/quotes" and request.method == "POST":
                action = "quote"
            elif path == "/v1/orders" and request.method == "POST":
                action = "order"
            elif path in {"/mcp", "/a2a"}:
                action = None  # SDK message boundaries check the exact operation.
            else:
                action = "outside"
            if action:
                service.handoff_authority.guard(action, request.path_params.get("id") if path.startswith("/v1/intents/") else None)
        if path.startswith("/v1/handoffs/") and path.endswith("/accept"):
            path = "/v1/handoffs/[redacted]/accept"
        service.handoff_authority.record_call(p, request.method + " " + path,
            request.path_params.get("id") if path.startswith("/v1/intents/") else None)
    except Exception:
        PRINCIPAL.reset(token)
        raise
    return p


@router.get("/v1/intents", response_model=dict)
def list_open_intents(request: Request, state: str = "open", p: Principal = Depends(resolve_principal)):
    if state != "open":
        raise DomainError("VALIDATION_ERROR", "Supported state filter is open", 422)
    return request.app.state.gateway.handoff_authority.open(p)


@router.get("/v1/intents/{id}/checkpoint", response_model=dict)
def get_intent_checkpoint(id: str, request: Request, p: Principal = Depends(resolve_principal),
                          mandate: str | None = Header(default=None, alias="Agent-Mandate")):
    return request.app.state.gateway.handoff_authority.checkpoint(id, p.id, mandate)


def confirmed(service, p, operation, target, body, apply, evidence=None):
    from .tenant_api import confirm_rest
    from .commands import json_value
    payload = body.model_dump(mode="json", exclude={"confirmation_token"})
    if evidence is not None:
        payload["evidence"] = evidence
    return json_value(confirm_rest(service, p, operation, target, payload,
                        body.confirmation_token, apply))


def review_evidence(service, conn, obj):
    return {"intent_id": obj["id"], "intent_version": obj["version"], "state": obj["state"],
        "holder": obj["active_agent"], "request": obj["request"],
        "quote": service.orders._quote(conn, obj["quote_id"]).model_dump(mode="json") if obj.get("quote_id") else None,
        "order": service.orders._order(conn, obj["order_id"]).model_dump(mode="json") if obj.get("order_id") else None,
        "effective_envelope": service.handoff_authority.snapshot(conn, obj, obj["active_agent"])}


@router.post("/v1/intents/{id}/handoff", response_model=dict)
@router.post("/v1/tenant/intents/{id}/handoff", response_model=dict)
def handoff_intent(id: str, body: Handoff, request: Request, p: Principal = Depends(resolve_principal),
                   mandate: str | None = Header(default=None, alias="Agent-Mandate")):
    service = request.app.state.gateway
    with service.db.connect(write=True) as conn:
        obj = service.intents.load(conn, id)
        owner = service.handoff_authority.owner(conn, obj, p)
        apply = lambda: service.handoff_authority.create_handoff(id, body, p, mandate)
        return confirmed(service, p, "handoff", id, body, apply, review_evidence(service, conn, obj)) if owner and body.mode == "may_commit" else apply()


@router.post("/v1/handoffs/{id}/approve", response_model=dict, include_in_schema=False)
@router.post("/v1/intents/handoffs/{id}/approve", response_model=dict)
@router.post("/v1/tenant/handoffs/{id}/approve", response_model=dict)
def approve_handoff(id: str, body: Confirmation, request: Request, p: Principal = Depends(resolve_principal)):
    service = request.app.state.gateway
    with service.db.connect(write=True) as conn:
        row = conn.execute("SELECT intent_id,data FROM authority_handoffs WHERE id=?", (id,)).fetchone()
        obj = service.intents.load(conn, row[0]) if row else None
        if not obj or not service.handoff_authority.owner(conn, obj, p):
            raise DomainError("OWNER_REQUIRED", "The holder's human owner must confirm", 403)
        evidence = {**review_evidence(service, conn, obj), "handoff": service.handoff_authority.public(json.loads(row["data"]))}
        return confirmed(service, p, "handoff_approval", id, body, lambda: service.handoff_authority.approve_handoff(id, p), evidence)


@router.post("/v1/handoffs/{code}/accept", response_model=dict, include_in_schema=False)
@router.post("/v1/intents/handoffs/{code}/accept", response_model=dict)
def accept_handoff(code: str, request: Request, p: Principal = Depends(resolve_principal),
                   mandate: str | None = Header(default=None, alias="Agent-Mandate")):
    return request.app.state.gateway.handoff_authority.accept(code, p, mandate)


@router.post("/v1/delegations", response_model=dict, include_in_schema=False)
@router.post("/v1/intents/delegations", response_model=dict)
@router.post("/v1/tenant/delegations", response_model=dict)
def delegate_task(body: Delegation, request: Request, p: Principal = Depends(resolve_principal),
                  mandate: str | None = Header(default=None, alias="Agent-Mandate")):
    service = request.app.state.gateway
    if "order" in body.operations:
        with service.db.connect(write=True) as conn:
            obj = service.intents.load(conn, body.intent_id)
            if not service.handoff_authority.owner(conn, obj, p):
                raise DomainError("OWNER_REQUIRED", "Order delegation needs the human owner", 403)
            return confirmed(service, p, "delegation", body.intent_id, body, lambda: service.handoff_authority.delegate(body, p, mandate), review_evidence(service, conn, obj))
    return service.handoff_authority.delegate(body, p, mandate)


@router.post("/v1/delegations/{id}/revoke", response_model=dict, include_in_schema=False)
@router.post("/v1/intents/delegations/{id}/revoke", response_model=dict)
def revoke_delegation(id: str, request: Request, p: Principal = Depends(resolve_principal)):
    return request.app.state.gateway.handoff_authority.revoke(id, p)


def register(server, gateway, current_agent, presented_mandate):
    from .agent_surface import principal

    @server.tool(description="Create a single-use handoff for the current holder. may_commit waits for the authenticated human owner's confirmed decision; preparation never grants order authority.")
    def handoff_intent(intent_id: str, request: Handoff) -> dict[str, Any]:
        return gateway.handoff_authority.create_handoff(intent_id, request, principal(server), presented_mandate.get())

    @server.tool(description="Accept a single-use handoff using this recipient's own agent credential; atomically move the exclusive holder and preserve the work context.")
    def accept_handoff(code: str) -> dict[str, Any]:
        return gateway.handoff_authority.accept(code, principal(server), presented_mandate.get())

    @server.tool(description="Issue an attenuated, intent-bound helper credential. Defaults to read-only research; order requires human owner approval and further hops require remaining depth.")
    def delegate_task(request: Delegation) -> dict[str, Any]:
        return gateway.handoff_authority.delegate(request, principal(server), presented_mandate.get())

    @server.tool(description="Read the transport-independent intent checkpoint with pending actions and artifact references, without credentials.")
    def get_intent_checkpoint(intent_id: str) -> dict[str, Any]:
        return gateway.handoff_authority.checkpoint(intent_id, current_agent(), presented_mandate.get())

    @server.tool(description="Find open intents held by this agent across all its connections and sessions.")
    def list_open_intents() -> dict[str, Any]:
        return gateway.handoff_authority.open(principal(server))

    @server.resource("luotsi://intents/open", description="Open buying work owned by this tenant and agent, independent of session.")
    def open_intents() -> str:
        return json.dumps(gateway.handoff_authority.open(principal(server)))


async def mcp_boundary(server, name, arguments, context, invoke):
    from .agent_surface import principal
    from .recovery import problem
    from mcp.types import CallToolResult, TextContent
    token = None
    try:
        p = principal(server)
        meta = getattr(context, "meta", None)
        if meta is None and context is not None:
            try:
                meta = context.request_context.meta
            except (ValueError, AttributeError):
                pass
        if hasattr(meta, "model_dump"):
            meta = meta.model_dump()
        p = server.gateway.handoff_authority.declared(p, (meta or {}).get("on_behalf_of"))
        token = PRINCIPAL.set(p)
        if name != "delegate_task":
            server.gateway.handoff_authority.guard(operation(name))
        server.gateway.handoff_authority.record_call(p, name, (arguments or {}).get("intent_id"))
        return await invoke(server, name, arguments, context)
    except DomainError as error:
        value = problem(error)
        return CallToolResult(is_error=True, structured_content=value, content=[TextContent(type="text", text=json.dumps(value))])
    finally:
        if token is not None:
            PRINCIPAL.reset(token)


def send_command(service, params, p, mandate, command):
    name = command.get("command")
    if name not in {"handoff_intent", "accept_handoff", "delegate_task", "get_intent_checkpoint", "list_open_intents"}:
        return None
    from .connection_contract_surface import validate_arguments
    validate_arguments(command, {"type": "object", "properties": {
        "version": {"const": 1}, "command": {"const": name}, "arguments": {"type": "object"},
        "_meta": {"type": "object"}}, "required": ["version", "command", "arguments"], "additionalProperties": False})
    from .a2a import t, artifact, proto, metadata, data_part
    from google.protobuf.json_format import MessageToDict
    from .canonical import canonical_json
    if command.get("version") != 1 or params.message.role != t.Role.ROLE_USER or not params.message.message_id:
        raise DomainError("VALIDATION_ERROR", "A version-1 user message with an ID is required", 422)
    args = command.get("arguments", {})
    fields, required = {
        "handoff_intent": ({"intent_id": {"type": "string"}, "request": Handoff.model_json_schema()}, ["intent_id", "request"]),
        "accept_handoff": ({"code": {"type": "string"}}, ["code"]),
        "delegate_task": ({"request": Delegation.model_json_schema()}, ["request"]),
        "get_intent_checkpoint": ({"intent_id": {"type": "string"}}, ["intent_id"]),
        "list_open_intents": ({}, []),
    }[name]
    validate_arguments(args, {"type": "object", "properties": fields, "required": required, "additionalProperties": False})
    scope = "agent:" + p.id
    fingerprint = sha256(canonical_json(MessageToDict(params.message))).hexdigest()
    with service.db.connect(write=True) as conn:
        previous = conn.execute("SELECT * FROM a2a_messages WHERE owner=? AND message_id=?", (scope, params.message.message_id)).fetchone()
        if previous:
            if previous["digest"] != fingerprint:
                raise DomainError("IDEMPOTENCY_CONFLICT", "Message ID is bound to another command", 409)
            # Single-use acceptance is never silently replayed as a second accept.
            if name == "accept_handoff":
                raise DomainError("HANDOFF_USED", "Handoff code was already accepted", 409)
            return service._load(conn, previous["task_id"], p)
        manager = service.gateway.handoff_authority
        if name == "accept_handoff":
            result = manager.accept(args["code"], p, mandate)
        elif name == "handoff_intent":
            result = manager.create_handoff(args["intent_id"], Handoff.model_validate(args["request"]), p, mandate)
        elif name == "delegate_task":
            result = manager.delegate(Delegation.model_validate(args["request"]), p, mandate)
        elif name == "list_open_intents":
            result = manager.open(p)
        else:
            result = manager.checkpoint(args["intent_id"], p.id, mandate)
        id = result.get("intent_id") or args.get("intent_id") or args.get("request", {}).get("intent_id")
        existing = conn.execute("SELECT data FROM a2a_tasks WHERE json_extract(data,'$.metadata.intent_id')=? AND owner=? LIMIT 1", (id, scope)).fetchone() if id else None
        task = proto(t.Task, json.loads(existing[0])) if existing else t.Task(id=uuid4().hex, context_id=result.get("context_id") or params.message.context_id or uuid4().hex)
        history = t.Message()
        history.CopyFrom(params.message)
        if name == "accept_handoff":
            history.parts.clear()
            history.parts.append(data_part({"version": 1, "command": name, "arguments": {"code": "[redacted]"}}))
        task.history.append(history)
        task.metadata.update({"owner": p.id, "owner_scope": scope, "correlation_id": correlation_id(),
                              "actor_chain": list(p.actor_chain or (p.id,))})
        if id:
            obj = service.gateway.intents.load(conn, id)
            task.context_id = obj.get("context_id", id)
            task.metadata.update({"intent_id": id, "intent_version": obj["version"],
                "context": {"customer_id": obj["request"]["customer_id"], "environment": service.gateway.settings.environment}})
        # Credentials are delivered to this call only, never persisted in tasks or events.
        safe = {k: v for k, v in result.items() if k not in {"credential", "code"}}
        artifact(task, name, safe)
        service._update(conn, task, t.TaskState.TASK_STATE_INPUT_REQUIRED, "Buying work " + name, safe)
        conn.execute("INSERT INTO a2a_messages(owner,message_id,digest,task_id) VALUES(?,?,?,?)", (scope, params.message.message_id, fingerprint, task.id))
        if "credential" in result or "code" in result:
            artifact(task, "single_use_credential", {k: result[k] for k in ("credential", "code") if k in result})
        return task


