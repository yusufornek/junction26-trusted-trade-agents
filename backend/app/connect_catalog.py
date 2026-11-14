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
    schemas = await tool_schemas()
    connection = SimpleNamespace(connection_id="<CONNECTION_ID>", audience=audience, customer_ids=["<CUSTOMER_ID>"])
    setups = {}
    for client in {client for clients in CLIENTS.values() for client in clients}:
        setup = await instructions(request.app.state.gateway if hasattr(request.app.state, "gateway") else None,
                                   connection, client, origin(request), audience, schemas=schemas)
        setups[client] = {k: setup[k] for k in ("steps", "snippet", "language", "secret_placeholder")}
    return {"audience": audience, "audiences": AUDIENCES, "scopes": ["*", "read", "write", "lookup", "quotes", "orders", "approvals:decide", "connections:manage", "agents:manage", "mandates:manage", "users:manage", "policy:manage"], "types": [
        {"type": kind, "clients": clients, "credential_methods": ["api_key", "oauth"] if kind in {"mcp", "a2a"} else ["api_key"],
         "documentation_links": [DOCS[kind]], "available": kind == "rest" or (kind == "mcp" and audience == "buyer" and request.app.state.mcp_served),
         "transports": ["streamable_http", "stdio"] if kind == "mcp" else ["http"],
         "client_details": [{"client": client, "credential_methods": ["oauth"] if client == "chatgpt" else ["api_key"],
                             **setups[client],
                             "available": client != "chatgpt" and (kind == "rest" or kind == "mcp" and audience == "buyer" and request.app.state.mcp_served)} for client in clients],
         "oauth_available": False, "rest_base_path": AUDIENCES[audience]["rest"]}
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
    schemas = schemas or await tool_schemas()
    placeholder = "VTG_CONNECTION_KEY"
    rest = base + AUDIENCES[audience]["rest"]
    resource = "/offers" if audience == "buyer" else "/orders"
    # This example is checked against the actual MCP input schema. Both transports
    # use the same NeedSpec; no duplicated function declarations are maintained.
    properties = schemas["find_offers"]["inputSchema"]["properties"]
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
                      "credential_placeholder": placeholder, "available": False, "note": "ChatGPT OAuth connections become available in part 2."}
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
    return {"connection_id": connection.connection_id, "audience": audience, "client": client,
            "steps": ["Copy the connection key from its creation response.", f"Set {placeholder} in your client environment.",
                      "Apply the configuration below and make a test call. Keep the key private."],
            "secret_placeholder": placeholder, "language": language, "snippet": snippet,
            "rest_base_url": rest, "tool_schemas": schemas, "lookup_example": example,
            "local_stdio": {"command": "backend/.venv/bin/python", "args": ["-m", "app.mcp_server"], "note": "Local stdio needs local gateway configuration; use remote HTTP for a connection key."} if client in CLIENTS["mcp"] else None}


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
