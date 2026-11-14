"""Credential resolution shared by REST and remote MCP. Only digests are retained."""
from dataclasses import dataclass, asdict
from hashlib import sha256
from secrets import compare_digest

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
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

    @property
    def id(self):
        return self.agent_id

    def public(self):
        return {"id": self.id, **{k: v for k, v in asdict(self).items() if k not in {"agent_id", "operator"}}}

    @property
    def owner(self):
        return None if self.operator else self.agent_id


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
            principal = Principal(self.operator_agent_id, operator=True, kind="operator", role="valio_operator")
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
                         ("operator:" if principal.operator else "agent:") + principal.id + ":key:1")


bearer = HTTPBearer(auto_error=False, scheme_name="BearerAuth", description="Operator-issued agent key or operator token.")


def resolve_credential(keys, secret, service):
    policy = service.policy.get()
    try:
        return keys.resolve(secret, policy)
    except DomainError as error:
        if error.status != 401:
            raise
    principal = service.connections.resolve(secret)
    # Issued connections have exactly the same policy-derived identity as env keys.
    identity = policy.identities.get(principal.id)
    organisation = identity.organisation_id if identity else None
    customers = policy.organisations[organisation].customer_ids if organisation in policy.organisations else (
        policy.agents[principal.id].customer_ids if principal.id in policy.agents else [])
    return Principal(principal.id, kind=identity.kind if identity else "agent", organisation_id=organisation,
                     customer_ids=tuple(customers), role=identity.role if identity else "agent",
                     credential_ref=principal.credential_ref)


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


async def resolve_principal(request: Request, credential: HTTPAuthorizationCredentials | None = Depends(bearer)):
    from .foundation import PRINCIPAL
    service = request.app.state.gateway
    source = request.client.host if request.client else "unknown"
    secret = credential.credentials if credential is not None else ""
    principal = admit_credential(service, request.app.state.keys, source, secret)
    request.state.principal = principal
    PRINCIPAL.set(principal)
    path = request.url.path
    if request.method == "GET" and not (path.startswith(("/v1/orders", "/v1/quotes")) or path in {"/v1/me", "/v1/approvals"}):
        service.limiter.consume(principal.id, "read")
    return principal


def require_operator(principal: Principal = Depends(resolve_principal)):
    if not principal.operator:
        raise DomainError("OPERATOR_REQUIRED", "The operator credential is required", 403)
    return principal


def require_reviewer(principal: Principal = Depends(resolve_principal)):
    if not principal.operator and not (principal.kind == "buyer_user" and principal.role in {"buyer_admin", "buyer_approver"}):
        raise DomainError("OPERATOR_REQUIRED", "A buyer approver or operator credential is required", 403)
    return principal


def check_owner(obj, agent_id, code):
    # Missing ownership on a legacy record never grants an agent access.
    if agent_id is not None and obj.agent_id != agent_id:
        raise DomainError(code, "Object was not found", 404)
    return obj


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
                if principal.operator or (self.agent_id is not None and principal.agent_id != self.agent_id):
                    raise DomainError("UNAUTHENTICATED", "This server requires its agent's credential", 401)
                principal_token = PRINCIPAL.set(principal)
            except DomainError as error:
                response_headers = {"X-Correlation-Id": correlation}
                response_headers.update({"WWW-Authenticate": "Bearer"} if error.status == 401 else {"Retry-After": str(error.data["retry_after_seconds"])})
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
