"""OAuth 2.1 code/PKCE grants bound to a human, connection and exact resource.

Only token/code digests are durable. All redemption and rotation operations use
one SQLite writer transaction; refresh replay invalidates every token in a family.
"""
import base64
import asyncio
import html
import ipaddress
import json
import os
import re
from dataclasses import replace
from datetime import datetime, timedelta
from secrets import compare_digest, token_urlsafe
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.routes import create_protected_resource_routes
from pydantic import AnyHttpUrl, BaseModel, Field

from . import audit
from .accounts import digest, origin
from .auth import Principal, resolve_principal, session_cookie, admit_credential
from .db import DomainError
from .models import ConnectionRequest
from .foundation import PRINCIPAL

router = APIRouter(tags=["oauth"])
RESOURCE_PATHS = {"/" + transport + ("" if audience == "buyer" else "/" + audience): audience
                  for transport in ("mcp", "a2a") for audience in ("buyer", "tenant", "operator")}
SCOPES = ["*", "read", "write", "lookup", "quotes", "orders", "orders:cancel", "approvals:decide",
          "connections:manage", "agents:manage", "mandates:manage", "users:manage", "policy:manage", "offline_access"]
OAUTH_ERRORS = {400: {"description": "OAuth protocol error", "content": {"application/json": {"schema": {
    "type": "object", "required": ["error", "error_description"],
    "properties": {"error": {"type": "string"}, "error_description": {"type": "string"}}}}}}}


class OAuthError(Exception):
    def __init__(self, code, description="OAuth request refused", status=400):
        self.code, self.description, self.status = code, description, status


def require_audience(p, audience, resource_path, resource_origin=None):
    if p.resource and (urlsplit(p.resource).path != resource_path or resource_origin is not None and p.resource != resource_origin+resource_path):
        raise DomainError("WRONG_AUDIENCE", "OAuth token is bound to another resource", 401)
    if audience != "buyer" and p.kind == "agent":
        raise DomainError("HUMAN_REQUIRED", "This audience requires a human account", 403)
    if p.connection_id and p.audience != audience:
        raise DomainError("WRONG_AUDIENCE", "Credential is bound to another audience", 401)
    if audience == "operator" and not p.operator:
        raise DomainError("OPERATOR_REQUIRED", "Platform administrator required", 403)
    if audience == "tenant" and (p.operator or not p.user_id):
        raise DomainError("HUMAN_REQUIRED", "A tenant human is required", 403)


def redirect_to(uri, **params):
    parts = urlsplit(uri)
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                      urlencode([*parse_qsl(parts.query), *[(k, v) for k, v in params.items() if v is not None]]), ""))


def secure_origin(request):
    base = origin(request)
    url = urlsplit(base)
    if url.scheme != "https" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise OAuthError("invalid_request", "OAuth requires HTTPS outside loopback development")
    return base


def client_metadata(data, *, dynamic=False):
    if not isinstance(data, dict):
        raise OAuthError("invalid_client_metadata")
    application = data.get("application_type")
    if dynamic and application not in {"native", "web"}:
        raise OAuthError("invalid_client_metadata", "application_type (native or web) is required")
    if data.get("token_endpoint_auth_method", "none") != "none":
        raise OAuthError("invalid_client_metadata", "Public PKCE clients use token_endpoint_auth_method=none")
    uris = data.get("redirect_uris")
    if not isinstance(uris, list) or not uris or len(uris) > 20:
        raise OAuthError("invalid_redirect_uri")
    for uri in uris:
        if not isinstance(uri, str) or len(uri) > 2048 or any(c.isspace() or ord(c) < 32 for c in uri):
            raise OAuthError("invalid_redirect_uri")
        try:
            u = urlsplit(uri)
            _ = u.port
        except ValueError:
            raise OAuthError("invalid_redirect_uri") from None
        loopback = u.hostname in {"localhost", "127.0.0.1", "::1"}
        if not u.hostname or u.username or u.password or u.fragment or not (u.scheme == "https" or u.scheme == "http" and loopback):
            raise OAuthError("invalid_redirect_uri")
        if application == "web" and u.scheme != "https":
            raise OAuthError("invalid_redirect_uri", "Web clients require HTTPS redirects")
    name = data.get("client_name")
    if not isinstance(name, str) or not 1 <= len(name) <= 200:
        raise OAuthError("invalid_client_metadata", "client_name is required")
    return {"client_name": name, "redirect_uris": uris, "application_type": application or "web",
            "token_endpoint_auth_method": "none"}


class OAuth(TokenVerifier):
    def __init__(self, gateway):
        self.gateway, self.db = gateway, gateway.db
        self._form_key = token_urlsafe(32)

    def form_csrf(self, p, request_id):
        import hmac
        return hmac.new(self._form_key.encode(), (p.user_id+":"+request_id).encode(), "sha256").hexdigest()

    def register(self, client_id, metadata, *, dynamic=False):
        """Local deployment pre-registration uses this same validated registry."""
        value = {**client_metadata(metadata, dynamic=dynamic), "client_id": client_id}
        with self.db.connect(write=True) as conn:
            conn.execute("INSERT INTO audience_oauth_clients VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data", (client_id, json.dumps(value)))
        return value

    async def client(self, client_id):
        with self.db.connect() as conn:
            row = conn.execute("SELECT data FROM audience_oauth_clients WHERE id=?", (client_id,)).fetchone()
        if row:
            return json.loads(row[0])
        if len(client_id) > 2048 or any(c.isspace() or ord(c) < 32 for c in client_id):
            raise OAuthError("invalid_client")
        try:
            u = urlsplit(client_id)
            _ = u.port
        except ValueError:
            raise OAuthError("invalid_client") from None
        if u.scheme != "https" or not u.hostname or not u.path or u.path == "/" or u.username or u.password or u.fragment or u.query:
            raise OAuthError("invalid_client", "Unknown client; register or use an HTTPS client metadata URL")
        # Reject obvious local targets and redirects. HTTPX has a hard response
        # deadline and the streamed document is capped before JSON parsing.
        if u.hostname.lower() == "localhost" or u.hostname.lower().endswith((".localhost", ".local", ".internal")):
            raise OAuthError("invalid_client")
        try:
            if not ipaddress.ip_address(u.hostname).is_global:
                raise OAuthError("invalid_client")
        except ValueError:
            pass
        try:
            async with asyncio.timeout(5):
                async with httpx.AsyncClient(timeout=httpx.Timeout(5), follow_redirects=False, trust_env=False) as client:
                    async with client.stream("GET", client_id, headers={"Accept": "application/json"}) as response:
                        if response.status_code != 200:
                            raise OAuthError("invalid_client", "Client metadata unavailable")
                        content = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=8192):
                            content.extend(chunk)
                            if len(content) > 65536:
                                raise OAuthError("invalid_client", "Client metadata too large")
            data = json.loads(content)
            if data.get("client_id") != client_id:
                raise OAuthError("invalid_client", "Client metadata id must match its URL exactly")
            return {**client_metadata(data), "client_id": client_id}
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError, AttributeError):
            raise OAuthError("invalid_client", "Client metadata is unavailable or invalid") from None

    def request(self, request_id, p):
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM audience_oauth_requests WHERE id=? AND user_id=?", (request_id, p.user_id)).fetchone()
        if not row:
            raise DomainError("OAUTH_REQUEST_NOT_FOUND", "Consent request was not found", 404)
        if row["used_at"] or row["expires_at"] <= audit.utcnow().isoformat():
            raise DomainError("OAUTH_REQUEST_EXPIRED", "Start authorization again", 410)
        return json.loads(row["data"])

    def decide(self, request_id, p, body):
        with self.db.connect(write=True) as conn:
            data = self.request(request_id, p)
            audience = RESOURCE_PATHS[urlsplit(data["resource"]).path]
            if audience == "operator" and not p.operator or audience != "operator" and p.operator:
                raise DomainError("HUMAN_REQUIRED", "Sign in to the requested audience", 403)
            if not body.approve:
                conn.execute("UPDATE audience_oauth_requests SET used_at=? WHERE id=?", (audit.utcnow().isoformat(), request_id))
                audit.append(conn, p.user_id, "CONSENT_DENIED", "Denied OAuth consent.", data={"tenant_id": p.tenant_id or "platform", "client_name": data["client"]["client_name"]})
                return {"redirect_url": redirect_to(data["redirect_uri"], error="access_denied", state=data.get("state"), iss=data["issuer"])}
            scopes = body.scopes if body.scopes is not None else data["scope"].split()
            if not scopes or not set(scopes) <= set(data["scope"].split()):
                raise DomainError("INSUFFICIENT_SCOPE", "Consent can only narrow requested scopes", 403)
            customers = body.customer_ids if body.customer_ids is not None else list(p.customer_ids)
            if not p.operator and (not customers or not set(customers) <= set(p.customer_ids)):
                raise DomainError("CUSTOMER_NOT_IN_MANDATE", "Consent customers exceed tenant scope", 403)
            agent = body.agent_id
            if audience == "buyer":
                self.gateway.accounts.manage(p)
                if not agent:
                    raise DomainError("VALIDATION_ERROR", "Choose an existing agent or supply a new agent id", 422)
                with self.db.connect() as read:
                    known = read.execute("SELECT tenant_id,disabled_at FROM tenant_agents WHERE agent_id=?", (agent,)).fetchone()
                if known and (known[0] != p.tenant_id or known[1]):
                    raise DomainError("AGENT_NOT_FOUND", "Agent was not found", 404)
            else:
                agent = "oauth-human-" + token_urlsafe(8).lower().replace("_", "-")
            # Human OAuth consent is also available to approvers and viewers.
            # The shared connection creator enforces tenant/customer authority;
            # its elevated admin projection exists only for this issuance step.
            issuer = replace(p, role="tenant_admin") if audience == "tenant" else p
            if audience == "operator" and not customers:
                with self.db.connect() as read:
                    customers = [r[0] for r in read.execute("SELECT customer_id FROM tenant_customers")]
            connection = self.gateway.connections.create(ConnectionRequest(agent_id=agent, client="mcp_other" if "/mcp" in data["resource"] else "a2a",
                type="mcp" if "/mcp" in data["resource"] else "a2a", audience=audience, customer_ids=customers,
                scopes=[s for s in scopes if s != "offline_access"], label=data["client"]["client_name"]), issuer, data["issuer"], True)
            # Throw away the API key: an OAuth connection is usable only through
            # its grants. Connection revoke invalidates those grants next request.
            conn.execute("UPDATE connections SET type='oauth',credential_kind='oauth',key_digest=?,key_prefix='oauth' WHERE connection_id=?", (digest(token_urlsafe(48)), connection.connection_id))
            code = "vtg_code_" + token_urlsafe(32)
            grant = {"client_id": data["client"]["client_id"], "resource": data["resource"], "redirect_uri": data["redirect_uri"],
                     "challenge": data["challenge"], "user_id": p.user_id, "tenant_id": p.tenant_id or "platform", "scope": " ".join(scopes)}
            conn.execute("INSERT INTO audience_oauth_codes VALUES(?,?,?,?,NULL)", (digest(code), connection.connection_id, json.dumps(grant), (audit.utcnow()+timedelta(minutes=5)).isoformat()))
            conn.execute("UPDATE audience_oauth_requests SET used_at=? WHERE id=?", (audit.utcnow().isoformat(), request_id))
            audit.append(conn, p.user_id, "CONSENT_APPROVED", "Approved OAuth connection.", data={"tenant_id": p.tenant_id or "platform", "connection_id": connection.connection_id, "audience": audience})
            return {"connection_id": connection.connection_id, "redirect_url": redirect_to(data["redirect_uri"], code=code, state=data.get("state"), iss=data["issuer"])}

    def _connection(self, connection_id, data):
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM connections WHERE connection_id=?", (connection_id,)).fetchone()
        if not row or row["revoked_at"] or row["expires_at"] and row["expires_at"] <= audit.utcnow().isoformat():
            raise OAuthError("invalid_grant", "Connection expired or revoked; authorize again")
        self.gateway.accounts.user(data["user_id"], active=True)
        p = Principal(row["agent_id"], tenant_id=row["tenant_id"], connection_id=connection_id,
            credential_ref=connection_id, customer_ids=tuple(json.loads(row["customer_ids"])), scopes=tuple(json.loads(row["scopes"])),
            audience=row["audience"], user_id=data["user_id"] if row["audience"] != "buyer" else None, resource=data["resource"])
        return self.gateway.accounts.bind(p)

    def _issue(self, conn, connection_id, data, family):
        self._connection(connection_id, data)
        now = audit.utcnow()
        result = {"token_type": "Bearer", "expires_in": 3600, "scope": data["scope"]}
        for kind, life in [("access", timedelta(hours=1)), ("refresh", timedelta(days=30))]:
            secret = "vtg_oauth_" + kind + "_" + token_urlsafe(32)
            conn.execute("INSERT INTO audience_oauth_tokens VALUES(?,?,?,?,?,?,NULL,NULL)",
                (digest(secret), kind, family, connection_id, json.dumps(data), (now+life).isoformat()))
            result[kind + "_token"] = secret
        conn.execute("UPDATE connections SET last_used_at=? WHERE connection_id=?", (now.isoformat(), connection_id))
        return result

    def token(self, form):
        failure = None
        with self.db.connect(write=True) as conn:
            if form.get("grant_type") == "authorization_code":
                row = conn.execute("SELECT * FROM audience_oauth_codes WHERE digest=?", (digest(form.get("code", "")),)).fetchone()
                if not row or row["used_at"] or row["expires_at"] <= audit.utcnow().isoformat():
                    raise OAuthError("invalid_grant")
                data = json.loads(row["data"])
                verifier = form.get("code_verifier", "")
                challenge = base64.urlsafe_b64encode(__import__('hashlib').sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                if not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier) or not compare_digest(challenge, data["challenge"]) or any(form.get(k) != data[k] for k in ("client_id", "resource", "redirect_uri")):
                    raise OAuthError("invalid_grant", "PKCE, redirect, client or resource mismatch")
                conn.execute("UPDATE audience_oauth_codes SET used_at=? WHERE digest=?", (audit.utcnow().isoformat(), row["digest"]))
                return self._issue(conn, row["connection_id"], data, token_urlsafe(24))
            if form.get("grant_type") != "refresh_token":
                raise OAuthError("unsupported_grant_type")
            row = conn.execute("SELECT * FROM audience_oauth_tokens WHERE digest=? AND kind='refresh'", (digest(form.get("refresh_token", "")),)).fetchone()
            if not row:
                raise OAuthError("invalid_grant")
            data = json.loads(row["data"])
            if any(form.get(k) != data[k] for k in ("client_id", "resource")):
                raise OAuthError("invalid_grant")
            if row["used_at"]:
                conn.execute("UPDATE audience_oauth_tokens SET revoked_at=? WHERE family=?", (audit.utcnow().isoformat(), row["family"]))
                audit.append(conn, data["user_id"], "TOKEN_FAMILY_REVOKED", "Refresh reuse revoked OAuth token family.", data={"connection_id": row["connection_id"], "tenant_id": data.get("tenant_id")})
                failure = OAuthError("invalid_grant", "Refresh reuse revoked this family; authorize again")
            elif row["revoked_at"] or row["expires_at"] <= audit.utcnow().isoformat():
                raise OAuthError("invalid_grant")
            else:
                conn.execute("UPDATE audience_oauth_tokens SET used_at=? WHERE digest=?", (audit.utcnow().isoformat(), row["digest"]))
                return self._issue(conn, row["connection_id"], data, row["family"])
        if failure:
            raise failure

    def resolve(self, secret):
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM audience_oauth_tokens WHERE digest=? AND kind='access'", (digest(secret),)).fetchone()
        if not row or row["revoked_at"] or row["expires_at"] <= audit.utcnow().isoformat():
            raise DomainError("UNAUTHENTICATED", "OAuth token expired or revoked; authorize again. See /developers#failures", 401)
        try:
            return self._connection(row["connection_id"], json.loads(row["data"]))
        except OAuthError:
            raise DomainError("UNAUTHENTICATED", "OAuth connection expired or revoked; authorize again", 401) from None

    async def verify_token(self, token):
        try:
            p = self.resolve(token)
            with self.db.connect() as conn:
                row = conn.execute("SELECT data,expires_at FROM audience_oauth_tokens WHERE digest=?", (digest(token),)).fetchone()
            return AccessToken(token=token, client_id=json.loads(row["data"])["client_id"], scopes=list(p.scopes),
                expires_at=int(datetime.fromisoformat(row["expires_at"]).timestamp()), resource=p.resource, subject=p.id)
        except DomainError:
            return None


class ConsentDecision(BaseModel):
    approve: bool
    agent_id: str | None = None
    customer_ids: list[str] | None = None
    scopes: list[str] | None = None
    notification_consent: dict = Field(default_factory=dict)


def human(request: Request, cookie: str | None = Depends(session_cookie)):
    cookie = request.cookies.get("vtg_session")
    if not cookie:
        scheme, _, secret = request.headers.get("authorization", "").partition(" ")
        admit_credential(request.app.state.gateway, request.app.state.keys,
            request.client.host if request.client else "unknown", secret if scheme.lower() == "bearer" else "")
        raise DomainError("HUMAN_REQUIRED", "Sign in at /v1/auth/login before authorizing this client", 403)
    return request.app.state.gateway.accounts.session(cookie)


@router.post("/oauth/register", status_code=201, response_model=dict, responses=OAUTH_ERRORS,
    openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": {
        "type": "object", "required": ["client_name", "redirect_uris", "application_type"], "properties": {
            "client_name": {"type": "string"}, "redirect_uris": {"type": "array", "items": {"type": "string", "format": "uri"}},
            "application_type": {"type": "string", "enum": ["native", "web"]},
            "token_endpoint_auth_method": {"type": "string", "enum": ["none"], "default": "none"}}}}}}})
async def register(request: Request, body: dict):
    secure_origin(request)
    return request.app.state.gateway.audience_oauth.register("client_"+token_urlsafe(24), body, dynamic=True)


@router.get("/.well-known/oauth-authorization-server", response_model=dict)
def authorization_metadata(request: Request):
    base = secure_origin(request)
    return {"issuer": base, "authorization_endpoint": base+"/oauth/authorize", "token_endpoint": base+"/oauth/token",
        "registration_endpoint": base+"/oauth/register", "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"], "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"], "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True, "scopes_supported": SCOPES}


@router.get("/oauth/authorize", response_class=HTMLResponse, responses=OAUTH_ERRORS)
async def authorize(request: Request, client_id: str, redirect_uri: str, resource: str, code_challenge: str,
                    code_challenge_method: str, response_type: str = "code", scope: str = "*", state: str | None = None,
                    p: Principal = Depends(human)):
    service = request.app.state.gateway
    secure_origin(request)
    client = await service.audience_oauth.client(client_id)
    if redirect_uri not in client["redirect_uris"]:
        raise OAuthError("invalid_redirect_uri")
    if response_type != "code" or code_challenge_method != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", code_challenge):
        raise OAuthError("invalid_request", "Authorization code with S256 PKCE is required")
    if not scope.split() or not set(scope.split()) <= set(SCOPES):
        raise OAuthError("invalid_scope")
    try:
        path = urlsplit(resource).path
    except ValueError:
        raise OAuthError("invalid_target") from None
    if path not in RESOURCE_PATHS or resource != origin(request) + path:
        raise OAuthError("invalid_target", "Choose this gateway's exact MCP or A2A resource")
    audience = RESOURCE_PATHS[path]
    if audience == "operator" and not p.operator or audience != "operator" and p.operator:
        raise DomainError("HUMAN_REQUIRED", "Sign in as a human of the requested audience", 403)
    request_id = "ahr_" + token_urlsafe(24)
    data = {"client": client, "redirect_uri": redirect_uri, "resource": resource, "challenge": code_challenge,
            "scope": scope, "state": state, "issuer": origin(request)}
    with service.db.connect(write=True) as conn:
        conn.execute("INSERT INTO audience_oauth_requests VALUES(?,?,?,?,NULL)", (request_id, p.user_id, json.dumps(data), (audit.utcnow()+timedelta(minutes=10)).isoformat()))
    frontend = service.settings.frontend_consent_url or os.getenv("FRONTEND_CONSENT_URL")
    if frontend:
        return RedirectResponse(redirect_to(frontend, request_id=request_id), status_code=302)
    return consent_page(service, request_id, p)


def consent_page(service, request_id, p):
    data = service.audience_oauth.request(request_id, p)
    esc = html.escape
    with service.db.connect() as conn:
        agents = [r[0] for r in conn.execute("SELECT agent_id FROM tenant_agents WHERE tenant_id=? AND disabled_at IS NULL", (p.tenant_id,))]
    options = ''.join(f'<option value="{esc(a)}">{esc(a)}</option>' for a in agents)
    return HTMLResponse(f'''<!doctype html><html><body><h1>Authorize {esc(data['client']['client_name'])}</h1>
<p>Redirect host: {esc(urlsplit(data['redirect_uri']).netloc)}. Resource: {esc(data['resource'])}</p>
<p>{esc(data['verification_label'])}. Registered: {esc(data['registered_at'] or 'unknown')}</p>
<form method="post" action="/oauth/requests/{esc(request_id)}/decision">
<label>Agent (tenant administrators may create one) <input name="agent_id" list="agents"></label><datalist id="agents">{options}</datalist>
<label>Customers <input name="customer_ids" value="{esc(' '.join(p.customer_ids))}"></label>
<label>Scopes <input name="scopes" value="{esc(data['scope'])}"></label>
<input name="csrf" type="hidden" value="{esc(service.audience_oauth.form_csrf(p, request_id))}">
<button name="approve" value="yes">Approve</button><button name="approve" value="no">Deny</button></form></body></html>''')


@router.get("/oauth/requests/{request_id}", response_class=HTMLResponse)
def consent(request_id: str, request: Request, p: Principal = Depends(human)):
    return consent_page(request.app.state.gateway, request_id, p)


@router.get("/v1/oauth/requests/{request_id}", response_model=dict)
def get_request(request_id: str, request: Request, p: Principal = Depends(resolve_principal)):
    service = request.app.state.gateway
    data = service.audience_oauth.request(request_id, p)
    with service.db.connect() as conn:
        agents = [dict(r) for r in conn.execute("SELECT agent_id,name FROM tenant_agents WHERE tenant_id=? AND disabled_at IS NULL", (p.tenant_id,))]
    return {"request_id": request_id, "client_name": data["client"]["client_name"], "redirect_host": urlsplit(data["redirect_uri"]).netloc,
        "resource": data["resource"], "scopes": data["scope"].split(), "customer_ids": list(p.customer_ids), "agents": agents,
        **{key: data[key] for key in ("client_tier", "registered_at", "verification_label", "logo")}}


@router.post("/v1/oauth/requests/{request_id}/decision", response_model=dict)
def decide(request_id: str, body: ConsentDecision, request: Request, p: Principal = Depends(resolve_principal)):
    return request.app.state.gateway.audience_oauth.decide(request_id, p, body)


@router.post("/oauth/requests/{request_id}/decision", response_class=RedirectResponse, status_code=303)
async def form_decision(request_id: str, request: Request, p: Principal = Depends(human)):
    service = request.app.state.gateway
    form = await request.form()
    if not compare_digest(form.get("csrf", ""), service.audience_oauth.form_csrf(p, request_id)):
        raise DomainError("CSRF_REQUIRED", "Invalid consent form token", 403)
    token = PRINCIPAL.set(p)
    try:
        result = service.audience_oauth.decide(request_id, p, ConsentDecision(approve=form.get("approve") == "yes", agent_id=form.get("agent_id") or None,
            customer_ids=str(form.get("customer_ids", "")).split() or None, scopes=str(form.get("scopes", "")).split() or None))
    finally:
        PRINCIPAL.reset(token)
    return RedirectResponse(result["redirect_url"], status_code=303)


@router.post("/oauth/token", response_model=dict, responses=OAUTH_ERRORS,
    openapi_extra={"requestBody": {"required": True, "content": {"application/x-www-form-urlencoded": {"schema": {
        "type": "object", "required": ["grant_type", "client_id", "resource"], "properties": {
            "grant_type": {"type": "string", "enum": ["authorization_code", "refresh_token"]},
            **{name: {"type": "string"} for name in ("client_id", "resource", "code", "redirect_uri", "code_verifier", "refresh_token")}}}}}}})
async def token(request: Request):
    secure_origin(request)
    form = dict(await request.form())
    return JSONResponse(request.app.state.gateway.audience_oauth.token(form), headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


def install(app):
    @app.exception_handler(OAuthError)
    async def oauth_error(request, error):
        return JSONResponse({"error": error.code, "error_description": error.description}, status_code=error.status, headers={"Cache-Control": "no-store"})
    app.include_router(router)
    # The SDK owns RFC 9728 serialization; build per-request to respect the
    # configured public origin and keep in-process/localhost deployments usable.
    # Starlette handlers are ASGI apps; forward through an ASGI endpoint.
    class Metadata:
        async def __call__(self, scope, receive, send):
            request = Request(scope, receive)
            path = request.url.path.removeprefix("/.well-known/oauth-protected-resource") or "/mcp"
            base = origin(request)
            route = create_protected_resource_routes(AnyHttpUrl(base+path), [AnyHttpUrl(base)], scopes_supported=[s for s in SCOPES if s != "offline_access"])[0]
            await route.app(scope, receive, send)
    from starlette.routing import Route
    for path in ["", *RESOURCE_PATHS]:
        app.router.routes.append(Route("/.well-known/oauth-protected-resource"+path, Metadata(), methods=["GET", "OPTIONS"]))
