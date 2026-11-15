"""Spec 44 tenant MCP and A2A adapters; all decisions use the REST boundary."""
import json
from contextlib import contextmanager
from uuid import uuid4

from fastapi import Depends, Request
from fastapi.concurrency import run_in_threadpool
from mcp import types as mt
from mcp.server.mcpserver.context import Context

from .auth import MCPBearerMiddleware, admit_credential, resolve_principal
from .commands import json_value
from .db import DomainError
from .foundation import PRINCIPAL
from .governance_api import MUTATIONS, Operation, invoke
from .mcp_server import GatewayMCPServer
from .recovery import problem
from .tenant_api import DecisionPreview


def create_tenant_server(gateway, keys=None):
    """Compatibility entry point for the combined tenant MCP audience."""
    from .mcp_server import create_server
    server = create_server(gateway, agent_id=None, audience="tenant")
    server.keys = keys or gateway.settings.keys
    return server


def register_tenant_tools(server):
    gateway = server.gateway
    @server.tool(description="Tenant agent governance: register (owner/status filters), effective_policy (every field's value/source_layer/version_id/author), activity, alerts, change_requests, versions/version, simulate (stored last-30-day quotes/orders, no upstream), report (from/to/signed), change_policy, rollback, approve_change/reject_change, pause/resume, transfer, pause_tenant/resume_tenant, guardrails/change_guardrails/guardrail_versions/rollback_guardrails. arguments uses agent_id, policy (partial AgentPolicy), reason, version_id, request_id, owner_user_id, from/to, signed as applicable. Mutations return input_required with a human confirmation request before applying; requester cannot approve its own loosening. OUTSIDE_GUARDRAIL includes limit/source; paused buyer calls get 423 AGENT_PAUSED. Customer and budget constraints are rechecked by the shared core.")
    def agent_governance(operation: Operation, arguments: dict, ctx: Context) -> mt.CallToolResult | mt.InputRequiredResult:
        p = PRINCIPAL.get()
        if not p or p.kind != "tenant_user":
            raise DomainError("HUMAN_REQUIRED", "A tenant human account is required", 403)
        gateway.limiter.consume(p.connection_id or p.id, "mutation" if operation in MUTATIONS else "read")
        token = None
        if ctx.request_state:
            response = (ctx.input_responses or {}).get("confirmation")
            if response is None or getattr(response, "action", None) != "accept" or (getattr(response, "content", None) or {}).get("confirm") is not True:
                raise DomainError("CONFIRMATION_INVALID", "The human must explicitly confirm this decision", 409)
            token = ctx.request_state
        result = invoke(gateway, p, operation, arguments, token)
        if isinstance(result, DecisionPreview):
            return mt.InputRequiredResult(request_state=result.confirmation_token, input_requests={"confirmation": mt.ElicitRequest(
                method="elicitation/create", params=mt.ElicitRequestFormParams(mode="form",
                    message="Confirm this governance decision: " + json.dumps(result.summary),
                    requested_schema={"type": "object", "properties": {"confirm": {"type": "boolean", "title": "Apply this decision"}}, "required": ["confirm"]}))})
        value = json_value(result)
        value = value if isinstance(value, dict) else {"items": value}
        return mt.CallToolResult(structured_content=value, content=[mt.TextContent(type="text", text=json.dumps(value))])
    return server


def install_tenant_a2a(app):
    from .a2a import Handler, ContextBuilder, t, data_part, command_data
    from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
    from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
    from a2a.utils.errors import InvalidParamsError, UnsupportedOperationError
    from google.protobuf.json_format import MessageToDict

    class TenantHandler(Handler):
        @contextmanager
        def authenticated(self, context, params=None):
            gateway = context.state["gateway"]
            headers = context.state.get("headers", {})
            scheme, _, secret = headers.get("authorization", "").partition(" ")
            try:
                if scheme.lower() != "bearer":
                    raise DomainError("UNAUTHENTICATED", "Bearer credential required", 401)
                p = admit_credential(gateway, context.state["keys"], context.state.get("source", "unknown"), secret, observe=False)
                if p.kind == "agent":
                    raise DomainError("HUMAN_REQUIRED", "Tenant A2A requires a human", 403)
                if p.audience != "tenant":
                    raise DomainError("WRONG_AUDIENCE", "Use a tenant-audience connection", 401)
                from .auth import observe_credential
                observe_credential(gateway, p, context.state.get("source", "unknown"))
                token = PRINCIPAL.set(p)
                try:
                    yield gateway, p
                finally:
                    PRINCIPAL.reset(token)
            except DomainError as error:
                raise InvalidParamsError(error.message, data=problem(error)) from None

        async def on_message_send(self, params, context):
            with self.authenticated(context, params) as (gateway, p):
                parts = [MessageToDict(part).get("data") for part in params.message.parts if part.HasField("data")]
                if len(parts) != 1:
                    raise InvalidParamsError("Send one JSON data part with operation and arguments")
                data = command_data(parts[0])
                try:
                    result = await run_in_threadpool(invoke, gateway, p, data.get("operation"), data.get("arguments", {}), data.get("confirmation_token"))
                except (KeyError, ValueError, TypeError):
                    raise InvalidParamsError("Invalid governance arguments") from None
                value = json_value(result)
                return t.Message(message_id=uuid4().hex, role=t.Role.ROLE_AGENT,
                    parts=[data_part(value if isinstance(value, dict) else {"items": value})])

        async def on_message_send_stream(self, params, context):
            raise UnsupportedOperationError("Tenant governance uses message/send with explicit two-step decisions")
            yield  # Required by the SDK's async-iterator interface.

    async def unsupported(self, params, context):
        raise UnsupportedOperationError("Tenant governance provides message/send only")
    for method in ("on_get_task", "on_list_tasks", "on_cancel_task", "on_get_task_push_notification_config", "on_create_task_push_notification_config",
                   "on_delete_task_push_notification_config", "on_list_task_push_notification_configs", "on_get_extended_agent_card"):
        setattr(TenantHandler, method, unsupported)

    add_a2a_routes_to_fastapi(app, jsonrpc_routes=create_jsonrpc_routes(TenantHandler(), rpc_url="/a2a/tenant", context_builder=ContextBuilder()))
    for route in app.routes:
        if getattr(route, "path", None) == "/a2a/tenant":
            route.openapi_extra = {**(route.openapi_extra or {}), "security": [{"BearerAuth": []}],
                "responses": {"200": {"description": "A2A 1.0 JSON-RPC governance response; decisions require human confirmation.",
                    "content": {"application/json": {"schema": {"type": "object"}}}}}}

    @app.get("/.well-known/agent-card/tenant.json", response_model=dict, summary="Tenant A2A 1.0 governance skills and human decision contract")
    @app.get("/a2a/tenant/agent-card.json", response_model=dict)
    def tenant_card(request: Request):
        base = str(request.base_url).rstrip("/")
        skills = [t.AgentSkill(id=operation, name=operation.replace("_", " "), description="Tenant-scoped agent governance; human confirmation required for decisions.", tags=["governance", "tenant"])
                  for operation in Operation.__args__]
        card = t.AgentCard(name="Buyer business agent governance", version="1.0.0", description="Spec 44 governance for tenant humans; data parts contain operation, arguments and optional confirmation_token. Mutations first return confirmation_required, summary and a five-minute single-use token; retry after human confirmation.",
            supported_interfaces=[t.AgentInterface(url=base+"/a2a/tenant", protocol_binding="JSONRPC", protocol_version="1.0")],
            capabilities=t.AgentCapabilities(streaming=False, push_notifications=False),
            security_schemes={"BearerAuth": t.SecurityScheme(http_auth_security_scheme=t.HTTPAuthSecurityScheme(scheme="bearer"))},
            default_input_modes=["application/json"], default_output_modes=["application/json"], skills=skills)
        from google.protobuf.json_format import ParseDict
        ParseDict({"schemes": {"BearerAuth": []}}, card.security_requirements.add())
        return MessageToDict(card)


class TenantMCPTransport:
    async def __call__(self, scope, receive, send):
        # The streamable HTTP server checks its configured URI; preserve the
        # resource URL rather than redirecting to the buyer resource.
        await scope["app"].state.tenant_mcp_http(scope, receive, send)
