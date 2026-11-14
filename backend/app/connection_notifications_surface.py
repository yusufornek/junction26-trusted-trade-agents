"""REST and native SDK adapters for the connection notification capability."""
import asyncio
import json
from fastapi import APIRouter, Depends, Query, Request
from starlette.responses import JSONResponse

from .auth import Principal, resolve_principal
from .db import DomainError
from .connection_notifications_models import NotificationAction, NotificationPreferences

router = APIRouter(tags=['connection notifications'])


# Keep compatibility aliases callable; document the audience-specific routes.
@router.get('/v1/connections/{connection_id}/notifications', include_in_schema=False)
@router.get('/v1/tenant/connections/{connection_id}/notifications')
@router.get('/v1/operator/connections/{connection_id}/notifications')
def get_connection_notifications(connection_id: str, request: Request, p: Principal = Depends(resolve_principal)) -> dict:
    return request.app.state.gateway.notifications.preferences(connection_id, p)


@router.put('/v1/connections/{connection_id}/notifications', include_in_schema=False)
@router.put('/v1/tenant/connections/{connection_id}/notifications')
@router.put('/v1/operator/connections/{connection_id}/notifications')
def put_connection_notifications(connection_id: str, body: NotificationPreferences, request: Request,
                                 p: Principal = Depends(resolve_principal)) -> dict:
    return request.app.state.gateway.notifications.configure(connection_id, body, p)


@router.get('/v1/notifications', include_in_schema=False)
@router.get('/v1/connect/notifications')
@router.get('/v1/tenant/notifications')
@router.get('/v1/operator/notifications')
def get_notifications(request: Request, cursor: str = '0', limit: int = Query(default=50, ge=1, le=100),
                      mark_read: bool = False, p: Principal = Depends(resolve_principal)) -> dict:
    """Read the connection's signed inbox; next_cursor resumes after these items. mark_read acknowledges receipt only."""
    return request.app.state.gateway.notifications.inbox(p, cursor, limit, mark_read)


@router.post('/v1/notifications/{notification_id}/actions', include_in_schema=False)
@router.post('/v1/connect/notifications/{notification_id}/actions')
@router.post('/v1/tenant/notifications/{notification_id}/actions')
@router.post('/v1/operator/notifications/{notification_id}/actions')
def notification_action(notification_id: str, body: NotificationAction, request: Request,
                        p: Principal = Depends(resolve_principal)) -> dict:
    """Acknowledge, exclusively claim or resolve an action-needed event; ownership does not grant purchasing authority."""
    return request.app.state.gateway.notifications.act(notification_id, body, p)


def install(app):
    app.include_router(router)
    app.add_middleware(ResponseContext)


class ResponseContext:
    """Propagate verified notification lineage on REST and A2A requests."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get('headers', []))
        if scope['type'] != 'http' or scope.get('method') in {'GET', 'HEAD', 'OPTIONS'} or not (headers.get(b'x-notification-id') or headers.get(b'x-cause-id')):
            return await self.app(scope, receive, send)
        from .auth import resolve_credential
        from .recovery import problem
        gateway = scope['app'].state.gateway
        scheme, _, credential = headers.get(b'authorization', b'').decode().partition(' ')
        try:
            p = resolve_credential(scope['app'].state.keys, credential if scheme.lower() == 'bearer' else '', gateway)
            with gateway.notifications.response(p, notification_id=headers.get(b'x-notification-id', b'').decode() or None,
                                                cause_id=headers.get(b'x-cause-id', b'').decode() or None):
                await self.app(scope, receive, send)
        except DomainError as exc:
            await JSONResponse(problem(exc), status_code=exc.status)(scope, receive, send)


def register(server):
    if not hasattr(server.gateway, 'notifications'):
        return
    class StreamSession:
        async def __call__(self, ctx, call_next):
            from .agent_surface import principal
            p = principal(server)
            if not p.connection_id:
                return await call_next(ctx)
            from mcp.server.session import MODERN_PROTOCOL_VERSIONS
            if ctx.protocol_version in MODERN_PROTOCOL_VERSIONS and ctx.method != 'subscriptions/listen':
                return await call_next(ctx)
            entry = (asyncio.get_running_loop(), server, p, ctx.session)
            entries = server.gateway.notifications.streams
            # The SDK constructs a new ServerSession facade for each request.
            # Its Connection is stable for the lifetime of the actual stream.
            entries[:] = [e for e in entries if e[3]._connection is not ctx.session._connection]
            entries.append(entry)
            try:
                return await call_next(ctx)
            finally:
                # Modern subscription sessions live only for the listen request.
                if ctx.method == 'subscriptions/listen' and entry in entries:
                    entries.remove(entry)
    server._lowlevel_server.middleware.append(StreamSession())


async def boundary(server, name, arguments, context, next_call):
    from .agent_surface import principal
    from .recovery import problem
    from mcp.types import CallToolResult, TextContent
    metadata = getattr(context, 'meta', None) if context is not None else None
    if metadata is None and context is not None:
        try:
            metadata = context.request_context.meta
        except (ValueError, AttributeError):
            pass
    if hasattr(metadata, 'model_dump'):
        metadata = metadata.model_dump()
    metadata = metadata if isinstance(metadata, dict) else {}
    notification_id, cause_id = metadata.get('notification_id'), metadata.get('cause_id')
    if not notification_id and not cause_id:
        return await next_call(server, name, arguments, context)
    try:
        with server.gateway.notifications.response(principal(server), notification_id=notification_id, cause_id=cause_id):
            return await next_call(server, name, arguments, context)
    except DomainError as exc:
        value = problem(exc)
        return CallToolResult(is_error=True, content=[TextContent(type='text', text=json.dumps(value))], structured_content=value)


def inbox(gateway, p, mandate):
    from .auth import check_scope
    result = {'notifications': gateway.notifications.inbox(p)['notifications']}
    readers = {'proposals': ('proposals', lambda: gateway.proposals.inbox(p, mandate)),
               'stockout_alerts': ('products', lambda: gateway.stockout_watch.inbox(p)),
               'suggestions': ('suggestions', lambda: gateway.memory.suggestions(p))}
    for key, (scope, read) in readers.items():
        try:
            check_scope(p, scope, 'read')
            result[key] = read()
        except DomainError:
            result[key] = []
    return result
