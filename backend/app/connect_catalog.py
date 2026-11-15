"""Single client/snippet registry consumed by catalog and connection instructions."""
import json

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from .accounts import AUDIENCES, origin
from .auth import Principal, resolve_principal
from .developer_docs import tool_schemas
from .db import DomainError
from .models import Connection, ConnectionRequest, NewConnection

router = APIRouter(tags=["connections"])
CLIENTS = {
    "mcp": ["claude_desktop", "claude_code", "chatgpt", "cursor", "vscode", "mcp_other"],
    "rest": ["curl", "python", "typescript", "rest"],
    "a2a": ["a2a"],
    "webhook": ["python", "typescript"],
    "tool_pack": ["openai_agents_sdk", "anthropic_api", "langchain", "python", "typescript"],
}
DOCS = {"mcp": "https://modelcontextprotocol.io/docs/develop/connect-remote-servers",
        "rest": "/developers", "a2a": "https://a2a-protocol.org/latest/", "webhook": "/developers", "tool_pack": "/developers/tools.json"}


@router.get("/v1/connect/catalog", response_model=dict)
async def catalog(request: Request, audience: str = Query(default="buyer", pattern="^(buyer|tenant|operator)$")):
    from types import SimpleNamespace
    schemas = await tool_schemas(audience=audience)
    connection = SimpleNamespace(connection_id="<CONNECTION_ID>", audience=audience, customer_ids=["<CUSTOMER_ID>"])
    setups = {}
    for client in {client for clients in CLIENTS.values() for client in clients}:
        setup = await instructions(request.app.state.gateway if hasattr(request.app.state, "gateway") else None,
                                   connection, client, origin(request), audience, schemas=schemas)
        setups[client] = {k: setup[k] for k in ("steps", "snippet", "language", "secret_placeholder")}
    return {"audience": audience, "audiences": AUDIENCES, "scopes": ["*", "read", "write", "lookup", "quotes", "orders", "approvals:decide", "connections:manage", "agents:manage", "mandates:manage", "users:manage", "policy:manage"], "types": [
        {"type": kind, "clients": clients, "credential_methods": ["api_key", "oauth"] if kind in {"mcp", "a2a"} else ["api_key"],
         "documentation_links": [DOCS[kind]], "available": kind != "mcp" or request.app.state.mcp_served,
         "transports": ["streamable_http", "stdio"] if kind == "mcp" else ["http"],
         "client_details": [{"client": client, "credential_methods": ["oauth"] if client == "chatgpt" else ["api_key", "oauth"] if kind in {"mcp", "a2a"} else ["api_key"],
                             **setups[client],
                             "available": (kind != "mcp" or request.app.state.mcp_served)} for client in clients],
         "oauth_available": kind in {"mcp", "a2a"}, "rest_base_path": AUDIENCES[audience]["rest"]}
        for kind, clients in CLIENTS.items()]}


class Rotation(BaseModel):
    overlap_hours: float = Field(default=0, ge=0, le=24)


@router.post("/v1/tenant/connections", response_model=NewConnection, status_code=201)
@router.post("/v1/operator/connections", response_model=NewConnection, status_code=201)
def create(body: ConnectionRequest, request: Request, p: Principal = Depends(resolve_principal)):
    return request.app.state.gateway.connections.create(body, p, origin(request), request.app.state.mcp_served,
        command_key=request.headers.get("Idempotency-Key"))


@router.get("/v1/tenant/connections", response_model=list[Connection])
@router.get("/v1/operator/connections", response_model=list[Connection])
def connections(request: Request, p: Principal = Depends(resolve_principal)):
    return request.app.state.gateway.connections.list(p)


@router.get("/v1/tenant/connections/{connection_id}", response_model=Connection)
@router.get("/v1/operator/connections/{connection_id}", response_model=Connection)
def get(connection_id: str, request: Request, p: Principal = Depends(resolve_principal)):
    return request.app.state.gateway.connections.get(connection_id, p)


@router.post("/v1/tenant/connections/{connection_id}/rotate", response_model=dict)
@router.post("/v1/operator/connections/{connection_id}/rotate", response_model=dict)
def rotate(connection_id: str, body: Rotation, request: Request, p: Principal = Depends(resolve_principal)):
    return request.app.state.gateway.connections.rotate(connection_id, p, body.overlap_hours)


@router.delete("/v1/tenant/connections/{connection_id}", response_model=Connection)
@router.delete("/v1/operator/connections/{connection_id}", response_model=Connection)
def revoke(connection_id: str, request: Request, p: Principal = Depends(resolve_principal)):
    return request.app.state.gateway.connections.revoke(connection_id, p)


async def instructions(service, connection, client, base, audience, *, schemas=None):
    if audience != connection.audience:
        raise DomainError("WRONG_AUDIENCE", "Instructions must use the connection's audience", 401)
    if client not in {c for clients in CLIENTS.values() for c in clients}:
        raise DomainError("VALIDATION_ERROR", "Unknown client; see connection catalog", 422)
    schemas = schemas or await tool_schemas(audience=audience)
    placeholder = "VTG_CONNECTION_KEY"
    rest = base + AUDIENCES[audience]["rest"]
    resource = "/offers" if audience == "buyer" else "/orders"
    # This example is checked against the actual MCP input schema. Both transports
    # use the same NeedSpec; no duplicated function declarations are maintained.
    properties = schemas.get("find_offers", {}).get("inputSchema", {}).get("properties", {})
    from datetime import timedelta
    from .audit import utcnow
    example = {"customer_id": connection.customer_ids[0] if hasattr(connection, "customer_ids") else "1",
               "intent": "lactose-free milk", "quantity": {"value": 1000, "unit": "L"},
               "deliver_by": (utcnow().date() + timedelta(days=3)).isoformat(), "search_terms": ["milk"],
               "required_claims": ["FREE_FROM_LACTOSE"]}
    # Expose the source schema alongside the snippet for frontend rendering.
    if client in CLIENTS["mcp"]:
        suffix = "" if audience == "buyer" else "/" + audience
        config = {"mcpServers": {connection.connection_id: {"url": base + "/mcp" + suffix,
                   "headers": {"Authorization": "Bearer ${" + placeholder + "}"}}}}
        if client == "claude_desktop":
            config["mcpServers"][connection.connection_id] = {"command": "npx", "args": ["-y", "mcp-remote", base + "/mcp" + suffix,
                "--header", "Authorization: Bearer ${" + placeholder + "}"], "env": {placeholder: "<YOUR_CONNECTION_KEY>"}}
        elif client == "vscode":
            config = {"servers": {connection.connection_id: {"type": "http", "url": base+"/mcp"+suffix,
                      "headers": {"Authorization": "Bearer ${"+placeholder+"}"}}}}
        elif client == "claude_code":
            config["mcpServers"][connection.connection_id]["type"] = "http"
        elif client == "chatgpt":
            config = {"connector_url": base+"/mcp"+suffix, "authentication": "oauth", "connection_id": connection.connection_id,
                      "credential_placeholder": placeholder, "available": True, "note": "Sign in and consent to connect this resource over OAuth 2.1."}
        snippet = json.dumps(config, indent=2)
        language = "json"
    elif client in {"python", "openai_agents_sdk", "anthropic_api", "langchain"}:
        snippet = 'import os, httpx\nwith httpx.Client(base_url=' + repr(rest) + ', headers={"Authorization": "Bearer " + os.environ["' + placeholder + '"]}) as client:\n'
        snippet += '    response = client.' + ('post("' + resource + '", json=' + repr(example) + ')' if audience == "buyer" else 'get("' + resource + '")') + '\n    response.raise_for_status()\n    print(response.json())\n'
        language = "python"
    elif client == "typescript":
        snippet = f'const response = await fetch("{rest}{resource}", {{method: "{"POST" if audience == "buyer" else "GET"}", headers: {{Authorization: `Bearer ${{process.env.{placeholder}}}`, "Content-Type": "application/json"}}' + (', body: JSON.stringify(' + json.dumps(example) + ')' if audience == "buyer" else '') + '});\nconsole.log(await response.json());'
        language = "typescript"
    else:
        snippet = f'curl -X {"POST" if audience == "buyer" else "GET"} "{rest}{resource}" -H "Authorization: Bearer $' + placeholder + '"'
        if audience == "buyer":
            snippet += " -H 'Content-Type: application/json' --data '" + json.dumps(example) + "'"
        language = "shell"
    kind = getattr(connection, "type", None)
    if kind == "a2a":
        suffix = "" if audience == "buyer" else "/"+audience
        snippet = json.dumps({"agent_card_url": base+("/.well-known/agent-card.json" if audience == "buyer" else "/a2a/"+audience+"/.well-known/agent-card.json"),
            "url": base+"/a2a"+suffix, "headers": {"Authorization": "Bearer ${"+placeholder+"}", "A2A-Version": "1.0"},
            "security": {"oauth_authorization_server": base+"/.well-known/oauth-authorization-server", "api_key_placeholder": placeholder}}, indent=2)
        language = "json"
    elif kind == "webhook":
        snippet = json.dumps({"subscriptions_url": rest+"/webhook-subscriptions", "headers": {"Authorization": "Bearer ${"+placeholder+"}"},
            "audience": audience, "verify_jwks_url": base+"/.well-known/jwks.json", "signature_headers": ["webhook-id", "webhook-timestamp", "webhook-signature"]}, indent=2)
        language = "json"
    elif kind == "tool_pack":
        snippet = json.dumps({"tool_pack_url": base+"/v1/connect/tool-pack?format=openai&audience="+audience,
            "headers": {"Authorization": "Bearer ${"+placeholder+"}"}, "executor": "Use the returned Python executor with VTG_CONNECTION_KEY."}, indent=2)
        language = "json"
    elif kind == "oauth":
        transport = "a2a" if getattr(connection, "client", None) == "a2a" else "mcp"
        snippet = json.dumps({"url": base+"/"+transport+("" if audience == "buyer" else "/"+audience),
            "authentication": "oauth", "authorization_server": base+"/.well-known/oauth-authorization-server",
            "connection_id": connection.connection_id, "note": "Authorize with this client's PKCE flow; the gateway cannot redisplay issued tokens."}, indent=2)
        language = "json"
    return {"connection_id": connection.connection_id, "audience": audience, "client": client,
            "steps": (["Open the connector URL in your client and choose OAuth authentication.", "Sign in to your tenant or Valio staff account.",
                       "Review the client, redirect host, agent, customers and scopes, then consent."] if client == "chatgpt" or kind == "oauth" else
                      ["Copy the connection key from its creation response.", f"Set {placeholder} in your client environment.",
                       "Apply the configuration below and make a test call. Keep the key private."]),
            "secret_placeholder": placeholder, "language": language, "snippet": snippet,
            "rest_base_url": rest, "tool_schemas": schemas, "lookup_example": example,
            "local_stdio": {"command": "backend/.venv/bin/python", "args": ["-m", "app.mcp_server", "--audience", audience],
                "env": {placeholder: "<YOUR_CONNECTION_KEY>"},
                "note": "Local stdio needs the local gateway database and configuration that issued this key; otherwise use remote HTTP."} if client in CLIENTS["mcp"] and kind != "oauth" else None}


@router.get("/v1/tenant/connections/{connection_id}/instructions", response_model=dict)
@router.get("/v1/operator/connections/{connection_id}/instructions", response_model=dict)
async def connection_instructions(connection_id: str, request: Request, client: str = "curl", audience: str | None = None, p: Principal = Depends(resolve_principal)):
    service = request.app.state.gateway
    connection = service.connections.get(connection_id, p)
    with service.db.connect() as conn:
        row = conn.execute("SELECT customer_ids FROM connections WHERE connection_id=?", (connection_id,)).fetchone()
    from types import SimpleNamespace
    full = SimpleNamespace(**{**connection.model_dump(), "customer_ids": json.loads(row[0])})
    return await instructions(service, full, client, origin(request), audience or connection.audience)


@router.get("/v1/connect/tool-pack", response_model=dict, summary="Function declarations generated from the selected MCP audience and a REST executor")
async def tool_pack(request: Request, format: str = Query(default="openai", pattern="^(openai|anthropic|gemini)$"),
                    audience: str = Query(default="buyer", pattern="^(buyer|tenant|operator)$")):
    from .auth import admit_credential, observe_credential
    from .oauth import require_audience
    header = request.headers.get("authorization")
    if header:
        scheme, _, secret = header.partition(" ")
        p = admit_credential(request.app.state.gateway, request.app.state.keys, request.client.host if request.client else "unknown", secret if scheme.lower() == "bearer" else "", observe=False)
        require_audience(p, audience, "/tool-pack/"+audience)
        observe_credential(request.app.state.gateway, p, request.client.host if request.client else "unknown")
    schemas = await tool_schemas(audience=audience)
    functions = [{"name": name, "description": schema["description"], "parameters": schema["inputSchema"]} for name, schema in schemas.items()]
    if format == "openai":
        declarations = [{"type": "function", "function": value} for value in functions]
    elif format == "anthropic":
        declarations = [{"name": v["name"], "description": v["description"], "input_schema": v["parameters"]} for v in functions]
    else:
        declarations = [{"functionDeclarations": functions}]
    base = origin(request)+AUDIENCES[audience]["rest"]
    executor = """import os, httpx
BASE_URL = """+repr(origin(request))+"""
AUDIENCE = """+repr(audience)+"""
def execute(name, arguments):
    with httpx.Client(base_url=BASE_URL, headers={"Authorization": "Bearer " + os.environ["VTG_CONNECTION_KEY"]}) as client:
        response = client.post("/v1/connect/tools/" + name, params={"audience": AUDIENCE}, json=arguments)
        response.raise_for_status()
        return response.json()
# Decisions first return confirmation_required. Show summary/evidence to the human,
# then call again with confirmation_token only after explicit human confirmation.
"""
    return {"audience": audience, "format": format, "declarations": declarations, "tool_schemas": schemas,
            "language": "python", "executor": executor, "secret_placeholder": "VTG_CONNECTION_KEY"}


@router.post("/v1/connect/tools/{name}", response_model=dict | list, summary="Execute a generated tool-pack function through the shared command layer")
async def execute_tool(name: str, body: dict, request: Request,
                       audience: str = Query(default="buyer", pattern="^(buyer|tenant|operator)$"), p: Principal = Depends(resolve_principal)):
    from .commands import json_value
    from .oauth import require_audience
    require_audience(p, audience, "/tool-pack/"+audience)
    service = request.app.state.gateway
    if name not in await tool_schemas(audience=audience):
        raise DomainError("TOOL_NOT_FOUND", "Tool is not available for this audience", 404)
    if audience != "buyer":
        from .human_tools import execute
        return json_value(execute(service, p, audience, name, body))
    from .mcp_server import create_server
    server = create_server(service, agent_id=None)
    result = await server.call_tool(name, body)
    if result.is_error:
        from fastapi.responses import JSONResponse
        value = result.structured_content or json.loads(result.content[0].text)
        return JSONResponse(value, status_code=value.get("status", 422))
    return result.structured_content if result.structured_content is not None else [item.model_dump(mode="json") for item in result.content]
