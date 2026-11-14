"""Public connection metadata; authority is deliberately absent from these documents."""
import hashlib
import json
from pathlib import Path

from fastapi import Request
from fastapi.responses import Response
from jsonschema import Draft202012Validator, FormatChecker
from mcp_types.version import SUPPORTED_PROTOCOL_VERSIONS

NAME = "fi.valioaimo/trusted-trade-agents"
TITLE = "Valio Aimo Trusted Trade Agents"
VERSION = "0.1.0"
DESCRIPTION = "Governed fixed-price procurement with human approval and signed simulated order receipts."
CARD_COMMIT = "526201bbc80231daa40ffcdecfc9da4e54e5dc93"
EXTENSION = "fi.valioaimo/gateway"
TRANSPORT = "streamable-http"
BACKEND = Path(__file__).resolve().parents[1]


def server_card(base):
    return {"$schema": "https://static.modelcontextprotocol.io/schemas/v1/server-card.schema.json",
            "name": NAME, "title": TITLE, "version": VERSION, "description": DESCRIPTION,
            "websiteUrl": base + "/developers",
            "remotes": [{"type": TRANSPORT, "url": base + "/mcp",
                         "supportedProtocolVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
                         "headers": [{"name": "Authorization", "description": "Issued agent Bearer credential",
                                      "isRequired": True, "isSecret": True}]}],
            "_meta": {EXTENSION: {"serverCardExtension": {"status": "draft", "commit": CARD_COMMIT},
                                  "jwksUrl": base + "/.well-known/jwks.json",
                                  "policyDocumentationUrl": base + "/developers#rules",
                                  "supportedEnvironments": ["live", "sandbox"]}}}


def check_server(server, card=None):
    """Fail startup on schema or runtime identity/transport drift (no network refs)."""
    card = card or server_card("http://localhost")
    schema = json.loads((BACKEND / "vendor/mcp-server-card/schema.json").read_text())
    Draft202012Validator({"$ref": "#/$defs/ServerCard", **schema}, format_checker=FormatChecker()).validate(card)
    runtime = server._lowlevel_server
    if (card["name"], card["version"], card.get("title"), card.get("description")) != (
            runtime.name, runtime.version, runtime.title, runtime.description):
        raise RuntimeError("MCP server card contradicts runtime identity")
    if [(remote["type"], remote["url"].rsplit("/", 1)[-1]) for remote in card["remotes"]] != [(TRANSPORT, "mcp")]:
        raise RuntimeError("MCP server card contradicts runtime transport")
    if card["remotes"][0]["supportedProtocolVersions"] != list(SUPPORTED_PROTOCOL_VERSIONS):
        raise RuntimeError("MCP server card contradicts runtime protocol support")


def base_url(request):
    return request.app.state.gateway.settings.public_url or str(request.base_url).rstrip("/")


def catalog(base, mcp_served=True):
    entries = [("mcp", "trusted-trade-agents", "application/mcp-server-card+json", "/mcp/server-card"),
               ("a2a", "procurement", "application/a2a+json", "/.well-known/agent-card.json"),
               ("openapi", "gateway", "application/vnd.oai.openapi+json", "/openapi.json"),
               ("text", "llms", "text/plain", "/llms.txt"),
               ("keys", "jwks", "application/jwk-set+json", "/.well-known/jwks.json")]
    return {"specVersion": "1.0", "entries": [
        {"identifier": f"urn:air:valioaimo.fi:{kind}:{name}", "type": mime, "url": base + path}
        for kind, name, mime, path in entries if mcp_served or kind != "mcp"]}


def cached(request, content, media_type):
    body = content.encode() if isinstance(content, str) else json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    etag = '"' + hashlib.sha256(body).hexdigest() + '"'
    headers = {"ETag": etag, "Cache-Control": "public, max-age=3600"}
    matches = [tag.strip().removeprefix("W/") for tag in request.headers.get("if-none-match", "").split(",")]
    return Response(status_code=304, headers=headers) if etag in matches or "*" in matches else Response(body, media_type=media_type, headers=headers)


def card_schema():
    """Inline the pinned definitions so OpenAPI refs resolve in its document context."""
    schema = json.loads((BACKEND / "vendor/mcp-server-card/schema.json").read_text())
    def expand(value):
        if isinstance(value, dict):
            if "$ref" in value:
                return expand({**schema["$defs"][value["$ref"].split("/")[-1]], **{k: v for k, v in value.items() if k != "$ref"}})
            return {k: expand(v) for k, v in value.items()}
        return [expand(v) for v in value] if isinstance(value, list) else value
    return expand(schema["$defs"]["ServerCard"])


def install_routes(app):
    def card(request: Request):
        return cached(request, server_card(base_url(request)), "application/mcp-server-card+json")

    if app.state.mcp_served:
        app.add_api_route("/mcp/server-card", card, response_class=Response, responses={
            200: {"content": {"application/mcp-server-card+json": {"schema": card_schema()}}},
            304: {"description": "Representation unchanged; ETag matched."}},
            summary="Draft MCP connection card; tools are listed at runtime")

    @app.get("/.well-known/ai-catalog.json", response_class=Response, responses={
        200: {"content": {"application/ai-catalog+json": {"schema": {"type": "object", "required": ["specVersion", "entries"],
            "properties": {"specVersion": {"const": "1.0"}, "entries": {"type": "array", "items": {
                "type": "object", "required": ["identifier", "type", "url"], "properties": {
                    "identifier": {"type": "string"}, "type": {"type": "string"}, "url": {"type": "string", "format": "uri"}}}}}}}}},
        304: {"description": "Representation unchanged; ETag matched."}},
             summary="Public MCP, A2A, OpenAPI, llms and verification-key discovery")
    def ai_catalog(request: Request):
        return cached(request, catalog(base_url(request), app.state.mcp_served), "application/ai-catalog+json")


class MCPTransport:
    """Forward the exact /mcp URI without Mount's slash redirect or path rewriting."""
    def __init__(self, audience="buyer"):
        self.audience = audience

    async def __call__(self, scope, receive, send):
        app = getattr(scope["app"].state, "mcp_http" if self.audience == "buyer" else "mcp_"+self.audience+"_http")
        await app({**scope, "path": "/mcp", "raw_path": b"/mcp"}, receive, send)
