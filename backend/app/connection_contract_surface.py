"""Schema boundary and installed SDK integration for connection contract 63."""
import asyncio
import copy
import difflib
import json
import weakref
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from jsonschema import Draft202012Validator
from mcp import types as mt
from mcp.server.mcpserver import Context
from pydantic import BaseModel, ConfigDict

from . import audit
from .auth import resolve_principal
from .connection_contract import CONTRACT_META, ECHO, SIGNED_QUOTE, manager
from .db import DomainError
from .foundation import PRINCIPAL
from .recovery import problem

RAW_ARGUMENTS = ContextVar("connection_contract_raw_arguments", default=None)


async def mcp_boundary(server, name, arguments, context, invoke):
    token = RAW_ARGUMENTS.set(arguments or {})
    from .connection_trust_lifecycle import READ_OPERATION, read_tool
    read = read_tool(server, name)
    read_token = READ_OPERATION.set(read)
    try:
        server.gateway.connection_trust.check(PRINCIPAL.get(), mutation=not read)
        return await invoke(server, name, arguments, context)
    finally:
        RAW_ARGUMENTS.reset(token)
        READ_OPERATION.reset(read_token)


def supplied_arguments(arguments):
    raw = RAW_ARGUMENTS.get()
    return dict(raw) if raw is not None else arguments


def apply_explicit_nulls(cap, arguments, values):
    # A URI query has no JSON null representation. After the published schema
    # validates it, retain an explicit SDK null rather than replacing it with a
    # FastAPI query default. Absent arguments still use the published defaults.
    from .agent_surface import argument_name
    for field in cap.route.dependant.query_params:
        name = argument_name(field.alias)
        if name in arguments and arguments[name] is None:
            values[field.name] = None


def strict_schema(schema):
    schema = copy.deepcopy(schema)
    def visit(value):
        if isinstance(value, dict):
            if "properties" in value:
                value["additionalProperties"] = False
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(schema)
    return schema


def validate_arguments(value, schema):
    """Reject, never repair or discard a client's arguments before dispatch."""
    def leaves(error):
        if error.context:
            return [leaf for child in error.context for leaf in leaves(child)]
        return [error]

    errors = sorted((leaf for error in Draft202012Validator(schema).iter_errors(value) for leaf in leaves(error)),
        key=lambda e: (e.validator != "additionalProperties", e.validator != "required", -len(e.absolute_path), str(e.absolute_path)))
    if not errors:
        return
    error = errors[0]
    path = list(error.absolute_path)
    data = {"violations": []}
    code = "VALIDATION_ERROR"
    if error.validator == "additionalProperties" and isinstance(error.instance, dict):
        unknown = sorted(set(error.instance) - set(error.schema.get("properties", {})))
        if unknown:
            field = unknown[0]
            known = list(error.schema.get("properties", {}))
            nearest = difflib.get_close_matches(field, known, n=1, cutoff=0)
            path.append(field)
            data.update(field=".".join(map(str, path)), nearest_known_field=nearest[0] if nearest else None)
            code = "UNKNOWN_ARGUMENT"
    elif error.validator == "required":
        missing = next((k for k in error.validator_value if k not in error.instance), None)
        path.append(missing)
        data["field"] = ".".join(map(str, path))
        code = "ARGUMENT_REQUIRED"
    data["violations"].append({"field": ".".join(map(str, path)), "problem": error.message,
        "valid_values": error.schema.get("enum", list(error.schema.get("properties", {}))), "nearest_known_field": data.get("nearest_known_field")})
    message = f"{'.'.join(map(str, path))}: {error.message}" if path else error.message
    if code == "UNKNOWN_ARGUMENT":
        raise DomainError("UNKNOWN_ARGUMENT", message, 422, data)
    if code == "ARGUMENT_REQUIRED":
        raise DomainError("ARGUMENT_REQUIRED", message, 422, data)
    raise DomainError("VALIDATION_ERROR", message, 422, data)


class ContractBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract_version: str
    protocol_revision: str
    client: str
    capabilities: dict = {}


class DrivingStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    check: str
    tool: str
    arguments: dict


router = APIRouter(tags=["connection contract"])


@router.get("/v1/connect/trading-profile", response_model=dict)
@router.get("/v1/trading-profile", response_model=dict, include_in_schema=False)
def trading_profile(request: Request):
    m = manager(request.app.state.gateway)
    return {"contract": m.public(), "profile": m.profile()}


def own_connection(gateway, cid, p):
    from .agent_surface import surface
    surface(gateway).health(cid, p)  # Reuses tenant/owner visibility, including the calling agent.
    return cid


@router.post("/v1/connect/connections/{connection_id}/contract", response_model=dict)
@router.post("/v1/connections/{connection_id}/contract", response_model=dict, include_in_schema=False)
def acknowledge_contract(connection_id: str, body: ContractBody, request: Request, p=Depends(resolve_principal)):
    g = request.app.state.gateway
    own_connection(g, connection_id, p)
    # An owner may inspect, but cannot impersonate the agent's successful use.
    if p.connection_id != connection_id:
        raise DomainError("CONNECTION_REQUIRED", "Acknowledge using the connection's own credential", 403)
    m = manager(g)
    return {"contract": m.public(), "connection": m.acknowledge(connection_id, body.contract_version,
        body.protocol_revision, body.client, body.capabilities)}


@router.post("/v1/connect/connections/{connection_id}/driving-test", response_model=dict)
@router.post("/v1/connections/{connection_id}/driving-test", response_model=dict, include_in_schema=False)
def start_driving_test(connection_id: str, request: Request, p=Depends(resolve_principal)):
    from .connection_contract_driving import driving
    own_connection(request.app.state.gateway, connection_id, p)
    if connection_id != p.connection_id:
        raise DomainError("CONNECTION_REQUIRED", "Run challenges through the agent's own connection", 403)
    return driving(request.app.state.gateway).start(p)


@router.post("/v1/connect/connections/{connection_id}/driving-test/{run_id}/steps", response_model=dict)
@router.post("/v1/connections/{connection_id}/driving-test/{run_id}/steps", response_model=dict, include_in_schema=False)
async def driving_test_step(connection_id: str, run_id: str, body: DrivingStep, request: Request, p=Depends(resolve_principal)):
    from .connection_contract_driving import driving
    if p.connection_id != connection_id:
        raise DomainError("CONNECTION_REQUIRED", "Use the connection that started the driving test", 403)
    return await driving(request.app.state.gateway).step(run_id, p, body.check, body.tool, body.arguments)


@router.post("/v1/connect/connections/{connection_id}/driving-test/{run_id}/finish", response_model=dict)
@router.post("/v1/connections/{connection_id}/driving-test/{run_id}/finish", response_model=dict, include_in_schema=False)
def finish_driving_test(connection_id: str, run_id: str, request: Request, p=Depends(resolve_principal)):
    from .connection_contract_driving import driving
    if p.connection_id != connection_id:
        raise DomainError("CONNECTION_REQUIRED", "Use the connection that started the driving test", 403)
    return driving(request.app.state.gateway).finish(run_id, p)


@router.post("/v1/connect/intents/{intent_id}/reevaluate", response_model=dict)
@router.post("/v1/intents/{intent_id}/reevaluate", response_model=dict, include_in_schema=False)
def reevaluate_intent(intent_id: str, request: Request, p=Depends(resolve_principal)):
    """A fresh lookup is evidence; acknowledging a version alone is not evaluation."""
    g, m = request.app.state.gateway, manager(request.app.state.gateway)
    with g.db.connect() as conn:
        row = conn.execute("SELECT data FROM lookups WHERE id=?", (intent_id,)).fetchone()
    if not row or json.loads(row[0]).get("agent_id") != p.id:
        raise DomainError("LOOKUP_NOT_FOUND", "Intent was not found", 404)
    from .models import NeedSpec
    old = json.loads(row[0])
    if old.get("basket"):
        raise DomainError("REEVALUATION_REQUIRED", "Repeat find_basket_offers with the original manifest under the refreshed contract", 409)
    result = g.find_offers(NeedSpec.model_validate(old["need"]), actor="agent:" + p.id)
    with g.db.connect(write=True, notify=False) as conn:
        conn.execute("UPDATE connection_contract_intents SET reevaluated_version=? WHERE id=? AND owner=?", (m.current()["version"], intent_id, p.id))
    return {"intent": m.intent(intent_id, p.id), "lookup": result.model_dump(mode="json")}


def install(app):
    app.include_router(router)

    @app.middleware("http")
    async def boundary(request, call_next):
        echo = ECHO.set(request.headers.get("luotsi-contract"))
        signed = SIGNED_QUOTE.set(request.headers.get("luotsi-contract-pin") == "signed-quote")
        try:
            if request.method in {"POST", "PUT", "PATCH"} and request.url.path.startswith("/v1/"):
                from starlette.routing import Match
                for route in app.routes:
                    if not hasattr(route, "body_field") or not route.body_field:
                        continue
                    if route.matches(request.scope)[0] == Match.FULL:
                        request.scope["route"] = route
                        try:
                            value = await request.json()
                        except (ValueError, UnicodeDecodeError):
                            break  # FastAPI's ordinary malformed-JSON error.
                        schema = route.body_field._type_adapter.json_schema()
                        validate_arguments(value, strict_schema(schema))
                        break
            return await call_next(request)
        except DomainError as error:
            # The strict schema boundary precedes FastAPI dependency resolution.
            # Preserve the route's authentication/role gates on malformed calls.
            refused = await app.state.parsing_auth_error(request)
            if refused is not None:
                return refused
            return JSONResponse(problem(error, request.url.path), error.status, media_type="application/problem+json")
        finally:
            ECHO.reset(echo)
            SIGNED_QUOTE.reset(signed)


class SDKContract:
    def __init__(self, server):
        self.server = server

    async def __call__(self, ctx, call_next):
        m, p = manager(self.server.gateway), PRINCIPAL.get()
        params = ctx.params or {}
        meta = params.get("_meta", {}) or {}
        from .connection_trust_lifecycle import CONTEXT, READ_OPERATION, context, read_tool
        observed = CONTEXT.get() or context()
        client = params.get("clientInfo", {}) if ctx.method == "initialize" else {}
        negotiated = getattr(ctx.session, "client_params", None)
        client_name = client.get("name") or getattr(getattr(negotiated, "client_info", None), "name", None)
        stateless_client = meta.get("io.modelcontextprotocol/clientInfo", {})
        client_name = client_name or (stateless_client.get("name") if isinstance(stateless_client, dict) else None)
        context_token = CONTEXT.set(context(observed["network"], "mcp", client_name or observed["client"]))
        read = ctx.method != "tools/call" or read_tool(self.server, params.get("name"))
        read_token = READ_OPERATION.set(read)
        echo = ECHO.set(meta.get(CONTRACT_META) or (ctx.request.headers.get("luotsi-contract") if ctx.request else None))
        signed = SIGNED_QUOTE.set(meta.get("luotsi/contractPin") == "signed-quote")
        try:
            if p:
                self.server.gateway.connection_trust.seen(p)
            if p and p.connection_id and ctx.method != "initialize":
                state = m.state(p.connection_id)
                previous = copy.deepcopy(state)
                if state["protocol_revision"] and state["protocol_revision"] != ctx.protocol_version:
                    for capability in state["capabilities"].values():
                        capability["status"] = "declared"
                state["protocol_revision"] = ctx.protocol_version
                for name, declaration in meta.get("io.modelcontextprotocol/clientCapabilities", {}).items():
                    state["capabilities"].setdefault(name, {"status": "declared", "declaration": declaration})
                if previous != state:
                    m.save_state(p.connection_id, state)
            if ctx.method == "subscriptions/listen":
                entry = (asyncio.get_running_loop(), weakref.ref(self.server), p)
                if entry not in m.streams:
                    m.streams.append(entry)
            result = await call_next(ctx)
            if ctx.method == "initialize":
                # The SDK negotiates revisions and serializes InitializeResult.
                # Middleware adds only the extension metadata after negotiation.
                current = m.public()
                if isinstance(result, dict):
                    result.setdefault("_meta", {})[CONTRACT_META] = current
                    revision = result.get("protocolVersion")
                else:
                    result.meta = {**(result.meta or {}), CONTRACT_META: current}
                    revision = result.protocol_version
                if p and p.connection_id:
                    client = params.get("clientInfo", {})
                    m.acknowledge(p.connection_id, ECHO.get() or current["version"], revision,
                        json.dumps(client, sort_keys=True), params.get("capabilities", {}))
                m.sessions[:] = [(loop, session) for loop, session in m.sessions if not loop.is_closed()]
                m.sessions.append((asyncio.get_running_loop(), ctx.session))
            elif p and p.connection_id and not (isinstance(result, dict) and result.get("isError")):
                capability = {"resources/read": "resources", "resources/templates/list": "resources",
                    "prompts/get": "prompts", "subscriptions/listen": "subscriptions"}.get(ctx.method)
                if capability and self.server.gateway.connection_trust.is_live(p):
                    m.prove(p.connection_id, capability)
            return result
        finally:
            ECHO.reset(echo)
            SIGNED_QUOTE.reset(signed)
            READ_OPERATION.reset(read_token)
            CONTEXT.reset(context_token)


def register(server):
    """Use SDK tools/resources/annotations/structured output, not protocol shims."""
    m = manager(server.gateway) if hasattr(server.gateway, "db") else None
    from mcp.server.lowlevel.server import NotificationOptions
    original_capabilities = server._lowlevel_server.get_capabilities
    def capabilities(notification_options=None, *args, **kwargs):
        options = notification_options or NotificationOptions()
        options.tools_changed = True
        return original_capabilities(options, *args, **kwargs)
    server._lowlevel_server.get_capabilities = capabilities

    @server.resource("luotsi://trading-profile", title="Commercial connection contract")
    def profile() -> str:
        return json.dumps({"contract": m.public(), "profile": m.profile()})

    # The connection-bound SDK tool replaces the generated REST adapter (which
    # otherwise asks an agent to repeat its own connection id and client info).
    server._tool_manager._tools.pop("acknowledge_contract", None)
    server.surface_generated.discard("acknowledge_contract")
    @server.tool(description="Acknowledge a contract version read from luotsi://trading-profile. Use after REFRESH_CONTRACT; repeat expired driving checks before ordering.")
    def acknowledge_contract(contract_version: str) -> dict[str, Any]:
        from .agent_surface import principal
        p = principal(server)
        if not p.connection_id:
            raise DomainError("CONNECTION_REQUIRED", "Use an issued connection credential", 403)
        return {"connection": m.acknowledge(p.connection_id, contract_version), "contract": m.public()}

    if server.audience == "buyer":
        for name in ("start_driving_test", "driving_test_step", "finish_driving_test"):
            server._tool_manager._tools.pop(name, None)
            server.surface_generated.discard(name)
        @server.tool(description="Open sandbox checks through this connection before enabling order scope. Next call driving_test_step; finish_driving_test signs measured results.")
        def start_driving_test() -> dict[str, Any]:
            from .agent_surface import principal
            from .connection_contract_driving import driving
            return driving(server.gateway).start(principal(server))

        @server.tool(description="Run an ordinary sandbox tool for a driving check through your own connection. Results measure actual behaviour; never submit claimed pass flags.")
        async def driving_test_step(run_id: str, check: str, tool: str, arguments: dict, ctx: Context) -> dict[str, Any]:
            from .agent_surface import principal
            from .connection_contract_driving import driving
            return await driving(server.gateway).step(run_id, principal(server), check, tool, arguments, ctx)

        @server.tool(description="Sign the observed driving result, including not_attempted checks. Unit, retry and approval must pass to enable ordering.")
        def finish_driving_test(run_id: str) -> dict[str, Any]:
            from .agent_surface import principal
            from .connection_contract_driving import driving
            return driving(server.gateway).finish(run_id, principal(server))

    for tool in server._tool_manager._tools.values():
        tool.parameters = strict_schema(tool.parameters)
    # Registration must not expand the bound legacy discovery inventory. Shared
    # gateways and explicit full_surface callers already advertise these tools.
    if m is not None:
        server._lowlevel_server.middleware.append(SDKContract(server))


def capture(server):
    if not hasattr(server.gateway, "db"):
        return
    from .models import Order
    manager(server.gateway).schemas[server.audience] = {name: {"input": t.parameters,
        "output": Order.model_json_schema(mode="serialization") if name in {"place_order", "get_order_status", "get_order_receipt", "cancel_order"} else t.fn_metadata.output_schema}
        for name, t in sorted(server._tool_manager._tools.items())}


async def question(server, ctx, schema, message):
    """Bounded SDK elicitation probe; only a validated response proves support."""
    from .agent_surface import principal
    from .agent_surface_interpret import supports_elicitation
    p = principal(server)
    m = manager(server.gateway)
    cid = p.connection_id or "legacy:" + p.id
    state = m.state(cid)
    capable = supports_elicitation(ctx)
    if capable and "elicitation" not in state["fallback_removed"]:
        try:
            async with asyncio.timeout(getattr(m, "elicitation_timeout_seconds", 2.0)):
                answer = await ctx.elicit(message, schema=schema)
            if answer.action == "accept" and answer.data is not None:
                m.prove(cid, "elicitation")
                return answer.data
            raise DomainError("CONFIRMATION_DECLINED", "Human question was declined", 409)
        except (TimeoutError, ConnectionError):
            pass
        m.fallback(cid, "elicitation")
    return None


def compatible_input_required(ctx, result):
    if not isinstance(result, mt.InputRequiredResult):
        return result
    try:
        version = ctx.request_context.protocol_version
    except (AttributeError, ValueError, RuntimeError):
        version = "2026-07-28"
    if version == "2026-07-28":
        return result
    # The installed SDK forbids InputRequiredResult in pre-2026 wire schemas.
    # Preserve its typed request/state within an ordinary structured tool result.
    value = {"status": "input_required", **result.model_dump(mode="json", by_alias=True, exclude_none=True)}
    return mt.CallToolResult(content=[mt.TextContent(type="text", text=json.dumps(value))],
                             structured_content=value, meta=result.meta)


async def notify_question(ctx, result):
    if ctx is None or not result.meta or not result.meta.get("approval_link"):
        return
    try:
        await ctx.session.send_notification(mt.ResourceUpdatedNotification(params=mt.ResourceUpdatedNotificationParams(
            uri="luotsi://agent/context", meta={"approval_link": result.meta["approval_link"]})))
    except (AttributeError, ValueError, RuntimeError, ConnectionError):
        pass  # The durable connection notification remains available for spec 61.


def validate_a2a_command(gateway, command, audience):
    """Validate the A2A envelope and its published SDK skill without rewriting it."""
    if not isinstance(command, dict):
        raise DomainError("VALIDATION_ERROR", "Supply object arguments", 422)
    envelope = {"type": "object", "additionalProperties": False, "properties": {
        "version": {"const": 1}, "command": {"const": "tool"}, "skill": {"type": "string"},
        "arguments": {"type": "object"}, "request_state": {"type": "string"},
        "input_responses": {"type": "object"}, "_meta": {"type": "object"}}, "required": ["version", "command", "skill"]}
    validate_arguments(command, envelope)
    from .mcp_server import create_server
    p = PRINCIPAL.get()
    server = create_server(gateway, p.id if p else None, audience=audience, full_surface=True)
    tool = server._tool_manager._tools.get(command["skill"])
    if tool:
        validate_arguments(command.get("arguments", {}), tool.parameters)


@contextmanager
def a2a_scope(gateway, params, p):
    from google.protobuf.json_format import MessageToDict
    from .a2a import command_data
    parts = [MessageToDict(part).get("data") for part in params.message.parts if part.WhichOneof("content") == "data"]
    command = command_data(parts[0]) if len(parts) == 1 else {}
    from hashlib import sha256
    from .canonical import canonical_json
    with gateway.db.connect() as conn:
        previous = conn.execute("SELECT digest FROM a2a_messages WHERE owner=? AND message_id=?",
            (f"{p.kind}:{p.id}", params.message.message_id)).fetchone()
    if previous and previous[0] != sha256(canonical_json(MessageToDict(params.message))).hexdigest():
        raise DomainError("IDEMPOTENCY_CONFLICT", "Message id belongs to a changed command", 409)
    meta = command.get("_meta", {}) if isinstance(command, dict) else {}
    from .connection_trust_lifecycle import READ_OPERATION, read_tool
    read = command.get("command") in {"lookup", "get_evidence"}
    if command.get("command") == "tool":
        from .mcp_server import create_server
        from .agent_surface import effective_audience
        read = read_tool(create_server(gateway, p.id, audience=effective_audience(p, "buyer"), full_surface=True), command.get("skill"))
    read_token = READ_OPERATION.set(read)
    echo = ECHO.set(meta.get(CONTRACT_META) or ECHO.get())
    signed = SIGNED_QUOTE.set(meta.get("luotsi/contractPin") == "signed-quote" or SIGNED_QUOTE.get())
    try:
        gateway.connection_trust.check(p, mutation=not read)
        if command.get("command") in {"lookup", "select_plan", "place_order", "cancel", "confirm_interpretation"}:
            quote_id = None
            if SIGNED_QUOTE.get() and command["command"] == "place_order" and params.message.task_id:
                from .a2a import metadata
                with gateway.db.connect() as conn:
                    quote_id = metadata(gateway.a2a._load(conn, params.message.task_id, p)).get("quote_id")
            manager(gateway).enforce(p, signed_quote=quote_id)
            common = {"version": {"const": 1}, "command": {"type": "string"}, "_meta": {"type": "object"}}
            fields = {"lookup": {"need": {"type": "object"}, "skill": {"type": "string"}, "arguments": {"type": "object"}},
                "select_plan": {"plan_index": {"type": "integer", "minimum": 0}}, "place_order": {},
                "cancel": {"order_id": {"type": "string"}, "reason": {"type": "string"}},
                "confirm_interpretation": {"confirm": {"type": "boolean"}, "request": {"type": "object"}, "kind": {"enum": ["need", "basket"]}}}[command["command"]]
            validate_arguments(command, {"type": "object", "properties": {**common, **fields}, "additionalProperties": False})
            if command["command"] == "lookup" and command.get("skill") != "find_basket_offers":
                from .models import NeedSpec
                validate_arguments(command.get("need", {}), strict_schema(NeedSpec.model_json_schema()))
        yield
    finally:
        ECHO.reset(echo)
        SIGNED_QUOTE.reset(signed)
        READ_OPERATION.reset(read_token)


def fallback_metadata(gateway, p, state):
    """Approval-link notification seam for spec 61, with a scoped pending input."""
    from .agent_surface import canonical_origin
    link = canonical_origin(gateway) + "/v1/connections/" + (p.connection_id or "legacy:" + p.id) + "/questions/" + state
    notification = {"type": "approval_link", "url": link, "resource": "luotsi://agent/context"}
    m = manager(gateway)
    cid = p.connection_id or "legacy:" + p.id
    record = m.state(cid)
    record.setdefault("notifications", []).append(notification)
    record["notifications"] = record["notifications"][-20:]
    m.save_state(cid, record)
    return {"approval_link": link, "notification": notification,
            "deadline": (audit.utcnow() + timedelta(minutes=5)).isoformat()}


@router.get("/v1/connect/connections/{connection_id}/questions/{state}", response_model=dict)
@router.get("/v1/connections/{connection_id}/questions/{state}", response_model=dict, include_in_schema=False)
def pending_question(connection_id: str, state: str, request: Request, p=Depends(resolve_principal)):
    g = request.app.state.gateway
    own_connection(g, connection_id, p)
    with g.db.connect() as conn:
        connection = conn.execute("SELECT agent_id FROM connections WHERE connection_id=?", (connection_id,)).fetchone()
        draft = conn.execute("SELECT * FROM agent_surface_drafts WHERE id=?", (state,)).fetchone()
    if not connection or not draft or json.loads(draft["owner"])[1] != connection[0] or draft["expires_at"] <= audit.utcnow().isoformat():
        raise DomainError("CONFIRMATION_INVALID", "Question is missing or expired", 409)
    return {"request_state": state, "purpose": draft["purpose"], "data": json.loads(draft["data"]),
            "expires_at": draft["expires_at"], "next_action": "WAIT_FOR_APPROVAL"}


class OverrideBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str


@router.post("/v1/connect/connections/{connection_id}/driving-test/override", response_model=dict)
@router.post("/v1/connections/{connection_id}/driving-test/override", response_model=dict, include_in_schema=False)
def request_driving_override(connection_id: str, body: OverrideBody, request: Request, p=Depends(resolve_principal)):
    from .connection_contract_driving import driving
    if not body.reason.strip():
        raise DomainError("ARGUMENT_REQUIRED", "An override requires a reason", 422, {"field": "reason"})
    return driving(request.app.state.gateway).request_override(connection_id, body.reason, p)


def register_connections(gateway, connections):
    from .agent_surface import surface
    from .connection_contract_driving import driving
    for connection in connections:
        if connection["revoked_at"] or connection["audience"] != "buyer":
            continue
        with gateway.db.connect() as conn:
            row = conn.execute("SELECT * FROM connections WHERE connection_id=?", (connection["connection_id"],)).fetchone()
        connection["connection_contract"] = driving(gateway).status(surface(gateway).connection_principal(row))
