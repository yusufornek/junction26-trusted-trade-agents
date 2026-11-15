"""Credential resolution shared by REST and remote MCP. Only digests are retained."""
from dataclasses import dataclass, asdict
from hashlib import sha256
from secrets import compare_digest

from fastapi import Depends, Header, Request
from fastapi.security import APIKeyCookie, HTTPAuthorizationCredentials, HTTPBearer
from starlette.responses import JSONResponse

from .db import DomainError


@dataclass(frozen=True)
class Principal:
    agent_id: str
    operator: bool = False
    kind: str = "agent"
    organisation_id: str | None = None
    customer_ids: tuple[str, ...] = ()
    role: str = "agent"
    credential_ref: str | None = None
    resource: str | None = None
    tenant_id: str | None = None
    user_id: str | None = None
    connection_id: str | None = None
    scopes: tuple[str, ...] = ("*",)
    audience: str = "legacy"
    actor_chain: tuple[str, ...] = ()
    delegation_id: str | None = None

    @property
    def customers(self):
        return self.customer_ids

    @property
    def id(self):
        if self.user_id and self.user_id != "legacy-operator" and self.kind in {"tenant_user", "platform_admin"}:
            return self.user_id
        return self.agent_id

    def public(self):
        return {"id": self.id, **{k: v for k, v in asdict(self).items() if k not in {"agent_id", "operator", "resource"}}}

    @property
    def owner(self):
        return None if self.operator or self.kind == "tenant_user" else self.agent_id


class Credentials:
    def __init__(self, operator_token="", agent_keys="", operator_agent_id="web-console", *, environment="live"):
        self.environment = environment
        self.operator_agent_id = operator_agent_id
        self._operator = sha256(operator_token.encode()).digest() if operator_token else None
        self._agents = []
        seen = {self._operator} if self._operator else set()
        for pair in agent_keys.split(","):
            if not pair.strip():
                continue
            agent_id, sep, secret = pair.strip().partition(":")
            if not sep or not agent_id.strip() or not secret:
                raise RuntimeError("Invalid AGENT_KEYS configuration; run scripts/gen_keys.py --write")
            digest = sha256(secret.encode()).digest()
            if digest in seen or any(agent_id == item[0] for item in self._agents):
                raise RuntimeError("Duplicate AGENT_KEYS identity or credential")
            seen.add(digest)
            self._agents.append((agent_id, digest))
        self._known = frozenset(seen)

    def require_operator(self):
        if self._operator is None:
            raise RuntimeError("OPERATOR_TOKEN is required; run backend/scripts/gen_keys.py --write")

    def has_agent(self, agent_id):
        return any(identity == agent_id for identity, _ in self._agents)

    def fingerprint(self, principal):
        """Local dispatch binding; never retain or expose the presented secret."""
        digest = self._operator if principal.operator else next(
            (digest for identity, digest in self._agents if identity == principal.id), None)
        return digest.hex() if digest else None

    @property
    def agent_ids(self):
        return tuple(identity for identity, _ in self._agents)

    def recognizes(self, secret):
        # A cheap credential-membership gate; full identity/policy resolution stays below admission.
        digest = sha256(secret.encode()).digest()
        return digest in self._known

    def resolve(self, secret, policy=None):
        digest = sha256(secret.encode()).digest()
        principal = None
        if self._operator is not None and compare_digest(digest, self._operator):
            principal = Principal(self.operator_agent_id, operator=True, kind="platform_admin", role="platform_admin", user_id="legacy-operator")
        # Do every comparison, including after a match.
        for agent_id, expected in self._agents:
            if compare_digest(digest, expected):
                principal = Principal(agent_id)
        if principal is None:
            raise DomainError("UNAUTHENTICATED", "A valid Bearer credential is required; expired or revoked credentials must be replaced by the issuer. See /developers#failures", 401)
        identity = policy.identities.get(principal.id) if policy else None
        organisation = identity.organisation_id if identity else None
        customers = policy.organisations[organisation].customer_ids if policy and organisation in policy.organisations else (
            policy.agents[principal.id].customer_ids if policy and principal.id in policy.agents else [])
        return Principal(principal.id, principal.operator, identity.kind if identity and not principal.operator else principal.kind,
                         organisation, tuple(customers), identity.role if identity and not principal.operator else principal.role,
                         ("operator:" if principal.operator else "agent:") + principal.id + ":key:1",
                         user_id=principal.user_id)


bearer = HTTPBearer(auto_error=False, scheme_name="BearerAuth", description="Operator-issued agent key, operator token or human session. OAuth access tokens are accepted only at their exact MCP or A2A resource.")
session_cookie = APIKeyCookie(name="vtg_session", auto_error=False, scheme_name="SessionCookie",
    description="Human session from POST /v1/auth/login. Mutations also require X-CSRF-Token from that response.")


def resolve_credential(keys, secret, service, resource=None):
    if secret.startswith("dg64_"):
        return service.handoff_authority.resolve(secret)
    if keys.recognizes(secret):
        return service.accounts.bind(keys.resolve(secret, service.policy.get()))
    try:
        keys.resolve(secret)  # Keep constant-time env-key comparisons below source admission.
    except DomainError as error:
        if error.status != 401:
            raise
    if secret.startswith(("oat_", "ort_")):
        from dataclasses import replace
        principal = service.oauth.resolve(secret, resource)
        return service.accounts.bind(replace(principal, connection_id=principal.credential_ref, audience="buyer"))
    if secret.startswith("vtg_oauth_"):
        return service.audience_oauth.resolve(secret)
    return service.accounts.bind(service.connections.resolve(secret))


def check_source(service, source):
    try:
        service.source_limiter.check(source)
    except DomainError as error:
        from . import audit
        with service.db.connect(write=True) as conn:
            audit.append(conn, "unauthenticated", "RATE_LIMITED", "Authentication source bucket exhausted.",
                         data={"category": "authentication", **error.data})
        raise


def observe_credential(service, principal, source):
    """Record a runtime only after the request's audience and identity checks pass."""
    service.connection_trust.seen(principal, source)
    if principal.kind == "agent" and principal.connection_id and hasattr(service, "governance"):
        service.governance.connection_seen(principal.agent_id, principal.connection_id, source)


def admit_credential(service, keys, source, secret, resource=None, *, observe=True):
    """Unknown credentials spend source tokens; known credentials retain principal limits."""
    known = keys.recognizes(secret)
    if not known:
        check_source(service, source)
    principal = resolve_credential(keys, secret, service, resource)
    if principal.kind == "agent" and hasattr(service, "governance"):
        try:
            service.governance.check_active(principal.agent_id)
        except DomainError as error:
            service.governance.note_refusal(principal.agent_id, error)
            raise
    if keys.environment != service.settings.environment:
        raise DomainError("UNAUTHENTICATED", "Credential belongs to another environment", 401)
    if observe:
        observe_credential(service, principal, source)
    if not known:
        service.source_limiter.refund(source)
    return principal


async def resolve_principal(request: Request, credential: HTTPAuthorizationCredentials | None = Depends(bearer),
                            cookie: str | None = Depends(session_cookie),
                            csrf: str | None = Header(default=None, alias="X-CSRF-Token",
                                description="Required on mutating requests authenticated by the human session cookie; returned by login.")):
    from .foundation import PRINCIPAL
    service = request.app.state.gateway
    source = request.client.host if request.client else "unknown"
    secret = credential.credentials if credential is not None else ""
    path = request.url.path
    resource = service.settings.public_url + "/mcp" if path == "/mcp" and service.settings.public_url else None
    if credential is None and request.cookies.get("vtg_session"):
        principal = service.accounts.session(request.cookies["vtg_session"])
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            service.accounts.csrf(request.cookies["vtg_session"], request.headers.get("X-CSRF-Token", ""))
    else:
        principal = admit_credential(service, request.app.state.keys, source, secret, resource, observe=False)
    if principal.resource:
        from .accounts import origin
        from .oauth import require_audience
        if path not in {"/mcp", "/a2a", "/a2a/tenant", "/a2a/operator"}:
            raise DomainError("WRONG_AUDIENCE", "OAuth token is bound to its MCP or A2A resource", 401)
        audience = "tenant" if path.endswith("/tenant") else "operator" if path.endswith("/operator") else "buyer"
        require_audience(principal, audience, path, origin(request))
    if request.url.path.startswith("/v1/connect/tools/"):
        from .oauth import require_audience
        audience = request.query_params.get("audience", "buyer")
        require_audience(principal, audience, "/tool-pack/"+audience)
    else:
        from .handoff_delegated_surfaces import scope_path
        service.accounts.guard(principal, scope_path(request.url.path), request.method)
    if principal.kind == "agent":
        service.governance.check_active(principal.agent_id)
    from .handoff_delegated_surfaces import rest_principal
    principal = rest_principal(service, principal, request)
    request.state.principal = principal
    PRINCIPAL.set(principal)
    service.accounts.guard_target(request, principal)
    observe_credential(service, principal, source)
    path = request.url.path
    if request.method == "GET" and not (path.startswith(("/v1/orders", "/v1/quotes", "/v1/lookups/")) or path in {"/v1/me", "/v1/approvals"}):
        service.limiter.consume(principal.id, "read")
    return principal


def require_operator(principal: Principal = Depends(resolve_principal)):
    if not principal.operator:
        raise DomainError("OPERATOR_REQUIRED", "The operator credential is required", 403)
    return principal


def require_reviewer(principal: Principal = Depends(resolve_principal)):
    if not principal.operator and not ((principal.kind == "buyer_user" and principal.role in {"buyer_admin", "buyer_approver"}) or
            (principal.kind == "tenant_user" and principal.role in {"tenant_admin", "approver"})):
        raise DomainError("OPERATOR_REQUIRED", "A buyer approver or operator credential is required", 403)
    return principal


def check_owner(obj, agent_id, code):
    # Missing ownership on a legacy record never grants an agent access.
    if agent_id is not None and obj.agent_id != agent_id:
        raise DomainError(code, "Object was not found", 404)
    customer_id = getattr(obj, "customer_id", None)
    if customer_id is None:
        customer_id = getattr(getattr(obj, "need", None) or getattr(obj, "request", None), "customer_id", None)
    if agent_id is not None and customer_id is not None:
        check_customer_scope(agent_id, customer_id)
    return obj


def check_customer_scope(agent_id, customer_id):
    """OAuth consent may narrow, never expand, an agent's current mandate."""
    if not customer_in_scope(agent_id, customer_id):
        raise DomainError("CUSTOMER_NOT_IN_MANDATE", "This connection does not allow the customer", 403)


def customer_in_scope(agent_id, customer_id):
    from .foundation import PRINCIPAL
    principal = PRINCIPAL.get()
    return (principal is None or principal.resource is None or principal.id != agent_id or
            customer_id in principal.customer_ids)
def check_scope(principal, resource, permission, action=None):
    if not principal or not principal.connection_id or "*" in principal.scopes:
        return
    accepted = {permission, resource, f"{resource}:{permission}"}
    if action:
        accepted.add(f"{resource}:{action}")
    if permission == "write":
        accepted.add(f"{resource}:manage")
        if resource == "approvals":
            accepted.add("approvals:decide")
    if not accepted & set(principal.scopes):
        raise DomainError("INSUFFICIENT_SCOPE", "Connection does not allow this operation", 403)


class MCPBearerMiddleware:
    """Guard every HTTP request, including initialization, GET and DELETE."""
    def __init__(self, app, keys, agent_id, gateway, audience="buyer"):
        self.app, self.keys, self.agent_id, self.gateway = app, keys, agent_id, gateway
        self.audience = audience

    async def __call__(self, scope, receive, send):
        from .foundation import CORRELATION, PRINCIPAL, correlation_id
        from .recovery import problem
        correlation_token = principal_token = None
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            correlation = headers.get(b"x-correlation-id", b"").decode("latin-1") or correlation_id()
            correlation_token = CORRELATION.set(correlation)
            try:
                source = (scope.get("client") or ("unknown",))[0]
                value = headers.get(b"authorization", b"").decode("latin-1")
                scheme, _, token = value.partition(" ")
                secret = token if scheme.lower() == "bearer" else ""
                from .oauth import OAuthError
                try:
                    resource = self.gateway.oauth.resource if self.audience == "buyer" and scope.get("path") == "/mcp" else None
                except OAuthError:
                    resource = None
                principal = admit_credential(self.gateway, self.keys, source, secret, resource, observe=False)
                from .oauth import require_audience
                base = self.gateway.settings.public_url or (scope.get("scheme", "http") + "://" + headers.get(b"host", b"localhost").decode("latin-1"))
                from .agent_surface import effective_audience
                selected = effective_audience(principal, self.audience) if self.agent_id is None else self.audience
                require_audience(principal, selected, "/mcp" + ("" if selected == "buyer" else "/" + selected), base)
                if selected == "buyer" and (principal.operator or (self.agent_id is not None and principal.agent_id != self.agent_id)):
                    raise DomainError("UNAUTHENTICATED", "This server requires its agent's credential", 401)
                observe_credential(self.gateway, principal, source)
                principal_token = PRINCIPAL.set(principal)
            except DomainError as error:
                response_headers = {"X-Correlation-Id": correlation}
                if error.status == 401:
                    base = self.gateway.settings.public_url or (scope.get("scheme", "http") + "://" + headers.get(b"host", b"localhost").decode("latin-1"))
                    suffix = "/mcp" + ("" if self.audience == "buyer" else "/" + self.audience)
                    response_headers["WWW-Authenticate"] = f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource{suffix}"'
                elif error.data.get("retry_after_seconds"):
                    response_headers["Retry-After"] = str(error.data["retry_after_seconds"])
                await JSONResponse(problem(error, scope.get("path")), status_code=error.status,
                                   media_type="application/problem+json", headers=response_headers)(scope, receive, send)
                CORRELATION.reset(correlation_token)
                return
        async def correlated_send(message):
            if message["type"] == "http.response.start" and correlation_token is not None:
                message["headers"] = [*message.get("headers", []), (b"x-correlation-id", CORRELATION.get().encode("latin-1")),
                                      (b"cache-control", b"private, no-store"), (b"vary", b"Authorization")]
            await send(message)
        try:
            from .connection_trust_transports import live_http
            if principal_token is not None:
                await live_http(self.app, self.gateway, principal, scope, receive, correlated_send)
            else:
                await self.app(scope, receive, correlated_send)
        finally:
            if principal_token is not None:
                PRINCIPAL.reset(principal_token)
            if correlation_token is not None:
                CORRELATION.reset(correlation_token)
