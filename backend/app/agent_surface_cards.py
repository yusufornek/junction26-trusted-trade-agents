"""Signed discovery generated from the live SDK registry (spec 60)."""
import json

from fastapi import Request
from fastapi.responses import JSONResponse
from google.protobuf.json_format import MessageToDict, ParseDict

from .agent_surface import PRIVATE, canonical_origin, effective_audience
from .auth import admit_credential
from .foundation import PRINCIPAL

EXTENSION = "urn:luotsi:agent-surface:v1"


def example(schema, definitions=None, seen=()):
    definitions = schema.get("$defs", definitions or {})
    if "default" in schema and schema["default"] is not None:
        return schema["default"]
    if "$ref" in schema:
        key = schema["$ref"].split("/")[-1]
        return example(definitions[key], definitions, (*seen, key)) if key not in seen else {}
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    if "anyOf" in schema:
        return example(next((s for s in schema["anyOf"] if s.get("type") != "null"), schema["anyOf"][0]), definitions, seen)
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        return {k: example(v, definitions, seen) for k, v in schema.get("properties", {}).items() if k in schema.get("required", [])}
    if kind == "array":
        return [example(schema.get("items", {}), definitions, seen) for _ in range(max(1, schema.get("minItems", 0)))]
    if kind in {"number", "integer"}:
        return max(1, schema.get("minimum", 1), schema.get("exclusiveMinimum", 0) + 1)
    if kind == "boolean":
        return True
    if schema.get("format") == "date":
        return "2026-11-16"
    if schema.get("format") == "date-time":
        return "2026-11-16T10:00:00Z"
    return "<" + schema.get("title", "value") + ">"


async def inventory(gateway, audience):
    from .mcp_server import create_server
    server = create_server(gateway, agent_id=None, audience=audience)
    return server, await server.list_tools()


def discovery_context(gateway, audience):
    p = PRINCIPAL.get()
    result = {"audience": audience}
    # Hash policy rather than putting any editable tenant document in a public card.
    from hashlib import sha256
    from .canonical import canonical_json
    result["policy_version"] = sha256(canonical_json(gateway.policy._editable(gateway.policy.get()))).hexdigest()
    if p is not None:
        result.update(tenant_id=p.tenant_id, scopes=list(p.scopes))
        if p.kind == "agent":
            with gateway.db.connect() as conn:
                result["policy_version"] = gateway.governance.resolve(p.id, conn=conn).version_id
    return result


async def enrich_agent_card(card, gateway, audience):
    if gateway is None:
        return card
    base = canonical_origin(gateway)
    audience = effective_audience(PRINCIPAL.get(), audience)
    server, tools = await inventory(gateway, audience)
    aliases = {"quote_and_order": "create_quote", "order_status": "get_order_status", "get_evidence": "get_order_evidence"}
    existing = {skill.id: skill for skill in card.skills}
    del card.skills[:]
    schemas = {}
    from .connection_contract import manager
    toolmap = {tool.name: tool for tool in tools}
    for name in [*toolmap, *(name for name, target in aliases.items() if target in toolmap)]:
        tool = toolmap.get(name) or toolmap[aliases[name]]
        sample = {"version": 1, "command": "tool", "skill": tool.name, "arguments": example(tool.input_schema)}
        card.skills.add(id=name, name=tool.title or name, description=tool.description,
            tags=[audience, "procurement"], input_modes=["application/json", "text/plain"] if audience == "buyer" else ["application/json"], output_modes=["application/json"],
            examples=[json.dumps(sample)])
        schemas[name] = {"input": tool.input_schema, "output": tool.output_schema}
    card.documentation_url = base + "/developers"
    del card.default_input_modes[:]
    card.default_input_modes.extend(["application/json", "text/plain"] if audience == "buyer" else ["application/json"])
    card.capabilities.extended_agent_card = True
    card.provider.url = base
    card.supported_interfaces[0].url = base + "/a2a" + ("" if audience == "buyer" else "/" + audience)
    # Replace all extension schema registries with this audience's live inventory.
    del card.capabilities.extensions[:]
    card.capabilities.extensions.add(uri=EXTENSION, description="Live MCP skill schemas, canonical issuer and exact OAuth resources",
        params={"schemas": schemas, "canonicalOrigin": base, "discovery": discovery_context(gateway, audience),
                "oauthResource": card.supported_interfaces[0].url,
                "mcpResource": base + "/mcp" + ("" if audience == "buyer" else "/" + audience),
                "documentationLinks": [base + "/developers", base + "/openapi.json", base + "/.well-known/jwks.json"]})
    for uri, description in (
        ("urn:luotsi:signed-receipts:v1", "Detached EdDSA receipts; verify with independently trusted gateway JWKS"),
        ("urn:luotsi:verifiable-mandate:v1", "Buyer-issued verifiable mandate; gateway policy may only tighten authority"),
        ("https://www.standardwebhooks.com/", "Standard Webhooks v1a push with gateway JWKS")):
        card.capabilities.extensions.add(uri=uri, description=description, params={"jwksUrl": base + "/.well-known/jwks.json"})
    ParseDict({"oauth2SecurityScheme": {"oauth2MetadataUrl": base + "/.well-known/oauth-authorization-server",
        "flows": {"authorizationCode": {"authorizationUrl": base + "/oauth/authorize", "tokenUrl": base + "/oauth/token", "scopes": {"*": "Consented " + audience + " operations"}}}}}, card.security_schemes["OAuth2"])
    card.capabilities.extensions.add(uri="urn:luotsi:connection-contract:v1", description="Versioned trading profile, schemas and recovery vocabulary",
                                    params=manager(gateway).public())
    del card.signatures[:]
    value = MessageToDict(card)
    protected, _, signature = gateway.signing.sign(value).split(".")
    card.signatures.add(protected=protected, signature=signature)
    return card


async def mcp_card(gateway, audience="buyer"):
    from .discovery import server_card, EXTENSION as MCP_EXTENSION
    from .connection_contract import manager
    base = canonical_origin(gateway)
    audience = effective_audience(PRINCIPAL.get(), audience)
    server, tools = await inventory(gateway, audience)
    card = server_card(base)
    card["remotes"][0]["url"] = base + "/mcp" + ("" if audience == "buyer" else "/" + audience)
    if audience != "buyer":
        card["name"], card["title"] = server.name, server._lowlevel_server.title
    card["_meta"][MCP_EXTENSION].update({"canonicalOrigin": base,
        "contract": manager(gateway).public(),
        "discovery": discovery_context(gateway, audience),
        "tools": [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools],
        "resources": [r.model_dump(mode="json", by_alias=True, exclude_none=True) for r in await server.list_resources()],
        "resourceTemplates": [r.model_dump(mode="json", by_alias=True, exclude_none=True) for r in await server.list_resource_templates()],
        "prompts": [p.model_dump(mode="json", by_alias=True, exclude_none=True) for p in await server.list_prompts()],
        "capabilities": server._lowlevel_server.get_capabilities().model_dump(mode="json", by_alias=True, exclude_none=True),
        "security": {"bearer": True, "oauthResource": card["remotes"][0]["url"], "authorizationServer": base}})
    card["_meta"][MCP_EXTENSION]["signature"] = gateway.signing.sign(card)
    return card


async def card_response(request: Request, audience="buyer", *, mcp=False):
    from .oauth import require_audience
    gateway = request.app.state.gateway
    authenticated = bool(request.headers.get("authorization") or request.cookies.get("vtg_session"))
    auth_origin = gateway.settings.public_url or str(request.base_url).rstrip("/")
    token = PRINCIPAL.set(None)
    try:
        if request.headers.get("authorization"):
            scheme, _, secret = request.headers["authorization"].partition(" ")
            p = admit_credential(gateway, request.app.state.keys, request.client.host if request.client else "unknown", secret if scheme.lower() == "bearer" else "", observe=False)
            audience = effective_audience(p, audience)
            require_audience(p, audience, ("/mcp" if mcp else "/a2a") + ("" if audience == "buyer" else "/" + audience), auth_origin)
            from .auth import observe_credential
            observe_credential(gateway, p, request.client.host if request.client else "unknown")
            PRINCIPAL.set(p)
        elif request.cookies.get("vtg_session"):
            p = gateway.accounts.session(request.cookies["vtg_session"])
            audience = effective_audience(p, audience)
            require_audience(p, audience, ("/mcp" if mcp else "/a2a") + ("" if audience == "buyer" else "/" + audience), auth_origin)
            PRINCIPAL.set(p)
        if mcp:
            value = await mcp_card(gateway, audience)
            from .discovery import cached
            if not authenticated:
                return cached(request, value, "application/mcp-server-card+json")
        else:
            from .a2a import agent_card
            value = MessageToDict(await agent_card(gateway, canonical_origin(gateway), audience))
        return JSONResponse(value, media_type="application/mcp-server-card+json" if mcp else "application/json",
            headers={"Cache-Control": PRIVATE if authenticated else "public, max-age=3600", "Vary": "Authorization"})
    finally:
        PRINCIPAL.reset(token)
