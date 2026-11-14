"""Select SDK JSON or streaming transports without implementing MCP messages.

Ordinary calls keep their JSON response for existing clients. SDK subscription
streams and legacy duplex elicitation use the SDK's SSE/session manager instead.
"""
import json
import weakref
from contextlib import asynccontextmanager

from .auth import MCPBearerMiddleware


class RESTUsage:
    """Store only operation names/error codes, never bodies or credentials."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/v1/") or scope["path"].startswith("/v1/connect/tools/"):
            return await self.app(scope, receive, send)
        status, prefix = 200, bytearray()
        async def observe(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            elif status >= 400 and message["type"] == "http.response.body" and len(prefix) < 16384:
                prefix.extend(message.get("body", b"")[:16384 - len(prefix)])
            await send(message)
        await self.app(scope, receive, observe)
        p = scope.get("state", {}).get("principal")
        if p is None or not p.connection_id:
            return
        gateway = scope["app"].state.gateway
        from .agent_surface import surface
        from mcp import types as mt
        s = surface(gateway)
        route = scope.get("route")
        target = getattr(route, "path", scope["path"])
        name = next((c.name for c in s.capabilities(p.audience if p.audience in {"buyer", "tenant", "operator"} else "buyer")
                     if c.route.path == target and c.method == scope["method"]), None)
        if name:
            code = None
            if status >= 400:
                try:
                    code = json.loads(prefix).get("code", "HTTP_ERROR")
                except (ValueError, TypeError, AttributeError):
                    code = "HTTP_ERROR"
            s.note(p, name, mt.CallToolResult(is_error=bool(code), structured_content={"code": code}, content=[]))


class SessionPrincipal:
    """Stateful SDK sessions bind identity; every message gets live scope."""
    def __init__(self, server):
        self.server = server
        self.identities = weakref.WeakKeyDictionary()

    async def __call__(self, ctx, call_next):
        if ctx.request is None:
            return await call_next(ctx)
        from .auth import resolve_credential
        from .foundation import PRINCIPAL
        from .agent_surface import effective_audience, canonical_origin
        from .oauth import require_audience
        from mcp.shared.exceptions import MCPError
        scheme, _, secret = ctx.request.headers.get("authorization", "").partition(" ")
        base = self.server.gateway.settings.public_url or str(ctx.request.base_url).rstrip("/")
        p = resolve_credential(self.server.keys, secret if scheme.lower() == "bearer" else "", self.server.gateway,
                               resource=base + "/mcp")
        audience = effective_audience(p, self.server.audience)
        require_audience(p, audience, "/mcp" + ("" if audience == "buyer" else "/" + audience), base)
        identity = (p.kind, p.id, p.tenant_id, p.connection_id, audience)
        previous = self.identities.setdefault(ctx.session, identity)
        if previous != identity:
            raise MCPError(-32001, "An MCP session cannot be reused by a different connection")
        token = PRINCIPAL.set(p)
        from .mcp_server import PRESENTED_MANDATE
        mandate_token = PRESENTED_MANDATE.set(ctx.request.headers.get("agent-mandate"))
        try:
            return await call_next(ctx)
        finally:
            PRESENTED_MANDATE.reset(mandate_token)
            PRINCIPAL.reset(token)


class SDKTransportChoice:
    def __init__(self, app, streaming):
        self.app, self.streaming = app, streaming

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        if headers.get(b"mcp-method") == b"subscriptions/listen" or b"mcp-session-id" in headers:
            return await self.streaming(scope, receive, send)
        if scope.get("method") != "POST" or b"mcp-method" in headers:
            return await self.app(scope, receive, send)
        # Legacy clients have no MCP-Method header. Inspect only small handshake
        # bodies; leave oversized/streaming bodies to the SDK's own size guard.
        first = await receive()
        stream = False
        if first.get("type") == "http.request" and not first.get("more_body") and len(first.get("body", b"")) <= 65536:
            try:
                data = json.loads(first["body"])
                stream = data.get("method") == "initialize" and "elicitation" in data.get("params", {}).get("capabilities", {})
            except (ValueError, TypeError, AttributeError):
                pass
        used = False

        async def replay():
            nonlocal used
            if not used:
                used = True
                return first
            return await receive()
        await (self.streaming if stream else self.app)(scope, replay, send)


def install(server, app, kwargs):
    if not kwargs.get("json_response"):
        return app
    streaming = server._lowlevel_server.streamable_http_app(**{**kwargs, "json_response": False, "stateless_http": False})
    streaming.add_middleware(MCPBearerMiddleware, keys=server.keys, agent_id=server.agent_id,
                             gateway=server.gateway, audience=server.audience)
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original(application):
            async with streaming.router.lifespan_context(streaming):
                yield
    app.router.lifespan_context = lifespan
    app.add_middleware(SDKTransportChoice, streaming=streaming)
    return app
