"""Generate connection documentation from repository prose and registered MCP schemas."""
import asyncio
import html
import json
from types import SimpleNamespace

from fastapi import Request
from fastapi.responses import Response

from .config import Settings
from .discovery import BACKEND, cached, base_url


async def tool_schemas(server=None, *, audience="buyer"):
    if server is None:
        from .mcp_server import create_server
        # Tool registration and schema generation require no database or runtime keys.
        server = create_server(SimpleNamespace(settings=Settings()), audience=audience)
    return {tool.name: {"description": tool.description, "inputSchema": tool.input_schema,
                        "outputSchema": tool.output_schema} for tool in await server.list_tools()}


def tables(schemas):
    esc = html.escape
    tools = []
    fields = []
    for name, tool in schemas.items():
        tools.append(f'<tr><td><code>{esc(name)}</code></td><td>{esc(tool["description"])}</td></tr>')
        for direction in ("inputSchema", "outputSchema"):
            schema = tool[direction] or {}
            sections = [(name + "." + direction, schema), *[(name + "." + direction + "." + key, value)
                        for key, value in schema.get("$defs", {}).items()]]
            for section, definition in sections:
                for field, detail in definition.get("properties", {}).items():
                    required = "required" if field in definition.get("required", []) else "optional"
                    encoded = json.dumps(detail, sort_keys=True, ensure_ascii=False)
                    fields.append(f'<tr><td>{esc(section + "." + field)}</td><td>{required}</td><td><code>{esc(encoded)}</code></td></tr>')
    return '<table id="tools"><tr><th>Tool</th><th>Description</th></tr>' + ''.join(tools) + '</table>\n' + (
        '<table id="fields"><tr><th>Field</th><th>Presence</th><th>Schema</th></tr>' + ''.join(fields) + '</table>')


def generated(schemas):
    template = (BACKEND / "docs/developers.html.in").read_text()
    return template.replace("{{SCHEMA_TABLES}}", tables(schemas))


def build():
    schemas = asyncio.run(tool_schemas())
    directory = BACKEND / "static"
    directory.mkdir(exist_ok=True)
    (directory / "developers.html").write_text(generated(schemas))
    (directory / "tools.json").write_text(json.dumps(schemas, indent=2, ensure_ascii=False) + "\n")
    (directory / "llms.txt").write_text((BACKEND / "docs/llms.txt.in").read_text())


def install_routes(app):
    @app.get("/developers", response_class=Response, responses={200: {"content": {"text/html": {"schema": {"type": "string"}}}},
        304: {"description": "Representation unchanged; ETag matched."}}, summary="Connect your procurement agent")
    def developers(request: Request):
        content = (BACKEND / "static/developers.html").read_text()
        return cached(request, content.replace("{{BASE_URL}}", html.escape(base_url(request))), "text/html")

    @app.get("/developers/tools.json", response_class=Response, responses={200: {"content": {"application/json": {
        "schema": {"type": "object", "additionalProperties": {"type": "object", "required": ["description", "inputSchema", "outputSchema"],
            "properties": {"description": {"type": "string"}, "inputSchema": {"type": "object"}, "outputSchema": {"type": ["object", "null"]}}}}}}},
        304: {"description": "Representation unchanged; ETag matched."}}, summary="Generated MCP tool descriptions and input/output schemas")
    def schemas(request: Request):
        return cached(request, (BACKEND / "static/tools.json").read_text(), "application/json")

    @app.get("/llms.txt", response_class=Response, responses={200: {"content": {"text/plain": {"schema": {"type": "string"}}}},
        304: {"description": "Representation unchanged; ETag matched."}}, summary="Navigation for external LLM agents")
    def llms(request: Request):
        content = (BACKEND / "static/llms.txt").read_text()
        return cached(request, content.replace("{{BASE_URL}}", base_url(request)), "text/plain")
