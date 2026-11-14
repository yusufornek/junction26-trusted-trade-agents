"""Reference receiver: authenticate the signed notification before presenting actions.

The caller supplies JWKS obtained from its configured trusted gateway origin.
For HTTP push, also verify the Standard Webhooks headers before calling this.
"""
from datetime import datetime, timezone
from app.signing import verify_jws


def verify_notification(value, jwks, *, connection_id, tenant, now=None):
    payload = {k: v for k, v in value.items() if k != 'signature'}
    verify_jws(value['signature'], payload, jwks)
    if value['aud'] != connection_id or value['tenant'] != tenant:
        raise ValueError('Notification audience or tenant mismatch')
    if not value['resource'].startswith('luotsi://'):
        raise ValueError('Untrusted notification resource')
    if datetime.fromisoformat(value['expires_at']) <= (now or datetime.now(timezone.utc)):
        raise ValueError('Expired notification')
    return payload


def response_headers(value):
    """Pass these headers with a resolving REST/A2A call; MCP uses the same keys in _meta."""
    return {'X-Notification-Id': value['id'], 'X-Cause-Id': value['cause_id']}
