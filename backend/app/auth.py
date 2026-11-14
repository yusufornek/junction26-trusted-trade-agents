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
    tenant_id: str | None = None
    user_id: str | None = None
    connection_id: str | None = None
    scopes: tuple[str, ...] = ("*",)
    audience: str = "legacy"

    @property
    def customers(self):
        return self.customer_ids

    @property
    def id(self):
        if self.user_id and self.user_id != "legacy-operator" and self.kind in {"tenant_user", "platform_admin"}:
            return self.user_id
        return self.agent_id

    def public(self):
        return {"id": self.id, **{k: v for k, v in asdict(self).items() if k not in {"agent_id", "operator"}}}

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


bearer = HTTPBearer(auto_error=False, scheme_name="BearerAuth", description="Operator-issued agent key or operator token.")
session_cookie = APIKeyCookie(name="vtg_session", auto_error=False, scheme_name="SessionCookie",
    description="Human session from POST /v1/auth/login. Mutations also require X-CSRF-Token from that response.")


def resolve_credential(keys, secret, service):
    if keys.recognizes(secret):
        return service.accounts.bind(keys.resolve(secret, service.policy.get()))
    try:
        keys.resolve(secret)  # Keep constant-time env-key comparisons below source admission.
    except DomainError as error:
        if error.status != 401:
            raise
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


def admit_credential(service, keys, source, secret):
    """Unknown credentials spend source tokens; known credentials retain principal limits."""
    known = keys.recognizes(secret)
    if not known:
        check_source(service, source)
    principal = resolve_credential(keys, secret, service)
    if not known:
        service.source_limiter.refund(source)
    if keys.environment != service.settings.environment:
        raise DomainError("UNAUTHENTICATED", "Credential belongs to another environment", 401)
    return principal


async def resolve_principal(request: Request, credential: HTTPAuthorizationCredentials | None = Depends(bearer),
                            cookie: str | None = Depends(session_cookie),
                            csrf: str | None = Header(default=None, alias="X-CSRF-Token",
                                description="Required on mutating requests authenticated by the human session cookie; returned by login.")):
    from .foundation import PRINCIPAL
    service = request.app.state.gateway
    source = request.client.host if request.client else "unknown"
    secret = credential.credentials if credential is not None else ""
    if credential is None and request.cookies.get("vtg_session"):
        principal = service.accounts.session(request.cookies["vtg_session"])
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            service.accounts.csrf(request.cookies["vtg_session"], request.headers.get("X-CSRF-Token", ""))
    else:
        principal = admit_credential(service, request.app.state.keys, source, secret)
    service.accounts.guard(principal, request.url.path, request.method)
    request.state.principal = principal
    PRINCIPAL.set(principal)
    service.accounts.guard_target(request, principal)
    path = request.url.path
    if request.method == "GET" and not (path.startswith(("/v1/orders", "/v1/quotes")) or path in {"/v1/me", "/v1/approvals"}):
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
    return obj


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
    def __init__(self, app, keys, agent_id, gateway):
        self.app, self.keys, self.agent_id, self.gateway = app, keys, agent_id, gateway

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
                principal = admit_credential(self.gateway, self.keys, source, secret)
                if principal.connection_id and principal.audience != "buyer":
                    raise DomainError("WRONG_AUDIENCE", "This key is bound to a different resource audience", 401)
                if principal.operator or (self.agent_id is not None and principal.agent_id != self.agent_id):
                    raise DomainError("UNAUTHENTICATED", "This server requires its agent's credential", 401)
                principal_token = PRINCIPAL.set(principal)
            except DomainError as error:
                response_headers = {"X-Correlation-Id": correlation}
                if error.status == 401:
                    response_headers["WWW-Authenticate"] = "Bearer"
                elif error.data.get("retry_after_seconds"):
                    response_headers["Retry-After"] = str(error.data["retry_after_seconds"])
                await JSONResponse(problem(error, scope.get("path")), status_code=error.status,
                                   media_type="application/problem+json", headers=response_headers)(scope, receive, send)
                CORRELATION.reset(correlation_token)
                return
        async def correlated_send(message):
            if message["type"] == "http.response.start" and correlation_token is not None:
                message["headers"] = [*message.get("headers", []), (b"x-correlation-id", CORRELATION.get().encode("latin-1"))]
            await send(message)
        try:
            await self.app(scope, receive, correlated_send)
        finally:
            if principal_token is not None:
                PRINCIPAL.reset(principal_token)
            if correlation_token is not None:
                CORRELATION.reset(correlation_token)
