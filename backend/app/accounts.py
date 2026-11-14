"""Human accounts, server-side sessions, tenant administration and REST audiences."""
import hashlib
import json
import os
import logging
import re
from dataclasses import replace
from datetime import datetime, timedelta
from secrets import compare_digest, token_bytes, token_urlsafe
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from . import audit
from .auth import Principal, resolve_principal
from .db import DomainError
from .tenancy import HOTEL, HOSPITAL

router = APIRouter(tags=["accounts"])
AUDIENCES = {
    "buyer": {"rest": "/v1", "human_required": False},
    "tenant": {"rest": "/v1/tenant", "human_required": True},
    "operator": {"rest": "/v1/operator", "human_required": True},
}
ROLES = {
    "viewer": ["orders:read", "evidence:read", "audit:read", "spend:read", "suggestions:read"],
    "approver": ["orders:read", "evidence:read", "audit:read", "spend:read", "suggestions:read", "approvals:decide", "orders:cancel"],
    "tenant_admin": ["orders:read", "evidence:read", "audit:read", "spend:read", "suggestions:read", "approvals:decide",
                     "orders:cancel", "users:manage", "agents:manage", "connections:manage", "mandates:manage", "policy:manage"],
    "platform_admin": ["platform:*", "tenants:manage", "users:manage", "connections:manage", "approvals:decide"],
}
DEMO_USERS = [
    ("aurora-director", HOTEL, "director@aurora.example", "tenant_admin", "DEMO_HOTEL_ADMIN_PASSWORD"),
    ("aurora-chef", HOTEL, "chef@aurora.example", "approver", "DEMO_HOTEL_APPROVER_PASSWORD"),
    ("aurora-finance", HOTEL, "finance@aurora.example", "viewer", "DEMO_HOTEL_VIEWER_PASSWORD"),
    ("kaari-manager", HOSPITAL, "manager@kaari.example", "tenant_admin", "DEMO_HOSPITAL_ADMIN_PASSWORD"),
    ("kaari-dietitian", HOSPITAL, "dietitian@kaari.example", "approver", "DEMO_HOSPITAL_APPROVER_PASSWORD"),
    ("valio-admin", None, "staff@valio.example", "platform_admin", "DEMO_PLATFORM_ADMIN_PASSWORD"),
]


class SecretLinkFilter(logging.Filter):
    def filter(self, record):
        text = record.getMessage()
        safe = re.sub(r"(/v1/auth/(?:invites|password-resets)/)[^/\s]+", r"\1[redacted]", text)
        safe = re.sub(r"vtg_(?:live|sbx)_con_[a-z0-9]+_[A-Za-z0-9_-]+", "[connection-key]", safe)
        if safe != text:
            record.msg, record.args = safe, ()
        return True


for _logger in ("httpx", "httpx2", "uvicorn.access"):
    logging.getLogger(_logger).addFilter(SecretLinkFilter())


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def hash_password(password):
    salt = token_bytes(16)
    result = hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64*1024*1024)
    return "scrypt$32768$8$1$" + salt.hex() + "$" + result.hex()


def password_matches(password, encoded):
    try:
        _, n, r, p, salt, expected = encoded.split("$")
        result = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), maxmem=64*1024*1024)
        return compare_digest(result, bytes.fromhex(expected))
    except (ValueError, AttributeError, TypeError):
        return False


def public_user(row):
    return {k: row[k] for k in ("id", "tenant_id", "email", "role", "created_at", "disabled_at", "last_sign_in", "invited_by")} | {
        "status": "removed" if row["removed_at"] else "disabled" if row["disabled_at"] else "active" if row["password_hash"] else "invited"}


def seed_demo(db):
    with db.connect(write=True) as conn:
        for uid, tid, email, role, env in DEMO_USERS:
            password = os.environ.get(env)
            encoded = hash_password(password) if password else None
            conn.execute("INSERT OR IGNORE INTO users(id,tenant_id,email,role,password_hash,created_at) VALUES(?,?,?,?,?,?)",
                         (uid, tid, email, role, encoded, audit.utcnow().isoformat()))
            if encoded:
                conn.execute("UPDATE users SET password_hash=? WHERE id=? AND password_hash IS NULL", (encoded, uid))
        for agent, tid in [("hotel-chain-agent", HOTEL), ("web-console", HOTEL), ("hospital-kitchen-agent", HOSPITAL)]:
            conn.execute("INSERT OR IGNORE INTO tenant_agents VALUES(?,?,?,NULL)", (agent, tid, agent))
        from .tenant_api import issuer_key
        for row in conn.execute("SELECT id FROM tenants"):
            issuer_key(conn, row[0])


class Accounts:
    def __init__(self, gateway):
        self.gateway, self.db = gateway, gateway.db
        self.dummy_password = hash_password(token_urlsafe(32))
        seed_demo(self.db)
        with self.db.connect(write=True) as conn:
            policy = gateway.policy.get(tenant_overlay=False)
            for customer in set(policy.customers) | {c for mandate in policy.agents.values() for c in mandate.customer_ids}:
                conn.execute("INSERT OR IGNORE INTO tenant_customers VALUES(?,?)", (customer, HOSPITAL if customer == "2" else HOTEL))
            for agent in set(gateway.policy.get().agents) | set(gateway.settings.keys.agent_ids if gateway.settings.keys else ()):
                mandate = policy.agents.get(agent)
                tid = HOSPITAL if agent == "hospital-kitchen-agent" or mandate and set(mandate.customer_ids) == {"2"} else HOTEL
                conn.execute("INSERT OR IGNORE INTO tenant_agents VALUES(?,?,?,NULL)",
                             (agent, tid, agent))
            conn.execute("INSERT OR IGNORE INTO tenant_agents VALUES(?,?,?,NULL)", ("hospital-kitchen-agent", HOSPITAL, "Hospital kitchen"))

    def customers(self, tid):
        with self.db.connect() as conn:
            return tuple(r[0] for r in conn.execute("SELECT customer_id FROM tenant_customers WHERE tenant_id=? ORDER BY customer_id", (tid,)))

    def bind(self, p):
        # Existing supplier identities publish consent-filtered campaigns across
        # buyers; their domain guards remain authoritative (spec 36).
        if p.kind == "supplier_agent" and not p.connection_id:
            return p
        if p.operator or p.user_id == "legacy-operator":
            return replace(p, kind="platform_admin", operator=True, role="platform_admin", user_id=p.user_id or "legacy-operator", tenant_id=None)
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM tenant_agents WHERE agent_id=?", (p.id,)).fetchone()
            tid = p.tenant_id or (row["tenant_id"] if row else HOSPITAL if p.id == "hospital-kitchen-agent" else HOTEL)
            if p.kind == "buyer_user" and p.customer_ids and set(p.customer_ids) == {"2"}:
                tid = HOSPITAL
            org = conn.execute("SELECT disabled_at FROM tenants WHERE id=?", (tid,)).fetchone()
        if row and row["disabled_at"] or org and org[0]:
            raise DomainError("UNAUTHENTICATED", "Account is disabled; contact its administrator", 401)
        if p.user_id:
            user = self.user(p.user_id, active=True)
            if user["tenant_id"] != tid and user["role"] != "platform_admin":
                raise DomainError("UNAUTHENTICATED", "Connection no longer belongs to this account", 401)
            p = replace(p, kind="platform_admin" if user["role"] == "platform_admin" else "tenant_user",
                        operator=user["role"] == "platform_admin", role=user["role"])
            if p.operator:
                return replace(p, tenant_id=None, organisation_id=None, customer_ids=())
        allowed = set(p.customer_ids) if p.connection_id or p.kind in {"agent", "buyer_user"} else set(self.customers(tid))
        allowed &= set(self.customers(tid))
        mandate = self.gateway.policy.get().agents.get(p.id)
        if p.kind == "agent" and mandate:
            allowed &= set(mandate.customer_ids)
        elif p.kind == "agent" and p.connection_id:
            from .mandates import decode
            with self.db.connect() as conn:
                subject = next((claims["vc"]["credentialSubject"] for r in conn.execute("SELECT jwt FROM mandate_credentials ORDER BY last_used DESC")
                                if (claims := decode(r[0])[1])["sub"] == p.id), None)
            if subject:
                allowed &= {subject["customer_id"]}
        return replace(p, tenant_id=tid, organisation_id=p.organisation_id or tid, customer_ids=tuple(sorted(allowed)))

    def user(self, uid, *, active=False):
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not row or row["removed_at"] or active and row["disabled_at"]:
            if active:
                raise DomainError("UNAUTHENTICATED", "User is unavailable", 401)
            raise DomainError("USER_NOT_FOUND", "User is unavailable", 404)
        if active and row["tenant_id"]:
            with self.db.connect() as conn:
                tenant = conn.execute("SELECT disabled_at FROM tenants WHERE id=?", (row["tenant_id"],)).fetchone()
            if not tenant or tenant[0]:
                raise DomainError("UNAUTHENTICATED", "Tenant is unavailable", 401)
        return row

    def user_principal(self, row, **kwargs):
        return Principal(row["id"], operator=row["role"] == "platform_admin",
                         kind="platform_admin" if row["role"] == "platform_admin" else "tenant_user",
                         tenant_id=row["tenant_id"], organisation_id=row["tenant_id"], user_id=row["id"],
                         role=row["role"], customer_ids=self.customers(row["tenant_id"]), **kwargs)

    def session(self, secret):
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE digest=? AND expires_at>?", (digest(secret), audit.utcnow().isoformat())).fetchone()
        if row is None:
            raise DomainError("UNAUTHENTICATED", "Sign in again; session expired or ended", 401)
        return self.user_principal(self.user(row["user_id"], active=True), credential_ref="session-user:" + row["user_id"])

    def csrf(self, session, token):
        with self.db.connect() as conn:
            row = conn.execute("SELECT csrf_digest FROM sessions WHERE digest=?", (digest(session),)).fetchone()
        if not row or not compare_digest(row[0], digest(token)):
            raise DomainError("CSRF_REQUIRED", "Send the sign-in CSRF token in X-CSRF-Token", 403)

    def login(self, email, password, source):
        email = email.strip().lower()
        now = audit.utcnow()
        buckets = ["email:" + digest(email), "address:" + digest(source)]
        with self.db.connect(write=True) as conn:
            for bucket in buckets:
                row = conn.execute("SELECT * FROM login_attempts WHERE bucket=?", (bucket,)).fetchone()
                if row and row["reset_at"] > now.isoformat() and row["count"] >= (5 if bucket.startswith("email:") else 30):
                    raise DomainError("LOGIN_THROTTLED", "Too many sign-in attempts; retry later", 429, {"retry_after_seconds": 900})
            row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            valid = password_matches(password, row["password_hash"] if row and row["password_hash"] else self.dummy_password)
            valid = valid and row is not None and not row["disabled_at"] and not row["removed_at"]
            if valid and row["tenant_id"]:
                org = conn.execute("SELECT disabled_at FROM tenants WHERE id=?", (row["tenant_id"],)).fetchone()
                valid = bool(org and not org[0])
            for bucket in buckets:
                conn.execute("INSERT INTO login_attempts VALUES(?,1,?) ON CONFLICT(bucket) DO UPDATE SET "
                             "count=CASE WHEN reset_at<=? THEN 1 ELSE count+1 END,reset_at=CASE WHEN reset_at<=? THEN excluded.reset_at ELSE reset_at END",
                             (bucket, (now+timedelta(minutes=15)).isoformat(), now.isoformat(), now.isoformat()))
            audit.append(conn, "authentication", "LOGIN_SUCCESS" if valid else "LOGIN_FAILED", "Human sign-in attempt.",
                         data={"email_digest": digest(email), "tenant_id": row["tenant_id"] if row else None})
            if valid:
                conn.execute("DELETE FROM login_attempts WHERE bucket=?", (buckets[0],))
                secret, csrf = token_urlsafe(32), token_urlsafe(32)
                conn.execute("INSERT INTO sessions VALUES(?,?,?,?)", (digest(secret), row["id"], digest(csrf), (now+timedelta(hours=12)).isoformat()))
                conn.execute("UPDATE users SET last_sign_in=? WHERE id=?", (now.isoformat(), row["id"]))
        if not valid:
            raise DomainError("UNAUTHENTICATED", "Email or password is incorrect", 401)
        return secret, csrf, self.user_principal(row)

    def guard(self, p, path, method):
        target = "tenant" if path.startswith("/v1/tenant") else "operator" if path.startswith("/v1/operator") else "buyer"
        if path.startswith("/v1/auth") or path == "/v1/me":
            return
        if target != "buyer":
            if p.kind == "agent":
                raise DomainError("HUMAN_REQUIRED", "This audience requires a human account", 403)
            if p.audience not in {"legacy", target}:
                raise DomainError("WRONG_AUDIENCE", "Credential is bound to another audience", 401)
            if target == "operator" and not p.operator:
                raise DomainError("OPERATOR_REQUIRED", "Platform administrator required", 403)
        elif p.connection_id and p.audience != "buyer":
            raise DomainError("WRONG_AUDIENCE", "Credential is bound to another audience", 401)
        if p.connection_id and "*" not in p.scopes:
            permission = "read" if method in {"GET", "HEAD"} else "write"
            segments = path.split("/")
            resource = next((name for name in ("mandates", "approvals", "connections", "orders", "quotes", "lookups", "basket-lookups", "agents", "users", "policy", "tenants", "audit", "suggestions", "spend") if name in segments), segments[-1])
            if resource in {"lookups", "basket-lookups", "offers"}:
                resource = "lookup"
            from .auth import check_scope
            check_scope(p, resource, permission, path.rstrip("/").split("/")[-1])

    def guard_target(self, request, p):
        """Hide foreign targets before validating a mutation's body, on every alias."""
        if p.operator or not p.tenant_id:
            return
        path, params = request.url.path, request.path_params
        target = None
        for fragment, parameter, table, key in [
            ("/orders/", "order_id", "orders", "id"), ("/approvals/", "order_id", "orders", "id"),
            ("/orders/", "id", "orders", "id"), ("/quotes/", "id", "quotes", "id"),
            ("/lookups/", "lookup_id", "lookups", "id"), ("/basket-lookups/", "lookup_id", "lookups", "id"),
            ("/connections/", "connection_id", "connections", "connection_id"),
            ("/agents/", "agent_id", "tenant_agents", "agent_id"), ("/users/", "user_id", "users", "id"),
            ("/recurring-orders/", "recurring_id", "recurring_orders", "id"),
            ("/proposals/", "proposal_id", "proposals", "id"),
        ]:
            if fragment in path and parameter in params:
                target = table, key, params[parameter]
                break
        if target:
            table, key, value = target
            with self.db.connect() as conn:
                row = conn.execute(f"SELECT tenant_id FROM main.{table} WHERE {key}=?", (value,)).fetchone()
            if row is not None and row[0] != p.tenant_id:
                errors = {"orders": DomainError("ORDER_NOT_FOUND", "Object was not found", 404),
                          "quotes": DomainError("QUOTE_NOT_FOUND", "Object was not found", 404),
                          "lookups": DomainError("LOOKUP_NOT_FOUND", "Object was not found", 404),
                          "tenant_agents": DomainError("AGENT_NOT_FOUND", "Object was not found", 404),
                          "users": DomainError("USER_NOT_FOUND", "Object was not found", 404),
                          "connections": DomainError("CONNECTION_NOT_FOUND", "Object was not found", 404)}
                raise errors.get(table, DomainError("HTTP_ERROR", "Object was not found", 404))

    def manage(self, p, *, platform=False):
        if platform:
            if not p.operator:
                raise DomainError("OPERATOR_REQUIRED", "Platform administrator required", 403)
        elif not p.operator and (p.kind != "tenant_user" or p.role != "tenant_admin"):
            raise DomainError("TENANT_ADMIN_REQUIRED", "Tenant administrator required", 403)

    def target_user(self, uid, p, platform=False):
        self.manage(p, platform=platform)
        row = self.user(uid)
        if (platform and row["tenant_id"] is not None) or (not platform and row["tenant_id"] != p.tenant_id):
            raise DomainError("USER_NOT_FOUND", "User was not found", 404)
        return row

    def link(self, conn, uid, kind, origin):
        secret = token_urlsafe(32)
        conn.execute("UPDATE account_links SET used_at=? WHERE user_id=? AND kind=? AND used_at IS NULL", (audit.utcnow().isoformat(), uid, kind))
        expires = audit.utcnow() + timedelta(days=7 if kind == "invite" else 1)
        conn.execute("INSERT INTO account_links VALUES(?,?,?,?,NULL)", (digest(secret), uid, kind, expires.isoformat()))
        endpoint = "invites" if kind == "invite" else "password-resets"
        return {"user_id": uid, "expires_at": expires, "link": f"{origin}/v1/auth/{endpoint}/{secret}/accept", "token": secret}

    def invite(self, email, role, p, origin, *, tenant_id=None, platform=False):
        self.manage(p, platform=platform)
        tid = None if platform else tenant_id or p.tenant_id
        allowed = {"platform_admin"} if platform else {"tenant_admin", "approver", "viewer"}
        if role not in allowed:
            raise DomainError("VALIDATION_ERROR", "Role is not valid for this team", 422)
        if tid:
            with self.db.connect() as conn:
                if not conn.execute("SELECT 1 FROM tenants WHERE id=? AND disabled_at IS NULL", (tid,)).fetchone():
                    raise DomainError("TENANT_NOT_FOUND", "Tenant was not found", 404)
        uid = "usr_" + token_urlsafe(12)
        with self.db.connect(write=True) as conn:
            if conn.execute("SELECT 1 FROM users WHERE email=?", (email.strip().lower(),)).fetchone():
                raise DomainError("EMAIL_EXISTS", "Email already has an account", 409)
            conn.execute("INSERT INTO users(id,tenant_id,email,role,created_at,invited_by) VALUES(?,?,?,?,?,?)",
                         (uid, tid, email.strip().lower(), role, audit.utcnow().isoformat(), p.user_id or p.id))
            result = self.link(conn, uid, "invite", origin)
            audit.append(conn, p.user_id or p.id, "USER_INVITED", "Invited a team member.", data={"user_id": uid, "tenant_id": tid, "role": role})
        return result

    def accept(self, token, password, kind):
        with self.db.connect(write=True) as conn:
            row = conn.execute("SELECT * FROM account_links WHERE digest=? AND kind=?", (digest(token), kind)).fetchone()
            if not row or row["used_at"] or row["expires_at"] <= audit.utcnow().isoformat():
                raise DomainError("LINK_EXPIRED", "Link expired or already used; request a new link", 410)
            user = self.user(row["user_id"], active=True)
            conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(password), user["id"]))
            conn.execute("UPDATE account_links SET used_at=? WHERE digest=?", (audit.utcnow().isoformat(), digest(token)))
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
            audit.append(conn, user["id"], "PASSWORD_SET", "Account password set.", data={"tenant_id": user["tenant_id"], "user_id": user["id"]})
        return {"user_id": user["id"], "role": user["role"]}

    def change_user(self, uid, changes, p, platform=False):
        with self.db.connect(write=True) as conn:
            row = self.target_user(uid, p, platform)
            role = changes.get("role", row["role"])
            if role not in ({"platform_admin"} if platform else {"tenant_admin", "approver", "viewer"}):
                raise DomainError("VALIDATION_ERROR", "Role is not valid for this team", 422)
            if row["role"] == "tenant_admin" and row["password_hash"] and not row["disabled_at"] and (role != "tenant_admin" or changes.get("disabled") or changes.get("remove")):
                count = conn.execute("SELECT COUNT(*) FROM users WHERE tenant_id=? AND role='tenant_admin' AND password_hash IS NOT NULL AND disabled_at IS NULL AND removed_at IS NULL", (row["tenant_id"],)).fetchone()[0]
                if count <= 1:
                    raise DomainError("LAST_TENANT_ADMIN", "Keep at least one active tenant administrator", 409)
            disabled = (audit.utcnow().isoformat() if changes["disabled"] else None) if "disabled" in changes else row["disabled_at"]
            conn.execute("UPDATE users SET role=?,disabled_at=?,removed_at=? WHERE id=?", (role, disabled, audit.utcnow().isoformat() if changes.get("remove") else None, uid))
            if disabled or changes.get("remove"):
                conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
                conn.execute("UPDATE connections SET revoked_at=? WHERE created_by=? AND revoked_at IS NULL", (audit.utcnow().isoformat(), uid))
                conn.execute("UPDATE account_links SET used_at=? WHERE user_id=? AND used_at IS NULL", (audit.utcnow().isoformat(), uid))
            audit.append(conn, p.user_id or p.id, "ROLE_CHANGED" if role != row["role"] else "USER_CHANGED", "Changed team membership.",
                         data={"user_id": uid, "tenant_id": row["tenant_id"], "role": role, "disabled": bool(disabled), "removed": bool(changes.get("remove"))})
            return public_user(conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())


class Login(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(max_length=1024, repr=False)


class SetPassword(BaseModel):
    password: str = Field(min_length=10, max_length=1024, repr=False)


class Invite(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    role: str


class UserChange(BaseModel):
    role: str | None = None
    disabled: bool | None = None


class TenantRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    customer_ids: list[str] = Field(default_factory=list)


class TenantChange(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    customer_ids: list[str] | None = None
    disabled: bool | None = None


class PublicUser(BaseModel):
    id: str
    tenant_id: str | None
    email: str
    role: str
    created_at: str
    disabled_at: str | None
    last_sign_in: str | None
    invited_by: str | None
    status: Literal["active", "invited", "disabled", "removed"]


class SessionResponse(BaseModel):
    user: PublicUser | None
    role: str
    tenant_id: str | None
    tenant: dict | None = None
    customer_ids: list[str]
    permissions: list[str] = Field(default_factory=list)


class LoginResponse(SessionResponse):
    csrf_token: str = Field(repr=False)


class LinkResponse(BaseModel):
    user_id: str
    expires_at: datetime
    link: str = Field(repr=False, description="Single-use link returned only on issue/resend; store privately.")
    token: str = Field(repr=False)


def tenant_data(a, tid):
    if not tid:
        return None
    with a.db.connect() as conn:
        row = conn.execute("SELECT * FROM tenants WHERE id=?", (tid,)).fetchone()
    return dict(row) if row else None


def accounts(request):
    return request.app.state.gateway.accounts


def origin(request):
    return request.app.state.gateway.settings.public_url or str(request.base_url).rstrip("/")


@router.post("/v1/auth/login", response_model=LoginResponse)
def login(body: Login, request: Request, response: Response):
    a = accounts(request)
    secret, csrf, p = a.login(body.email, body.password, request.client.host if request.client else "unknown")
    response.set_cookie("vtg_session", secret, httponly=True, secure=request.url.hostname not in {"localhost", "127.0.0.1", "::1"}, samesite="lax", max_age=43200)
    response.headers["Cache-Control"] = "no-store"
    return {"csrf_token": csrf, "user": public_user(a.user(p.user_id)), "role": p.role, "tenant_id": p.tenant_id,
            "tenant": tenant_data(a, p.tenant_id), "customer_ids": p.customer_ids, "permissions": ROLES.get(p.role, [])}


@router.get("/v1/auth/session", response_model=SessionResponse)
def session(request: Request, p: Principal = Depends(resolve_principal)):
    a = accounts(request)
    return {"user": public_user(a.user(p.user_id)) if p.user_id and p.user_id != "legacy-operator" else None,
            "role": p.role, "tenant_id": p.tenant_id, "tenant": tenant_data(a, p.tenant_id), "customer_ids": p.customer_ids, "permissions": ROLES.get(p.role, [])}


@router.post("/v1/auth/logout", response_model=dict)
def logout(request: Request, response: Response, p: Principal = Depends(resolve_principal)):
    with accounts(request).db.connect(write=True) as conn:
        conn.execute("DELETE FROM sessions WHERE digest=?", (digest(request.cookies.get("vtg_session", "")),))
    response.delete_cookie("vtg_session")
    return {"signed_out": True}


@router.post("/v1/auth/invites/{token}/accept", response_model=dict)
def accept_invite(token: str, body: SetPassword, request: Request):
    return accounts(request).accept(token, body.password, "invite")


@router.post("/v1/auth/password-resets/{token}/accept", response_model=dict)
def accept_reset(token: str, body: SetPassword, request: Request):
    return accounts(request).accept(token, body.password, "reset")


@router.get("/v1/tenant/roles", response_model=list[dict])
def roles(p: Principal = Depends(resolve_principal)):
    return [{"role": role, "permissions": permissions} for role, permissions in ROLES.items() if role != "platform_admin"]


def install_team_routes(prefix, platform):
    @router.get(prefix, response_model=list[PublicUser], name=("platform" if platform else "tenant") + "_users")
    def users(request: Request, p: Principal = Depends(resolve_principal)):
        a = accounts(request)
        a.manage(p, platform=platform)
        with a.db.connect() as conn:
            rows = conn.execute("SELECT * FROM users WHERE tenant_id IS ? AND removed_at IS NULL ORDER BY email", (None if platform else p.tenant_id,))
            return [public_user(r) for r in rows]

    @router.post(prefix, response_model=LinkResponse, status_code=201, name=("platform" if platform else "tenant") + "_invite")
    def invite(body: Invite, request: Request, p: Principal = Depends(resolve_principal)):
        return accounts(request).invite(body.email, body.role, p, origin(request), platform=platform)

    @router.patch(prefix + "/{user_id}", response_model=PublicUser, name=("platform" if platform else "tenant") + "_change_user")
    def change(user_id: str, body: UserChange, request: Request, p: Principal = Depends(resolve_principal)):
        return accounts(request).change_user(user_id, body.model_dump(exclude_none=True), p, platform)

    @router.delete(prefix + "/{user_id}", name=("platform" if platform else "tenant") + "_remove_user", response_model=dict)
    def remove(user_id: str, request: Request, p: Principal = Depends(resolve_principal)):
        return accounts(request).change_user(user_id, {"remove": True}, p, platform)

    @router.get(prefix + "/{user_id}", response_model=PublicUser, name=("platform" if platform else "tenant") + "_user")
    def get(user_id: str, request: Request, p: Principal = Depends(resolve_principal)):
        return public_user(accounts(request).target_user(user_id, p, platform))

    @router.post(prefix + "/{user_id}/resend-invite", response_model=LinkResponse, name=("platform" if platform else "tenant") + "_resend_invite")
    def resend(user_id: str, request: Request, p: Principal = Depends(resolve_principal)):
        a = accounts(request)
        user = a.target_user(user_id, p, platform)
        if user["password_hash"]:
            raise DomainError("ALREADY_ACTIVE", "User already accepted its invitation", 409)
        with a.db.connect(write=True) as conn:
            conn.execute("UPDATE users SET disabled_at=NULL WHERE id=?", (user_id,))
            result = a.link(conn, user_id, "invite", origin(request))
            audit.append(conn, p.user_id or p.id, "INVITE_RESENT", "Replaced invitation.", data={"user_id": user_id, "tenant_id": user["tenant_id"]})
            return result

    @router.post(prefix + "/{user_id}/cancel-invite", name=("platform" if platform else "tenant") + "_cancel_invite", response_model=dict)
    def cancel(user_id: str, request: Request, p: Principal = Depends(resolve_principal)):
        a = accounts(request)
        user = a.target_user(user_id, p, platform)
        if user["password_hash"]:
            raise DomainError("ALREADY_ACTIVE", "User already accepted its invitation", 409)
        with a.db.connect(write=True) as conn:
            conn.execute("UPDATE account_links SET used_at=? WHERE user_id=?", (audit.utcnow().isoformat(), user_id))
            conn.execute("UPDATE users SET disabled_at=? WHERE id=?", (audit.utcnow().isoformat(), user_id))
            audit.append(conn, p.user_id or p.id, "INVITE_CANCELLED", "Cancelled invitation.", data={"user_id": user_id, "tenant_id": user["tenant_id"]})
        return {"cancelled": True}

    @router.post(prefix + "/{user_id}/password-reset", response_model=LinkResponse, name=("platform" if platform else "tenant") + "_password_reset")
    def reset(user_id: str, request: Request, p: Principal = Depends(resolve_principal)):
        a = accounts(request)
        user = a.target_user(user_id, p, platform)
        with a.db.connect(write=True) as conn:
            result = a.link(conn, user_id, "reset", origin(request))
            audit.append(conn, p.user_id or p.id, "PASSWORD_RESET_ISSUED", "Issued a password reset link.", data={"user_id": user_id, "tenant_id": user["tenant_id"]})
            return result


install_team_routes("/v1/tenant/users", False)
install_team_routes("/v1/operator/users", True)


@router.get("/v1/operator/tenants", response_model=list[dict])
def tenants(request: Request, p: Principal = Depends(resolve_principal)):
    a = accounts(request)
    a.manage(p, platform=True)
    with a.db.connect() as conn:
        return [{**dict(r), "customer_ids": a.customers(r["id"])} for r in conn.execute("SELECT * FROM tenants ORDER BY name")]


@router.post("/v1/operator/tenants", status_code=201, response_model=dict)
def create_tenant(body: TenantRequest, request: Request, p: Principal = Depends(resolve_principal)):
    a = accounts(request)
    a.manage(p, platform=True)
    tid = "ten_" + token_urlsafe(12)
    with a.db.connect(write=True) as conn:
        conn.execute("INSERT INTO tenants VALUES(?,?,?,NULL)", (tid, body.name, audit.utcnow().isoformat()))
        from .tenant_api import issuer_key
        issuer_key(conn, tid)
        for customer in body.customer_ids:
            if conn.execute("SELECT 1 FROM tenant_customers WHERE customer_id=?", (customer,)).fetchone():
                raise DomainError("CUSTOMER_ASSIGNED", "Customer already belongs to a tenant; use explicit move", 409)
            conn.execute("INSERT INTO tenant_customers VALUES(?,?)", (customer, tid))
        audit.append(conn, p.user_id or p.id, "TENANT_CREATED", "Created a buyer tenant.", data={"tenant_id": tid})
    return {"id": tid, "name": body.name, "customer_ids": body.customer_ids}


@router.patch("/v1/operator/tenants/{tenant_id}", response_model=dict)
def change_tenant(tenant_id: str, body: TenantChange, request: Request, p: Principal = Depends(resolve_principal)):
    a = accounts(request)
    a.manage(p, platform=True)
    with a.db.connect(write=True) as conn:
        row = conn.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        if not row:
            raise DomainError("TENANT_NOT_FOUND", "Tenant was not found", 404)
        disabled = audit.utcnow().isoformat() if body.disabled else None
        conn.execute("UPDATE tenants SET name=?,disabled_at=? WHERE id=?", (body.name or row["name"], disabled if body.disabled is not None else row["disabled_at"], tenant_id))
        if body.customer_ids is not None:
            conn.execute("DELETE FROM tenant_customers WHERE tenant_id=?", (tenant_id,))
            for customer in body.customer_ids:
                conn.execute("INSERT INTO tenant_customers VALUES(?,?) ON CONFLICT(customer_id) DO UPDATE SET tenant_id=excluded.tenant_id", (customer, tenant_id))
        if body.disabled:
            conn.execute("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE tenant_id=?)", (tenant_id,))
            conn.execute("UPDATE connections SET revoked_at=? WHERE tenant_id=? AND revoked_at IS NULL", (disabled, tenant_id))
        audit.append(conn, p.user_id or p.id, "TENANT_CHANGED", "Changed buyer tenant.", data={"tenant_id": tenant_id})
        return {**dict(conn.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()), "customer_ids": a.customers(tenant_id)}


@router.post("/v1/operator/tenants/{tenant_id}/users", status_code=201, response_model=dict)
def invite_tenant_admin(tenant_id: str, body: Invite, request: Request, p: Principal = Depends(resolve_principal)):
    accounts(request).manage(p, platform=True)
    return accounts(request).invite(body.email, body.role, p, origin(request), tenant_id=tenant_id)
