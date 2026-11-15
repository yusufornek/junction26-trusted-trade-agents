"""Live emission checks using SDK handlers and ASGI cancellation, never JSON-RPC."""
import asyncio
from contextlib import suppress
from dataclasses import replace

from starlette.responses import JSONResponse

from .auth import Principal
from .connection_trust_lifecycle import CONTEXT, READ_OPERATION, context
from .db import DomainError


class ContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        path = scope.get("path", "")
        # OAuth issuance/refresh is a control protocol of its own. Its context
        # remains stable while the corresponding resource is MCP or A2A.
        protocol = "oauth" if path.startswith("/oauth") else "mcp" if path.startswith("/mcp") else "a2a" if path.startswith("/a2a") else "rest"
        token = CONTEXT.set(context((scope.get("client") or ("unknown",))[0], protocol,
                                    headers.get(b"a2a-client", b"").decode("latin1") if protocol == "a2a" else ""))
        read_token = READ_OPERATION.set(scope.get("method") in {"GET", "HEAD", "OPTIONS"} or
            path in {"/v1/offers", "/v1/basket-lookups"})
        try:
            await self.app(scope, receive, send)
        finally:
            CONTEXT.reset(token)
            READ_OPERATION.reset(read_token)


async def live_http(app, gateway, p, scope, receive, send):
    """End idle HTTP streams too; auth on admission alone cannot do this."""
    trust = gateway.connection_trust
    started = ended = False
    streaming = scope.get("method") == "GET"
    with gateway.db.connect() as conn:
        epoch = trust.epoch(conn)

    def check():
        trust.check(p)
        trust.heartbeat(p)
        with gateway.db.connect() as conn:
            if streaming and trust.epoch(conn) != epoch:
                raise DomainError("POLICY_CHANGED", "Policy changed; reconnect the stream", 409)

    async def guarded_send(message):
        nonlocal started, ended, streaming
        if message["type"] == "http.response.start":
            streaming |= any(key.lower() == b"content-type" and b"text/event-stream" in value.lower()
                             for key, value in message.get("headers", []))
        check()
        started |= message["type"] == "http.response.start"
        ended |= message["type"] == "http.response.body" and not message.get("more_body", False)
        await send(message)

    task = asyncio.create_task(app(scope, receive, guarded_send))
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=.2)
            if not task.done():
                check()
        await task
    except DomainError as error:
        task.cancel()
        with suppress(asyncio.CancelledError, DomainError):
            await task
        if not started:
            from .recovery import problem
            await JSONResponse(problem(error), status_code=error.status)(scope, receive, send)
        elif not ended:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def listen(server, ctx, params):
    """Each connection gets its own native SDK handler and graceful close handle."""
    from mcp.server.subscriptions import ListenHandler
    from mcp.shared.subscriptions import SUBSCRIPTION_ID_META_KEY
    from .agent_surface import principal
    from mcp import types as mt
    trust, p = server.gateway.connection_trust, principal(server)
    trust.check(p)
    with server.gateway.db.connect() as conn:
        epoch = trust.epoch(conn)

    def check():
        trust.check(p)
        trust.heartbeat(p)
        with server.gateway.db.connect() as conn:
            if epoch != trust.epoch(conn):
                raise DomainError("POLICY_CHANGED", "Policy changed; reconnect the subscription", 409)
    handler = ListenHandler(server._subscriptions)
    original_session = ctx.session

    class GuardedSession:
        def __getattr__(self, name):
            return getattr(original_session, name)

        async def send_notification(self, *args, **kwargs):
            check()
            return await original_session.send_notification(*args, **kwargs)

    guarded_ctx = replace(ctx, session=GuardedSession())
    task = asyncio.create_task(handler(guarded_ctx, params))
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=.2)
            if not task.done():
                try:
                    check()
                except DomainError:
                    handler.close()
                    task.cancel()  # Do not drain buffered updates after withdrawal.
                    with suppress(asyncio.CancelledError):
                        await task
                    return mt.SubscriptionsListenResult(meta={SUBSCRIPTION_ID_META_KEY: ctx.request_id})
        return await task
    except DomainError:
        return mt.SubscriptionsListenResult(meta={SUBSCRIPTION_ID_META_KEY: ctx.request_id})
    finally:
        handler.close()
        task.cancel()
        with suppress(asyncio.CancelledError, DomainError):
            await task


def dispatch_authority(gateway, conn, order, credential):
    credential = credential or (order.context or {}).get("connection_authority")
    if credential:
        p = Principal(order.agent_id, **{k: v for k, v in credential.items() if k in Principal.__dataclass_fields__ and k != "agent_id"})
    else:
        p = Principal(order.agent_id)
    gateway.connection_trust.require_order(p, conn)


def push_authority(gateway, conn, sub):
    if sub.get("connection_id"):
        row = conn.execute("SELECT * FROM main.connections WHERE connection_id=?", (sub["connection_id"],)).fetchone()
        if not row:
            raise DomainError("UNAUTHENTICATED", "Push connection was removed", 401)
        from .agent_surface import surface
        p = surface(gateway).connection_principal(row)
    else:
        p = Principal(sub["owner"], kind=sub["owner_kind"])
    gateway.connection_trust.check(p, conn)
