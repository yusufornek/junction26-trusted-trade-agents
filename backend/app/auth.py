"""Credential resolution shared by REST and remote MCP. Only digests are retained."""
from dataclasses import dataclass
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

    @property
    def owner(self):
        return None if self.operator else self.agent_id


class Credentials:
    def __init__(self, operator_token="", agent_keys="", operator_agent_id="web-console"):
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

    def require_operator(self):
        if self._operator is None:
            raise RuntimeError("OPERATOR_TOKEN is required; run backend/scripts/gen_keys.py --write")

    def has_agent(self, agent_id):
        return any(identity == agent_id for identity, _ in self._agents)

    def resolve(self, secret):
        digest = sha256(secret.encode()).digest()
        principal = None
        if self._operator is not None and compare_digest(digest, self._operator):
            principal = Principal(self.operator_agent_id, operator=True)
        # Do every comparison, including after a match.
        for agent_id, expected in self._agents:
            if compare_digest(digest, expected):
                principal = Principal(agent_id)
        if principal is None:
            raise DomainError("UNAUTHENTICATED", "A valid Bearer credential is required", 401)
        return principal


bearer = HTTPBearer(auto_error=False, scheme_name="BearerAuth", description="Operator-issued agent key or operator token.")


def resolve_principal(request: Request, credential: HTTPAuthorizationCredentials | None = Depends(bearer)):
    if credential is None:
        raise DomainError("UNAUTHENTICATED", "A valid Bearer credential is required", 401)
    principal = request.app.state.keys.resolve(credential.credentials)
    request.state.principal = principal
    return principal


def require_operator(principal: Principal = Depends(resolve_principal)):
    if not principal.operator:
        raise DomainError("OPERATOR_REQUIRED", "The operator credential is required", 403)
    return principal


def check_owner(obj, agent_id, code):
    # Missing ownership on a legacy record never grants an agent access.
    if agent_id is not None and obj.agent_id != agent_id:
        raise DomainError(code, "Object was not found", 404)
    return obj


class MCPBearerMiddleware:
    """Guard every HTTP request, including initialization, GET and DELETE."""
    def __init__(self, app, keys, agent_id):
        self.app, self.keys, self.agent_id = app, keys, agent_id

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            try:
                headers = dict(scope.get("headers", []))
                value = headers.get(b"authorization", b"").decode("latin-1")
                scheme, _, token = value.partition(" ")
                if scheme.lower() != "bearer" or not token:
                    raise DomainError("UNAUTHENTICATED", "A valid Bearer credential is required", 401)
                principal = self.keys.resolve(token)
                if principal.operator or principal.agent_id != self.agent_id:
                    raise DomainError("UNAUTHENTICATED", "This server requires its agent's credential", 401)
            except DomainError as error:
                await JSONResponse({"code": error.code, "message": error.message}, status_code=401,
                                   headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
                return
        await self.app(scope, receive, send)
