"""Spec 37: discovery through the real in-process Streamable HTTP transport."""
import asyncio
import json
from contextlib import asynccontextmanager

import httpx2
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.api import create_app
from app.discovery import BACKEND, EXTENSION, CARD_COMMIT, check_server
from app.mcp_server import create_server
from conftest import agent_headers


@asynccontextmanager
async def connected(gateway, *, agent="external", discover=False):
    app = create_app(gateway)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://localhost",
                                     headers=agent_headers(agent)) as http:
            card = None
            if discover:
                catalog = (await http.get("/.well-known/ai-catalog.json")).json()
                entry = next(e for e in catalog["entries"] if e["type"] == "application/mcp-server-card+json")
                response = await http.get(entry["url"], headers={"Accept": entry["type"]})
                assert response.headers["content-type"] == entry["type"]
                card = response.json()
            url = card["remotes"][0]["url"] if card else "http://localhost/mcp"
            async with streamable_http_client(url, http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    yield session, http, card


def test_ac_1_catalog_to_card_to_runtime_tools(gateway):
    async def run():
        async with connected(gateway, discover=True) as (session, _, card):
            initialized = await session.initialize()
            assert initialized.server_info.name == card["name"]
            assert initialized.server_info.version == card["version"]
            assert initialized.protocol_version in card["remotes"][0]["supportedProtocolVersions"]
            assert card["remotes"][0]["type"] == "streamable-http"
            assert "find_offers" in {t.name for t in (await session.list_tools()).tools}
    asyncio.run(run())
    # Startup must also fail if a card's runtime comparison drifts.
    server = create_server(gateway)
    server._lowlevel_server.version = "drift"
    with pytest.raises(RuntimeError, match="runtime identity"):
        check_server(server)
    check_disabled_mcp_discovery(gateway)


def check_disabled_mcp_discovery(gateway):
    with TestClient(create_app(gateway, serve_mcp=False)) as client:
        catalog = client.get("/.well-known/ai-catalog.json")
        assert catalog.status_code == 200
        entries = catalog.json()["entries"]
        assert len(entries) == 4
        assert all(entry["type"] != "application/mcp-server-card+json" for entry in entries)
        assert all("/mcp" not in entry["url"] for entry in entries)
        assert all(client.get(entry["url"]).status_code == 200 for entry in entries)
        assert client.get("/mcp/server-card").status_code == 404
        assert "/mcp/server-card" not in client.get("/openapi.json").json()["paths"]


def test_review_disabled_mcp_discovery_omits_transport(gateway):
    check_disabled_mcp_discovery(gateway)


def test_ac_2_validates_pinned_draft_schema(gateway):
    schema = json.loads((BACKEND / "vendor/mcp-server-card/schema.json").read_text())
    validator = Draft202012Validator({**schema, "$ref": "#/$defs/ServerCard"}, format_checker=FormatChecker())
    with TestClient(create_app(gateway)) as client:
        response = client.get("/mcp/server-card", headers={"Origin": "https://outside.example"})
        card = response.json()
        validator.validate(card)
        extension = card["_meta"][EXTENSION]
        assert extension["serverCardExtension"] == {"status": "draft", "commit": CARD_COMMIT}
        assert client.get("/v1/meta").json()["specifications"]["mcp_server_card"]["draft_commit"] == CARD_COMMIT
        assert response.headers["access-control-allow-origin"] == "*"
        assert response.headers["access-control-expose-headers"] == "ETag"
        assert response.headers["cache-control"] == "public, max-age=3600"
        preflight = client.options("/mcp/server-card", headers={"Origin": "https://outside.example",
            "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "If-None-Match"})
        assert preflight.status_code == 204
        assert "If-None-Match" in preflight.headers["access-control-allow-headers"]


def test_ac_3_conditional_get_returns_304(gateway):
    with TestClient(create_app(gateway)) as client:
        for path in ("/mcp/server-card", "/.well-known/ai-catalog.json"):
            first = client.get(path)
            for tag in (first.headers["etag"], "W/" + first.headers["etag"], '"older", ' + first.headers["etag"], "*"):
                cached = client.get(path, headers={"If-None-Match": tag})
                assert cached.status_code == 304 and not cached.content
                assert cached.headers["etag"] == first.headers["etag"]
                assert cached.headers["access-control-allow-origin"] == "*"
            assert client.get(path, headers={"If-None-Match": '"stale"'}).status_code == 200


def test_ac_4_all_catalog_links_resolve(gateway):
    with TestClient(create_app(gateway)) as client:
        response = client.get("/.well-known/ai-catalog.json")
        assert response.headers["content-type"] == "application/ai-catalog+json"
        catalog = response.json()
        assert catalog["specVersion"] == "1.0"
        expected = {"application/mcp-server-card+json": "/mcp/server-card", "application/a2a+json": "/.well-known/agent-card.json",
                    "application/vnd.oai.openapi+json": "/openapi.json", "text/plain": "/llms.txt", "application/jwk-set+json": "/.well-known/jwks.json"}
        from urllib.parse import urlsplit
        assert {e["type"]: urlsplit(e["url"]).path for e in catalog["entries"]} == expected
        assert len(catalog["entries"]) == 5
        assert client.get(expected["application/vnd.oai.openapi+json"]).json()["openapi"] == "3.1.0"
        assert client.get(expected["application/a2a+json"]).json()["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"
        assert "keys" in client.get(expected["application/jwk-set+json"]).json()
        assert "Developer" in client.get(expected["text/plain"]).text
        for entry in catalog["entries"]:
            assert entry["identifier"].startswith("urn:air:") and "data" not in entry
            assert client.get(entry["url"]).status_code == 200
        card = client.get("/mcp/server-card").json()
        extension = card["_meta"][EXTENSION]
        for field in ("jwksUrl", "policyDocumentationUrl"):
            assert client.get(extension[field]).status_code == 200


def test_ac_5_public_metadata_has_no_customer_authority(gateway):
    with TestClient(create_app(gateway)) as client:
        for path in ("/.well-known/ai-catalog.json", "/mcp/server-card", "/v1/meta"):
            public = client.get(path)
            # Spec 60 R-12: authenticated discovery is computed per audience and tenant (private, no-store), so only
            # the unauthenticated documents must be identical; neither may carry customer authority (checked below).
            if path != "/mcp/server-card":
                assert public.content == client.get(path, headers=agent_headers("external")).content
                assert public.content == client.get(path, headers=agent_headers("other")).content
            data = public.json()
            # Spec 60 R-6: the public card lists tool, resource and prompt schemas; their property names are schema,
            # not customer authority, so the check covers everything outside those published schemas.
            for ext in (data.get("_meta") or {}).values():
                if isinstance(ext, dict):
                    for listed in ("tools", "resources", "resourceTemplates", "prompts", "capabilities"):
                        ext.pop(listed, None)
            def keys(value):
                if isinstance(value, dict):
                    return set(value) | set().union(*(keys(v) for v in value.values()))
                if isinstance(value, list):
                    return set().union(*(keys(v) for v in value))
                return set()
            assert not {"customer_ids", "principal", "remaining_budget", "mandate", "tools"} & keys(data)
        assert client.get("/v1/me").status_code == 401
        headers = {"Accept": "application/json, text/event-stream"}
        assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=headers).status_code == 401


@pytest.mark.parametrize("drift", ["transport", "endpoint"])
def test_review_startup_validates_constructed_transport(gateway, monkeypatch, drift):
    if drift == "transport":
        monkeypatch.setattr("app.discovery.TRANSPORT", "sse")
    else:
        from app.mcp_server import GatewayMCPServer
        original = GatewayMCPServer.streamable_http_app
        def different(self, **kwargs):
            return original(self, streamable_http_path="/different-mcp", **kwargs)
        monkeypatch.setattr(GatewayMCPServer, "streamable_http_app", different)
    with pytest.raises(RuntimeError, match="runtime transport"):
        with TestClient(create_app(gateway)):
            pass
