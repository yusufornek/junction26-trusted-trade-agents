"""Operator-consented OAuth for the buyer MCP resource (MCP 2026-07-28 / 2025-11-25).

Only protocol issuance responses contain credentials. SQLite retains digests of codes,
access/refresh tokens, browser sessions and client secrets. No upstream sees a bearer.
"""
import base64
import ipaddress
import json
import re
import socket
import time
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html import escape
from secrets import compare_digest, token_hex, token_urlsafe
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit, unquote_plus

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import audit
from .auth import Principal, admit_credential, require_operator, resolve_principal
from .oauth_audiences import require_audience, ConsentDecision as AudienceConsentDecision, SCOPES

AUDIENCE_SCOPES = [scope for scope in SCOPES if scope != "offline_access"]
from .db import DomainError
from .connection_notifications_models import NotificationConsent, consent_form, form_consent

SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_clients (
 client_id TEXT PRIMARY KEY, metadata TEXT NOT NULL, secret_digest TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_requests (
 id TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL, state TEXT,
 challenge TEXT NOT NULL, resource TEXT NOT NULL, scope TEXT NOT NULL,
 expires_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS oauth_families (
 id TEXT PRIMARY KEY, connection_id TEXT NOT NULL REFERENCES connections(connection_id),
 client_id TEXT NOT NULL, resource TEXT NOT NULL, scope TEXT NOT NULL,
 expires_at TEXT NOT NULL, revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS oauth_codes (
 digest TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES oauth_requests(id),
 family_id TEXT NOT NULL REFERENCES oauth_families(id), expires_at TEXT NOT NULL, used_at TEXT
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
 digest TEXT PRIMARY KEY, family_id TEXT NOT NULL REFERENCES oauth_families(id),
 kind TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT, revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS oauth_tokens_family ON oauth_tokens(family_id);
CREATE INDEX IF NOT EXISTS oauth_families_connection ON oauth_families(connection_id);
CREATE TABLE IF NOT EXISTS oauth_browser_sessions (
 digest TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES oauth_requests(id),
 csrf_digest TEXT NOT NULL, expires_at TEXT NOT NULL
);
"""

router = APIRouter(tags=["OAuth"])
SAFE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"}
FORM_SCHEMA = {"requestBody": {"required": True, "content": {"application/x-www-form-urlencoded": {
    "schema": {"type": "object", "additionalProperties": {"type": "string"}}}}}}


def now():
    # OAuth lifetimes use wall time even in a sandbox with a frozen business clock.
    return datetime.now(timezone.utc)


def digest(value):
    return sha256(value.encode()).hexdigest()


def challenge(verifier):
    return base64.urlsafe_b64encode(sha256(verifier.encode()).digest()).decode().rstrip("=")


class OAuthError(Exception):
    def __init__(self, error="invalid_request", status=400):
        self.error, self.status = error, status


class ClientMetadata(BaseModel):
    # RFC 7591 extension metadata may be sent, but is never reflected or retained.
    client_name: str = Field(min_length=1, max_length=120)
    redirect_uris: list[str] = Field(min_length=1, max_length=10)
    application_type: Literal["web", "native"] | None = None
    token_endpoint_auth_method: Literal["none", "client_secret_post", "client_secret_basic"] = "none"
    # Unknown grant types (claude.ai also lists jwt-bearer) are ignored, not rejected; only these two are ever issued.
    grant_types: list[str] = Field(default_factory=lambda: ["authorization_code"], max_length=10)
    response_types: list[Literal["code"]] = Field(default_factory=lambda: ["code"])


class Registration(ClientMetadata):
    client_name: str = Field(default="MCP client", min_length=1, max_length=120)
    application_type: Literal["web", "native"]
    token_endpoint_auth_method: Literal["none", "client_secret_post", "client_secret_basic"] = "client_secret_basic"


class PreRegistration(Registration):
    client_id: str = Field(min_length=1, max_length=200)


class RegisteredClient(Registration):
    client_id: str
    client_id_issued_at: int
    client_secret: str | None = Field(default=None, repr=False)
    client_secret_expires_at: int | None = None


class ConsentDecision(BaseModel):
    notifications: NotificationConsent = Field(default_factory=NotificationConsent)
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "deny"]
    agent_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{1,47}$")
    customer_ids: list[str] = Field(default_factory=list, max_length=100)
    scopes: list[str] | None = None
    notification_consent: dict = Field(default_factory=dict)


class ConsentResult(BaseModel):
    status: Literal["approved", "denied"]
    redirect_url: str = Field(description="One-time OAuth response; navigate once, never persist or log it.")
    connection_id: str | None = None


class ConsentRequest(BaseModel):
    notifications: NotificationConsent = Field(default_factory=NotificationConsent)
    id: str
    client_name: str
    redirect_host: str
    resource: str
    scopes: list[str]
    expires_at: str
    status: str
    customer_ids: list[str]
    agents: list[str]
    client_tier: str = "unverified"
    registered_at: str | None = None
    verification_label: str = "not verified"
    logo: str | None = None


class TokenResponse(BaseModel):
    access_token: str = Field(repr=False)
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    refresh_token: str | None = Field(default=None, repr=False)
    scope: str


class OAuthFailure(BaseModel):
    error: str


class ResourceMetadata(BaseModel):
    resource: str
    authorization_servers: list[str]
    scopes_supported: list[str]
    bearer_methods_supported: list[str]


class AuthorizationMetadata(BaseModel):
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    response_types_supported: list[str]
    grant_types_supported: list[str]
    code_challenge_methods_supported: list[str]
    token_endpoint_auth_methods_supported: list[str]
    scopes_supported: list[str]
    client_id_metadata_document_supported: bool
    authorization_response_iss_parameter_supported: bool


OAUTH_RESPONSES = {code: {"model": OAuthFailure} for code in (400, 401, 503)}
TOKEN_FORM_SCHEMA = {"requestBody": {"required": True, "content": {"application/x-www-form-urlencoded": {
    "schema": {"type": "object", "required": ["grant_type", "resource"], "properties": {
        "grant_type": {"type": "string", "enum": ["authorization_code", "refresh_token"]},
        **{name: {"type": "string"} for name in ["client_id", "code", "redirect_uri", "resource", "scope"]},
        **{name: {"type": "string", "writeOnly": True} for name in ["code_verifier", "refresh_token", "client_secret"]}
    }}}}}}


def validate_metadata(value, *, dcr=False):
    try:
        model = (Registration if dcr else ClientMetadata).model_validate(value)
        model.grant_types = [g for g in model.grant_types if g in {"authorization_code", "refresh_token"}]
        if (any(ord(c) < 32 for c in model.client_name) or "authorization_code" not in model.grant_types or
                "code" not in model.response_types):
            raise ValueError()
        for uri in model.redirect_uris:
            url = urlsplit(uri)
            local = url.hostname in {"localhost", "127.0.0.1", "::1"}
            if (len(uri) > 2048 or any(c.isspace() for c in uri) or not url.hostname or
                    url.username is not None or url.password is not None or url.fragment or "#" in uri or
                    "*" in uri or url.port == 0 or
                    not (url.scheme == "https" or (url.scheme == "http" and local and model.application_type != "web")) or
                    any(k in {"code", "state", "iss", "error"} for k, _ in parse_qsl(url.query))):
                raise ValueError()
        return model.model_dump(exclude_none=True)
    except (ValidationError, TypeError, ValueError):
        raise OAuthError("invalid_client_metadata") from None


async def form(request):
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
        raise OAuthError()
    data = bytearray()
    async for part in request.stream():
        data.extend(part)
        if len(data) > 16_384:
            raise OAuthError()
    try:
        pairs = parse_qsl(data.decode("utf-8"), keep_blank_values=True, max_num_fields=100)
    except (ValueError, UnicodeError):
        raise OAuthError() from None
    return pairs


def unique(pairs):
    values = {}
    for key, value in pairs:
        if key in values:
            raise OAuthError()
        values[key] = value
    return values


class OAuth:
    metadata_limit = 65_536
    metadata_timeout = 5

    def __init__(self, gateway):
        self.gateway = gateway
        self.resolver = socket.getaddrinfo

    @property
    def issuer(self):
        origin = self.gateway.settings.public_url
        url = urlsplit(origin)
        if not origin or (url.scheme != "https" and url.hostname not in {"localhost", "127.0.0.1", "::1"}):
            raise OAuthError("temporarily_unavailable", 503)
        return origin

    @property
    def resource(self):
        return self.issuer + "/mcp"

    def www_authenticate(self):
        try:
            return f'Bearer resource_metadata="{self.issuer}/.well-known/oauth-protected-resource/mcp", scope="mcp"'
        except OAuthError:
            return "Bearer"

    def register(self, value, metadata=None, *, client_id=None):
        if metadata is not None:
            return self.gateway.audience_oauth.register(value, metadata)
        metadata = validate_metadata(value, dcr=True)
        client_id = client_id or "cli_" + token_urlsafe(24)
        secret = None if metadata["token_endpoint_auth_method"] == "none" else token_urlsafe(32)
        with self.gateway.db.connect(write=True) as conn:
            if conn.execute("SELECT 1 FROM oauth_clients WHERE client_id=?", (client_id,)).fetchone():
                raise OAuthError("invalid_client_metadata")
            conn.execute("INSERT INTO oauth_clients VALUES(?,?,?,?)", (
                client_id, json.dumps(metadata), digest(secret) if secret else None, now().isoformat()))
        return {**metadata, "client_id": client_id, "client_id_issued_at": int(now().timestamp()),
                "client_secret": secret, "client_secret_expires_at": 0 if secret else None}

    async def verify_token(self, token):
        return await self.gateway.audience_oauth.verify_token(token)

    def fetch_metadata(self, client_id):
        """Bounded HTTPS fetch, no redirects/proxies/caller credentials; pin validated DNS."""
        try:
            url = urlsplit(client_id)
            if (url.scheme != "https" or not url.hostname or url.path in {"", "/"} or url.username is not None or
                    url.password is not None or url.fragment or url.query or len(client_id) > 2048):
                raise ValueError()
            started = time.monotonic()
            addresses = list(dict.fromkeys(row[4][0] for row in self.resolver(
                url.hostname, url.port or 443, type=socket.SOCK_STREAM)))
            if not addresses:
                raise ValueError()
            for address in addresses:
                ip = ipaddress.ip_address(address)
                if not ip.is_global or ip.is_multicast or ip.is_reserved or (
                        ip.version == 6 and (ip.ipv4_mapped or ip.sixtofour or ip.teredo)):
                    raise ValueError()
            try:
                literal = ipaddress.ip_address(url.hostname)
            except ValueError:
                literal = None
            if literal is not None and not literal.is_global:
                raise ValueError()
            pinned = httpx.URL(client_id).copy_with(host=addresses[0])
            with httpx.Client(timeout=self.metadata_timeout, follow_redirects=False, trust_env=False) as client:
                with client.stream("GET", pinned, headers={"Host": url.netloc, "Accept": "application/json"},
                                   extensions={"sni_hostname": url.hostname}) as response:
                    if (response.status_code != 200 or response.headers.get("content-type", "").split(";")[0] != "application/json" or
                            int(response.headers.get("content-length", "0")) > self.metadata_limit):
                        raise ValueError()
                    data = bytearray()
                    for part in response.iter_bytes():
                        data.extend(part)
                        if len(data) > self.metadata_limit or time.monotonic() - started > self.metadata_timeout:
                            raise ValueError()
            value = json.loads(data)
            if not isinstance(value, dict) or value.get("client_id") != client_id:
                raise ValueError()
            metadata = validate_metadata(value)
            # private_key_jwt is optional; CIMD clients here are public, protected by PKCE.
            if metadata["token_endpoint_auth_method"] != "none":
                raise ValueError()
            return metadata
        except (httpx.HTTPError, ValueError, TypeError, OSError, UnicodeError, OAuthError):
            raise OAuthError("invalid_client") from None

    def client(self, client_id, *, discover=False):
        if discover and client_id.startswith("https://"):
            metadata = self.fetch_metadata(client_id)
            with self.gateway.db.connect(write=True) as conn:
                conn.execute("INSERT INTO oauth_clients VALUES(?,?,NULL,?) ON CONFLICT(client_id) DO UPDATE "
                             "SET metadata=excluded.metadata", (client_id, json.dumps(metadata), now().isoformat()))
            return metadata
        with self.gateway.db.connect() as conn:
            row = conn.execute("SELECT metadata FROM oauth_clients WHERE client_id=?", (client_id,)).fetchone()
        if row is None:
            raise OAuthError("invalid_client")
        return json.loads(row[0])

    def authorize(self, params):
        client_id, redirect = params.get("client_id", ""), params.get("redirect_uri", "")
        metadata = self.client(client_id, discover=True)
        # Never redirect an error to an unvalidated URI.
        if redirect not in metadata["redirect_uris"]:
            raise OAuthError()
        try:
            self.validate_authorization(params)
        except OAuthError as error:
            url = urlsplit(redirect)
            response = {"error": error.error, "iss": self.issuer}
            if params.get("state") is not None and len(params["state"]) <= 2048:
                response["state"] = params["state"]
            error.redirect_url = urlunsplit(url._replace(query=urlencode([*parse_qsl(url.query), *response.items()])))
            raise
        scopes = params.get("scope", "mcp").split()
        request_id = "oar_" + token_urlsafe(24)
        with self.gateway.db.connect(write=True) as conn:
            conn.execute("INSERT INTO oauth_requests(id,client_id,redirect_uri,state,challenge,resource,scope,expires_at) "
                         "VALUES(?,?,?,?,?,?,?,?)", (request_id, client_id, redirect, params.get("state"),
                         params["code_challenge"], self.resource, " ".join(dict.fromkeys(scopes)),
                         (now() + timedelta(minutes=10)).isoformat()))
        return request_id

    def validate_authorization(self, params):
        if params.get("response_type") != "code":
            raise OAuthError("unsupported_response_type")
        if params.get("resource") != self.resource:
            raise OAuthError("invalid_target")
        if params.get("code_challenge_method") != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.get("code_challenge", "")):
            raise OAuthError()
        requested = (params.get("scope") or "mcp").split()
        # Issue what this resource offers ("mcp", plus "offline_access" when asked) instead of refusing clients that
        # request every advertised scope; RFC 6749 section 3.3 lets the server grant fewer scopes than requested.
        params["scope"] = " ".join(["mcp"] + (["offline_access"] if "offline_access" in requested else []))
        if len(params.get("state", "")) > 2048:
            raise OAuthError()

    def request_row(self, conn, request_id, *, pending=False):
        row = conn.execute("SELECT * FROM oauth_requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise DomainError("OAUTH_REQUEST_NOT_FOUND", "Consent request was not found", 404)
        if datetime.fromisoformat(row["expires_at"]) <= now():
            raise DomainError("OAUTH_REQUEST_EXPIRED", "Restart sign-in from the MCP client", 410)
        if pending and row["status"] != "pending":
            raise DomainError("OAUTH_REQUEST_DECIDED", "Consent was already decided; restart sign-in", 409)
        return row

    def view(self, request_id):
        with self.gateway.db.connect() as conn:
            row = self.request_row(conn, request_id)
        policy = self.gateway.policy.get()
        customers = set(policy.customers)
        for mandate in policy.agents.values():
            customers.update(mandate.customer_ids)
        for organisation in policy.organisations.values():
            customers.update(organisation.customer_ids)
        return {"id": row["id"], "client_name": self.client(row["client_id"])["client_name"],
                "redirect_host": urlsplit(row["redirect_uri"]).netloc, "resource": row["resource"],
                "scopes": row["scope"].split(), "status": row["status"], "expires_at": row["expires_at"],
                "customer_ids": sorted(customers), "agents": sorted(policy.agents), "notifications": NotificationConsent().model_dump()}

    def decide(self, request_id, body, principal):
        require_operator(principal)
        self.gateway.limiter.consume(principal.id, "mutation")
        with self.gateway.db.connect(write=True) as conn:
            row = self.request_row(conn, request_id, pending=True)
            # Changing the public origin cannot silently change an outstanding grant's audience.
            if row["resource"] != self.resource:
                raise OAuthError("invalid_target")
            approved = body.decision == "approve"
            connection_id = None
            response = {"iss": self.issuer}
            if row["state"] is not None:
                response["state"] = row["state"]
            if approved:
                if not body.agent_id or not body.customer_ids:
                    raise DomainError("VALIDATION_ERROR", "Choose an agent and at least one customer", 422)
                scopes = body.scopes if body.scopes is not None else row["scope"].split()
                if "mcp" not in scopes or set(scopes) - set(row["scope"].split()):
                    raise OAuthError("invalid_scope")
                self.gateway.connections.prepare_agent(conn, body.agent_id, body.customer_ids)
                connection_id = "con_" + token_hex(4)
                # Classify by the verified callback host, never the untrusted display name.
                host = urlsplit(row["redirect_uri"]).hostname
                client_kind = "chatgpt" if host in {"chatgpt.com", "chat.openai.com"} else "mcp_other"
                conn.execute("INSERT INTO connections(connection_id,agent_id,client,key_prefix,key_digest,"
                             "customer_ids,created_at,credential_kind) VALUES(?,?,?,?,?,?,?,?)", (
                             connection_id, body.agent_id, client_kind, "oauth", digest(token_urlsafe(32)),
                             json.dumps(list(dict.fromkeys(body.customer_ids))), now().isoformat(), "oauth"))
                self.gateway.notifications.initialize(connection_id, body.notifications)
                family_id = "of_" + token_urlsafe(24)
                conn.execute("INSERT INTO oauth_families VALUES(?,?,?,?,?,?,NULL)", (family_id, connection_id,
                             row["client_id"], row["resource"], " ".join(dict.fromkeys(scopes)),
                             (now() + timedelta(days=30)).isoformat()))
                code = token_urlsafe(32)
                conn.execute("INSERT INTO oauth_codes VALUES(?,?,?,?,NULL)", (digest(code), request_id, family_id,
                             (now() + timedelta(minutes=5)).isoformat()))
                response["code"] = code
                audit.append(conn, "human:operator", "CONNECTION_CREATED", "Approved an OAuth connection.",
                             data={"connection_id": connection_id, "agent_id": body.agent_id,
                                   "client": client_kind, "credential_kind": "oauth"})
            else:
                response["error"] = "access_denied"
            status = "approved" if approved else "denied"
            conn.execute("UPDATE oauth_requests SET status=? WHERE id=?", (status, request_id))
            conn.execute("DELETE FROM oauth_browser_sessions WHERE request_id=?", (request_id,))
            audit.append(conn, "human:operator", "OAUTH_CONSENT_APPROVED" if approved else "OAUTH_CONSENT_DENIED",
                         "Operator decided an MCP consent request.", data={"connection_id": connection_id})
            url = urlsplit(row["redirect_uri"])
            query = urlencode([*parse_qsl(url.query, keep_blank_values=True), *response.items()])
            return {"status": status, "redirect_url": urlunsplit(url._replace(query=query)), "connection_id": connection_id}

    def authenticate_client(self, values, authorization):
        client_id, secret = values.get("client_id", ""), values.get("client_secret")
        method = "client_secret_post" if secret is not None else "none"
        if authorization:
            try:
                if not authorization.startswith("Basic ") or secret is not None:
                    raise ValueError()
                identity, secret = base64.b64decode(authorization[6:], validate=True).decode().split(":", 1)
                identity, secret = unquote_plus(identity), unquote_plus(secret)
                if client_id and client_id != identity:
                    raise ValueError()
                client_id, method = identity, "client_secret_basic"
            except (ValueError, UnicodeError):
                raise OAuthError("invalid_client", 401) from None
        with self.gateway.db.connect() as conn:
            row = conn.execute("SELECT * FROM oauth_clients WHERE client_id=?", (client_id,)).fetchone()
        if row is None:
            raise OAuthError("invalid_client", 401)
        metadata = json.loads(row["metadata"])
        registered = metadata["token_endpoint_auth_method"]
        # Confidential clients may present their secret in the body or in a Basic header (clients differ on this);
        # a public client is accepted even if it also sends an empty or unused secret.
        if registered == "none":
            if row["secret_digest"]:
                raise OAuthError("invalid_client", 401)
        elif secret is None or not compare_digest(digest(secret), row["secret_digest"] or ""):
            raise OAuthError("invalid_client", 401)
        return client_id, metadata

    def revoke_family(self, conn, family_id):
        family = conn.execute("SELECT * FROM oauth_families WHERE id=?", (family_id,)).fetchone()
        if family and family["revoked_at"] is None:
            conn.execute("UPDATE oauth_families SET revoked_at=? WHERE id=?", (now().isoformat(), family_id))
            audit.append(conn, "human:operator", "OAUTH_TOKEN_FAMILY_REVOKED", "Revoked an OAuth token family.",
                         data={"connection_id": family["connection_id"]})

    def revoke_connection(self, conn, connection_id):
        conn.execute("UPDATE audience_oauth_tokens SET revoked_at=? WHERE connection_id=?", (now().isoformat(), connection_id))
        for row in conn.execute("SELECT id FROM oauth_families WHERE connection_id=?", (connection_id,)).fetchall():
            self.revoke_family(conn, row[0])

    def issue_tokens(self, conn, family, *, issue_refresh=True):
        family = dict(family)
        family["expires_at"] = (now() + timedelta(days=30)).isoformat()
        conn.execute("UPDATE oauth_families SET expires_at=? WHERE id=?", (family["expires_at"], family["id"]))
        access, refresh = "oat_" + token_urlsafe(32), "ort_" + token_urlsafe(32)
        expires = min(now() + timedelta(hours=1), datetime.fromisoformat(family["expires_at"]))
        for value, kind, expiry in [(access, "access", expires.isoformat()), (refresh, "refresh", family["expires_at"])]:
            if kind == "refresh" and not issue_refresh:
                continue
            conn.execute("INSERT INTO oauth_tokens VALUES(?,?,?,?,NULL,NULL)", (digest(value), family["id"], kind, expiry))
        return {"access_token": access, "token_type": "Bearer", "expires_in": max(0, round((expires - now()).total_seconds())),
                **({"refresh_token": refresh} if issue_refresh else {}), "scope": family["scope"]}

    def token(self, values, authorization):
        client_id, metadata = self.authenticate_client(values, authorization)
        grant = values.get("grant_type")
        if grant not in {"authorization_code", "refresh_token"}:
            raise OAuthError("unsupported_grant_type")
        if grant not in metadata["grant_types"]:
            raise OAuthError("unauthorized_client")
        if values.get("resource") != self.resource:
            raise OAuthError("invalid_target")
        error, result = None, None
        with self.gateway.db.connect(write=True) as conn:
            if grant == "authorization_code":
                credential = conn.execute("SELECT c.*,r.client_id,r.redirect_uri,r.challenge FROM oauth_codes c "
                    "JOIN oauth_requests r ON r.id=c.request_id WHERE c.digest=?", (digest(values.get("code", "")),)).fetchone()
            else:
                credential = conn.execute("SELECT * FROM oauth_tokens WHERE digest=? AND kind='refresh'",
                                          (digest(values.get("refresh_token", "")),)).fetchone()
            family = conn.execute("SELECT f.*,c.revoked_at AS connection_revoked FROM oauth_families f "
                "JOIN connections c ON c.connection_id=f.connection_id WHERE f.id=?",
                (credential["family_id"],)).fetchone() if credential else None
            if family is None or family["client_id"] != client_id or family["resource"] != values["resource"]:
                raise OAuthError("invalid_grant")
            if grant == "authorization_code":
                verifier = values.get("code_verifier", "")
                if (values.get("redirect_uri") != credential["redirect_uri"] or
                        not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier) or
                        not compare_digest(challenge(verifier), credential["challenge"])):
                    raise OAuthError("invalid_grant")
            if credential["used_at"] is not None:
                # Commit revocation before returning the error; an exception inside the transaction rolls it back.
                self.revoke_family(conn, family["id"])
                error = OAuthError("invalid_grant")
            elif (family["revoked_at"] is not None or family["connection_revoked"] is not None or
                    datetime.fromisoformat(family["expires_at"]) <= now() or
                    datetime.fromisoformat(credential["expires_at"]) <= now()):
                raise OAuthError("invalid_grant")
            else:
                if values.get("scope") is not None:
                    scopes = values["scope"].split()
                    if "mcp" not in scopes or set(scopes) - set(family["scope"].split()):
                        raise OAuthError("invalid_scope")
                    family = dict(family)
                    family["scope"] = " ".join(dict.fromkeys(scopes))
                    conn.execute("UPDATE oauth_families SET scope=? WHERE id=?", (family["scope"], family["id"]))
                table = "oauth_codes" if grant == "authorization_code" else "oauth_tokens"
                conn.execute(f"UPDATE {table} SET used_at=? WHERE digest=?", (now().isoformat(), credential["digest"]))
                if grant == "refresh_token":
                    conn.execute("UPDATE oauth_tokens SET revoked_at=? WHERE family_id=? AND kind='access'",
                                 (now().isoformat(), family["id"]))
                result = self.issue_tokens(conn, family, issue_refresh="refresh_token" in metadata["grant_types"])
        if error:
            raise error
        return result

    def resolve(self, secret, resource):
        def refused():
            return DomainError("UNAUTHENTICATED", "MCP sign-in expired or was revoked; reconnect at the issuer. See /developers#failures", 401)
        try:
            if resource != self.resource:
                raise refused()
        except OAuthError:
            raise refused() from None
        with self.gateway.db.connect(write=True, notify=False) as conn:
            row = conn.execute("SELECT t.digest,t.expires_at AS token_expires_at,t.revoked_at AS token_revoked_at,"
                "f.revoked_at AS family_revoked,f.expires_at AS family_expires_at,f.resource,c.* "
                "FROM oauth_tokens t JOIN oauth_families f ON f.id=t.family_id "
                "JOIN connections c ON c.connection_id=f.connection_id WHERE t.digest=? AND t.kind='access'",
                (digest(secret),)).fetchone()
            if (row is None or not compare_digest(row["digest"], digest(secret)) or row["resource"] != resource or
                    row["token_revoked_at"] is not None or row["family_revoked"] is not None or row["revoked_at"] is not None or
                    datetime.fromisoformat(row["token_expires_at"]) <= now() or
                    datetime.fromisoformat(row["family_expires_at"]) <= now()):
                raise refused()
            conn.execute("UPDATE connections SET last_used_at=? WHERE connection_id=? AND "
                         "(last_used_at IS NULL OR last_used_at<=?)", (now().isoformat(), row["connection_id"],
                         (now() - timedelta(minutes=1)).isoformat()))
        customers = set(json.loads(row["customer_ids"]))
        mandate = self.gateway.policy.get().agents.get(row["agent_id"])
        if mandate:
            customers.intersection_update(mandate.customer_ids)
        else:
            # Signed-only agents retain their existing authority, but history is not
            # authority by itself: verify signature, expiry and revocation on every call.
            from .mandates import decode
            allowed = set()
            with self.gateway.db.connect() as conn:
                for stored in conn.execute("SELECT jwt FROM mandate_credentials"):
                    try:
                        subject = decode(stored[0])[1]["vc"]["credentialSubject"]
                        if subject["agent_id"] != row["agent_id"]:
                            continue
                        self.gateway.mandates.verify(conn, stored[0], row["agent_id"], subject["customer_id"])
                        allowed.add(subject["customer_id"])
                    except (DomainError, KeyError, TypeError):
                        continue
            customers.intersection_update(allowed)
        return Principal(row["agent_id"], customer_ids=tuple(sorted(customers)),
                         credential_ref=row["connection_id"], resource=resource)


def page(content):
    return HTMLResponse('<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width"><title>Connect a procurement agent</title>'
        '<body><main><h1>Connect a procurement agent</h1>' + content + '</main></body></html>',
        headers={**SAFE_HEADERS, "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"})


def intro(oauth, request_id):
    with oauth.gateway.db.connect() as conn:
        row = oauth.request_row(conn, request_id, pending=True)
    name = oauth.client(row["client_id"])["client_name"]
    host = urlsplit(row["redirect_uri"]).netloc
    warning = '<p>Warning: a local application will receive this authorization.</p>' if urlsplit(row["redirect_uri"]).hostname in {"localhost", "127.0.0.1", "::1"} else ''
    return (f'<p>Client: <strong>{escape(name)}</strong></p><p>Redirect host: <strong>{escape(host)}</strong></p>'
            + f'<p>{escape(oauth.view(request_id)["verification_label"])}. Registered: {escape(oauth.view(request_id)["registered_at"] or "unknown")}</p>'
            + warning + f'<form method="post" action="/oauth/authorize"><input type="hidden" name="request_id" value="{escape(request_id)}">')


@router.get("/.well-known/oauth-protected-resource", response_model=ResourceMetadata, responses=OAUTH_RESPONSES, summary="RFC 9728 buyer MCP protected resource metadata")
@router.get("/.well-known/oauth-protected-resource/mcp", response_model=ResourceMetadata, responses=OAUTH_RESPONSES, summary="RFC 9728 path-specific MCP resource metadata")
def protected_resource(request: Request):
    oauth = request.app.state.gateway.oauth
    from .accounts import origin
    base = origin(request)
    return {"resource": base + "/mcp", "authorization_servers": [base],
            "scopes_supported": ["mcp", *AUDIENCE_SCOPES], "bearer_methods_supported": ["header"]}


@router.get("/.well-known/oauth-authorization-server", response_model=AuthorizationMetadata, responses=OAUTH_RESPONSES, summary="RFC 8414 OAuth authorization server metadata")
def authorization_server(request: Request):
    oauth = request.app.state.gateway.oauth
    from .accounts import origin
    issuer = origin(request)
    return {"issuer": issuer, "authorization_endpoint": issuer + "/oauth/authorize", "token_endpoint": issuer + "/oauth/token",
            "registration_endpoint": issuer + "/oauth/register", "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"], "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
            "scopes_supported": ["mcp", *AUDIENCE_SCOPES, "offline_access"], "client_id_metadata_document_supported": False,
            "authorization_response_iss_parameter_supported": True}


@router.post("/oauth/register", status_code=201, response_model=RegisteredClient, response_model_exclude_none=True,
             responses=OAUTH_RESPONSES, openapi_extra={"requestBody": {"required": True,
                "content": {"application/json": {"schema": Registration.model_json_schema()}}}},
             summary="RFC 7591 public or confidential client registration; application_type defaults to web")
async def register(request: Request):
    # Parse manually so OAuth clients receive RFC 7591's 400, never framework 422 input echoes.
    request.app.state.gateway.source_limiter.check(request.client.host if request.client else "unknown")
    data = bytearray()
    async for part in request.stream():
        data.extend(part)
        if len(data) > 16_384:
            raise OAuthError("invalid_client_metadata")
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError):
        raise OAuthError("invalid_client_metadata") from None
    # RFC 7591 / OIDC dynamic registration: a missing application_type means "web". claude.ai and ChatGPT omit it.
    if isinstance(value, dict) and value.get("application_type") is None:
        value["application_type"] = "web"
    result = request.app.state.gateway.oauth.register(value)
    if value.get("token_endpoint_auth_method", "none") == "none":
        request.app.state.gateway.audience_oauth.register(result["client_id"], value, dynamic=True)
    return JSONResponse({k: v for k, v in result.items() if v is not None}, status_code=201, headers=SAFE_HEADERS)


@router.post("/v1/oauth/clients", response_model=RegisteredClient, response_model_exclude_none=True, status_code=201,
             summary="Operator pre-registration; client secret returned only here")
def preregister(body: PreRegistration, request: Request, principal: Principal = Depends(require_operator)):
    result = request.app.state.gateway.oauth.register(body.model_dump(exclude={"client_id"}), client_id=body.client_id)
    return JSONResponse({k: v for k, v in result.items() if v is not None}, status_code=201, headers=SAFE_HEADERS)


@router.get("/oauth/authorize", response_class=HTMLResponse, responses=OAUTH_RESPONSES, summary="Start code + S256 consent for the MCP resource",
            openapi_extra={"parameters": [{"name": name, "in": "query", "required": name not in {"scope", "state"},
                "schema": {"type": "string"}} for name in ["client_id", "redirect_uri", "response_type", "resource",
                    "code_challenge", "code_challenge_method", "scope", "state"]]})
async def authorize(request: Request):
    params = unique(request.query_params.multi_items())
    # Existing operator requests use the mcp scope. Signed-in humans and the
    # additional resource audiences use their independent consent registry.
    scopes = params.get("scope", "").split()
    try:
        resource_path = urlsplit(params.get("resource", "")).path
    except ValueError:
        resource_path = ""
    human_resource = resource_path in {"/mcp/tenant", "/mcp/operator", "/a2a/tenant", "/a2a/operator"}
    if "mcp" not in scopes and (request.cookies.get("vtg_session") or scopes or human_resource):
        from . import oauth_audiences
        p = oauth_audiences.human(request)
        required = ("client_id", "redirect_uri", "resource", "code_challenge", "code_challenge_method")
        if any(not params.get(key) for key in required):
            raise OAuthError()
        return await oauth_audiences.authorize(request, **{key: params[key] for key in required},
            response_type=params.get("response_type", "code"), scope=params.get("scope", "*"), state=params.get("state"), p=p)
    oauth = request.app.state.gateway.oauth
    oauth.gateway.source_limiter.check(request.client.host if request.client else "unknown")
    request_id = oauth.authorize(unique(request.query_params.multi_items()))
    frontend = oauth.gateway.settings.frontend_consent_url
    if frontend:
        url = urlsplit(frontend)
        if url.scheme != "https" or not url.hostname or url.fragment or url.username:
            raise OAuthError("temporarily_unavailable", 503)
        return RedirectResponse(urlunsplit(url._replace(query=urlencode([*parse_qsl(url.query), ("request_id", request_id)]))),
                                status_code=303, headers=SAFE_HEADERS)
    return page(intro(oauth, request_id) + '<label>Operator token <input type="password" name="operator_token" '
                'required autocomplete="off"></label><button name="action" value="login">Sign in</button></form>')


@router.post("/oauth/authorize", response_class=HTMLResponse, openapi_extra=FORM_SCHEMA,
             summary="Operator sign-in and CSRF-protected HTML consent decision")
async def browser_decision(request: Request):
    pairs = await form(request)
    values = unique([(k, v) for k, v in pairs if k not in {"customer_ids", "scopes"}])
    oauth = request.app.state.gateway.oauth
    request_id = values.get("request_id", "")
    cookie_name = "oauth_consent_" + request_id
    if values.get("action") == "login":
        principal = admit_credential(oauth.gateway, request.app.state.keys,
            request.client.host if request.client else "unknown", values.get("operator_token", ""), observe=False)
        require_operator(principal)
        from .auth import observe_credential
        observe_credential(oauth.gateway, principal, request.client.host if request.client else "unknown")
        view = oauth.view(request_id)
        session, csrf = token_urlsafe(32), token_urlsafe(32)
        with oauth.gateway.db.connect(write=True) as conn:
            row = oauth.request_row(conn, request_id, pending=True)
            conn.execute("INSERT INTO oauth_browser_sessions VALUES(?,?,?,?)", (
                digest(session), request_id, digest(csrf), row["expires_at"]))
        content = intro(oauth, request_id) + f'<input type="hidden" name="csrf" value="{csrf}">'
        content += '<label>Existing agent <select name="existing_agent"><option value="">Create a new agent</option>'
        content += ''.join(f'<option value="{escape(agent)}">{escape(agent)}</option>' for agent in view["agents"]) + '</select></label>'
        content += '<p><label>New agent id <input name="agent_id" pattern="[a-z0-9][a-z0-9-]{1,47}"></label></p><fieldset><legend>Allowed customers</legend>'
        content += ''.join(f'<label><input type="checkbox" name="customer_ids" value="{escape(c)}">{escape(c)}</label>' for c in view["customer_ids"])
        content += '</fieldset><fieldset><legend>Scopes</legend>' + ''.join(
            f'<label><input type="checkbox" name="scopes" value="{escape(s)}" checked>{escape(s)}</label>' for s in view["scopes"])
        content += '</fieldset>' + consent_form() + '<p>New agents require approval for every order. Existing mandates remain in force.</p><button name="action" value="approve">Approve</button><button name="action" value="deny">Deny</button></form>'
        response = page(content)
        response.set_cookie(cookie_name, session, max_age=600, httponly=True, secure=oauth.issuer.startswith("https://"),
                            samesite="lax", path="/oauth/authorize")
        return response
    with oauth.gateway.db.connect() as conn:
        row = conn.execute("SELECT * FROM oauth_browser_sessions WHERE digest=? AND request_id=?", (
            digest(request.cookies.get(cookie_name, "")), request_id)).fetchone()
    if (row is None or datetime.fromisoformat(row["expires_at"]) <= now() or
            not compare_digest(row["csrf_digest"], digest(values.get("csrf", "")))):
        raise DomainError("OAUTH_CONSENT_CSRF", "Sign in again before deciding this request", 403)
    try:
        body = ConsentDecision(decision=values.get("action"), agent_id=values.get("existing_agent") or values.get("agent_id") or None,
            customer_ids=[v for k, v in pairs if k == "customer_ids"], scopes=[v for k, v in pairs if k == "scopes"], notifications=form_consent(values))
    except ValidationError:
        raise DomainError("VALIDATION_ERROR", "Choose a valid decision, agent id, customers and scopes", 422) from None
    result = oauth.decide(request_id, body, Principal(request.app.state.keys.operator_agent_id, operator=True))
    response = RedirectResponse(result["redirect_url"], status_code=303, headers=SAFE_HEADERS)
    response.delete_cookie(cookie_name, path="/oauth/authorize")
    return response


def consent_principal(request_id: str, request: Request, principal: Principal = Depends(resolve_principal)):
    if request_id.startswith("oar_"):
        require_operator(principal)
    else:
        request.app.state.gateway.audience_oauth.request(request_id, principal)
    return principal


@router.get("/v1/oauth/requests/{request_id}", response_model=ConsentRequest | dict,
            summary="Operator-only consent details and available agent/customer choices; no credentials")
def consent_request(request_id: str, request: Request, principal: Principal = Depends(consent_principal)):
    if not request_id.startswith("oar_"):
        from .oauth_audiences import get_request
        return JSONResponse(get_request(request_id, request, principal), headers=SAFE_HEADERS)
    require_operator(principal)
    return JSONResponse(request.app.state.gateway.oauth.view(request_id), headers=SAFE_HEADERS)


@router.post("/v1/oauth/requests/{request_id}/decision", response_model=ConsentResult | dict,
             summary="Approve/deny once; return a one-time client redirect with code or access_denied")
def consent_decision(request_id: str, body: ConsentDecision | AudienceConsentDecision, request: Request, principal: Principal = Depends(consent_principal)):
    if not request_id.startswith("oar_"):
        if not isinstance(body, AudienceConsentDecision):
            raise DomainError("VALIDATION_ERROR", "Use approve for human consent", 422)
        return JSONResponse(request.app.state.gateway.audience_oauth.decide(request_id, principal, body), headers=SAFE_HEADERS)
    require_operator(principal)
    if not isinstance(body, ConsentDecision):
        raise DomainError("VALIDATION_ERROR", "Use decision for operator consent", 422)
    return JSONResponse(request.app.state.gateway.oauth.decide(request_id, body, principal), headers=SAFE_HEADERS)


@router.post("/oauth/token", response_model=TokenResponse, openapi_extra=TOKEN_FORM_SCHEMA, responses=OAUTH_RESPONSES,
             summary="Exchange code + verifier or rotate refresh token; resource is required")
async def token(request: Request):
    request.app.state.gateway.source_limiter.check(request.client.host if request.client else "unknown")
    values = unique(await form(request))
    if values.get("code", "").startswith("vtg_code_") or values.get("refresh_token", "").startswith("vtg_oauth_"):
        from .oauth_audiences import secure_origin
        secure_origin(request)
        result = request.app.state.gateway.audience_oauth.token(values)
    else:
        result = request.app.state.gateway.oauth.token(values, request.headers.get("authorization"))
    return JSONResponse(result, headers=SAFE_HEADERS)


def install(app):
    app.include_router(router)
    from . import oauth_audiences
    shared = {(route.path, method) for route in router.routes for method in route.methods}
    for route in oauth_audiences.router.routes:
        if not any((route.path, method) in shared for method in route.methods):
            app.router.routes.append(route)
    from .accounts import origin
    def resource_metadata(resource_path):
        def metadata(request: Request):
            base = origin(request)
            return {"resource": base + resource_path, "authorization_servers": [base],
                "scopes_supported": AUDIENCE_SCOPES, "bearer_methods_supported": ["header"]}
        return metadata

    for resource_path in oauth_audiences.RESOURCE_PATHS:
        if resource_path != "/mcp":
            app.add_api_route("/.well-known/oauth-protected-resource" + resource_path, resource_metadata(resource_path), methods=["GET"], response_model=ResourceMetadata)

    @app.exception_handler(oauth_audiences.OAuthError)
    async def audience_error(request, error):
        return JSONResponse({"error": error.code, "error_description": error.description}, status_code=error.status, headers=SAFE_HEADERS)

    @app.exception_handler(OAuthError)
    async def oauth_error(request, error):
        if getattr(error, "redirect_url", None):
            return RedirectResponse(error.redirect_url, status_code=303, headers=SAFE_HEADERS)
        return JSONResponse({"error": error.error}, status_code=error.status,
                            headers={**SAFE_HEADERS, **({"WWW-Authenticate": 'Basic realm="oauth"'} if error.status == 401 else {})})
