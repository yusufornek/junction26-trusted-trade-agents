"""Tenant operations and the REST human confirmation boundary."""
import json
import math
from datetime import timedelta
from secrets import token_urlsafe

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from . import audit
from .accounts import accounts, digest
from .auth import Principal, resolve_principal, require_reviewer
from .autonomy import authorize_review
from .db import DomainError
from .models import ApprovalRequest, AuditEntry, CancelRequest, Order

router = APIRouter(tags=["tenant operations"])


class DecisionPreview(BaseModel):
    confirmation_required: bool = True
    confirmation_token: str = Field(repr=False)
    expires_at: str
    summary: dict


def confirm_rest(service, p, operation, target, payload, token, apply, *, force=False):
    try:
        return _confirm_rest(service, p, operation, target, payload, token, apply, force=force)
    except DomainError as error:
        if operation in {"approve", "reject", "cancel"} and error.code == "ORDER_NOT_FOUND" and not getattr(error, "audited", False):
            actor = "human:operator" if p.user_id == "legacy-operator" else "human:" + (p.user_id or p.id)
            with service.db.connect(write=True) as conn:
                audit.append(conn, actor, operation.upper()+"_FAILED", error.message, order_id=target,
                    data={"error": error.code, "inputs": {"order_id": target, **payload}, "live_calls": {"price": 0, "availability": 0}})
            error.audited = True
        raise


def _confirm_rest(service, p, operation, target, payload, token, apply, *, force=False):
    """Atomically bind one human confirmation to target, body and evidence revision."""
    if not force and not (p.operator or p.kind in {"tenant_user", "platform_admin", "buyer_user"}):
        return apply()
    user = p.user_id or p.id
    encoded = json.dumps(payload, sort_keys=True, default=str)
    error = None
    with service.db.connect(write=True) as conn:
        order = None
        if operation in {"approve", "reject", "cancel"}:
            order = service.orders._order(conn, target)
            authorize_review(service.policy.get(), p, order)
        revision = order.evidence_revision if order else None
        if token is None:
            secret = token_urlsafe(32)
            expires = audit.utcnow() + timedelta(minutes=5)
            conn.execute("INSERT INTO decision_confirmations VALUES(?,?,?,?,?,?,?,?,NULL)",
                         (digest(secret), user, p.tenant_id, operation, target, encoded, revision, expires.isoformat()))
            return DecisionPreview(confirmation_token=secret, expires_at=expires.isoformat(),
                summary={"operation": operation, "target": target, "order": order.model_dump(mode="json") if order else None, "changes": payload})
        row = conn.execute("SELECT * FROM decision_confirmations WHERE digest=?", (digest(token),)).fetchone()
        if (not row or row["used_at"] or row["expires_at"] <= audit.utcnow().isoformat() or
                row["user_id"] != user or row["tenant_id"] != p.tenant_id or row["operation"] != operation or
                row["target"] != target or row["payload"] != encoded):
            raise DomainError("CONFIRMATION_INVALID", "Confirmation expired, used, or belongs to another decision", 409)
        conn.execute("UPDATE decision_confirmations SET used_at=? WHERE digest=? AND used_at IS NULL", (audit.utcnow().isoformat(), digest(token)))
        if row["revision"] != revision:
            error = DomainError("EVIDENCE_CHANGED", "Review the updated evidence and confirm again", 409, {"evidence_revision": revision})
        else:
            try:
                result = apply()
            except DomainError as caught:
                error = caught
    if error:
        raise error
    return result


class AgentCreate(BaseModel):
    agent_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,47}$")
    name: str = Field(min_length=1, max_length=200)


class AgentChange(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    disabled: bool | None = None


def target_agent(service, agent_id, p):
    service.accounts.manage(p)
    with service.db.connect() as conn:
        row = conn.execute("SELECT * FROM tenant_agents WHERE agent_id=?", (agent_id,)).fetchone()
    if not row or not p.operator and row["tenant_id"] != p.tenant_id:
        raise DomainError("AGENT_NOT_FOUND", "Agent was not found", 404)
    return row


@router.get("/v1/tenant/agents", response_model=list[dict])
def agents(request: Request, p: Principal = Depends(resolve_principal)):
    a = accounts(request)
    a.manage(p)
    with a.db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM tenant_agents WHERE tenant_id=? ORDER BY agent_id", (p.tenant_id,))]


@router.post("/v1/tenant/agents", status_code=201, response_model=dict)
def create_agent(body: AgentCreate, request: Request, p: Principal = Depends(resolve_principal)):
    a = accounts(request)
    a.manage(p)
    with a.db.connect(write=True) as conn:
        if conn.execute("SELECT 1 FROM tenant_agents WHERE agent_id=?", (body.agent_id,)).fetchone():
            raise DomainError("AGENT_EXISTS", "Agent identifier is already registered", 409)
        conn.execute("INSERT INTO tenant_agents VALUES(?,?,?,NULL)", (body.agent_id, p.tenant_id, body.name))
        audit.append(conn, p.user_id or p.id, "AGENT_CREATED", "Created a tenant agent.", data={"agent_id": body.agent_id, "tenant_id": p.tenant_id})
    return {"agent_id": body.agent_id, "tenant_id": p.tenant_id, "name": body.name, "autonomy": "approval_required"}


@router.patch("/v1/tenant/agents/{agent_id}", response_model=dict)
def change_agent(agent_id: str, body: AgentChange, request: Request, p: Principal = Depends(resolve_principal)):
    service = request.app.state.gateway
    with service.db.connect(write=True) as conn:
        row = target_agent(service, agent_id, p)
        disabled = (audit.utcnow().isoformat() if body.disabled else None) if body.disabled is not None else row["disabled_at"]
        conn.execute("UPDATE tenant_agents SET name=?,disabled_at=? WHERE agent_id=?", (body.name or row["name"], disabled, agent_id))
        if body.disabled:
            conn.execute("UPDATE connections SET revoked_at=? WHERE agent_id=? AND revoked_at IS NULL", (disabled, agent_id))
        audit.append(conn, p.user_id or p.id, "AGENT_CHANGED", "Changed tenant agent.", data={"agent_id": agent_id})
    return dict(conn_row) if (conn_row := target_agent(service, agent_id, p)) else None


class MandateIssue(BaseModel):
    customer_id: str
    max_auto_total_eur: float = Field(default=0, ge=0)
    max_auto_daily_total_eur: float = Field(default=5000, ge=0)
    autonomy: str = Field(default="approval_required", pattern="^(approval_required|autonomous_within_limits)$")
    valid_days: int = Field(default=30, ge=1, le=365)


def issuer_key(conn, tid):
    from .signing import b64, unb64
    row = conn.execute("SELECT private_key FROM tenant_issuers WHERE tenant_id=?", (tid,)).fetchone()
    if row:
        key = Ed25519PrivateKey.from_private_bytes(unb64(row[0]))
    else:
        key = Ed25519PrivateKey.generate()
        conn.execute("INSERT INTO tenant_issuers VALUES(?,?)", (tid, b64(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()))))
    public = b64(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    return key, {"kty": "OKP", "crv": "Ed25519", "alg": "EdDSA", "use": "sig", "kid": tid, "x": public}


def tenant_issuer_keys(conn, issuer, customer_id):
    """Only the unique current owner of a customer may issue its mandates."""
    if not issuer.startswith("urn:tenant:"):
        return []
    tid = issuer.removeprefix("urn:tenant:")
    if not conn.execute("SELECT 1 FROM tenant_customers WHERE tenant_id=? AND customer_id=?", (tid, customer_id)).fetchone():
        return []
    row = conn.execute("SELECT private_key FROM tenant_issuers WHERE tenant_id=?", (tid,)).fetchone()
    if not row:
        return []
    from .models import BuyerJWK
    return [BuyerJWK(**issuer_key(conn, tid)[1])]


@router.post("/v1/tenant/agents/{agent_id}/mandates", status_code=201, response_model=dict)
def issue_mandate(agent_id: str, body: MandateIssue, request: Request, p: Principal = Depends(resolve_principal)):
    service = request.app.state.gateway
    agent = target_agent(service, agent_id, p)
    tid = agent["tenant_id"]
    if body.customer_id not in service.accounts.customers(tid):
        raise DomainError("CUSTOMER_NOT_IN_MANDATE", "Customer does not belong to this tenant", 403)
    from .mandates import sign_jwt
    with service.db.connect(write=True) as conn:
        key, jwk = issuer_key(conn, tid)
        now = audit.utcnow()
        claims = {"iss": "urn:tenant:"+tid, "sub": agent_id, "jti": "mandate_"+token_urlsafe(12), "nbf": now.timestamp(), "exp": (now+timedelta(days=body.valid_days)).timestamp(),
                  "vc": {"@context": ["https://www.w3.org/ns/credentials/v2"], "type": ["VerifiableCredential", "PurchasingMandate"],
                         "credentialSubject": {"agent_id": agent_id, **body.model_dump(exclude={"valid_days"})}}}
        jwt = sign_jwt(key, claims, kid=jwk["kid"])
        service.mandates.used(conn, jwt)
        audit.append(conn, p.user_id or p.id, "MANDATE_ISSUED", "Issued a tenant purchasing mandate.", data={"tenant_id": tid, "agent_id": agent_id, "mandate_id": claims["jti"]})
    return {"id": claims["jti"], "jwt": jwt, "issuer": claims["iss"], "public_key": jwk, "expires_at": claims["exp"]}


@router.get("/v1/tenant/agents/{agent_id}/mandates", response_model=list[dict])
def mandates(agent_id: str, request: Request, p: Principal = Depends(resolve_principal)):
    service = request.app.state.gateway
    target_agent(service, agent_id, p)
    from .mandates import decode
    with service.db.connect() as conn:
        return [{"id": claims["jti"], "issuer": claims["iss"], "subject": claims["vc"]["credentialSubject"],
                 "expires_at": claims["exp"], "revoked": bool(conn.execute("SELECT 1 FROM mandate_revocations WHERE issuer=? AND mandate_id=?", (claims["iss"], claims["jti"])).fetchone())}
                for row in conn.execute("SELECT jwt FROM mandate_credentials")
                if (claims := decode(row[0])[1])["sub"] == agent_id]


@router.delete("/v1/tenant/agents/{agent_id}/mandates/{mandate_id}", response_model=dict)
def revoke_mandate(agent_id: str, mandate_id: str, request: Request, p: Principal = Depends(resolve_principal)):
    service = request.app.state.gateway
    target_agent(service, agent_id, p)
    from .mandates import decode
    with service.db.connect(write=True) as conn:
        claims = next((decode(r[0])[1] for r in conn.execute("SELECT jwt FROM mandate_credentials") if decode(r[0])[1].get("jti") == mandate_id and decode(r[0])[1].get("sub") == agent_id), None)
        if not claims:
            raise DomainError("MANDATE_NOT_FOUND", "Mandate was not found", 404)
        conn.execute("INSERT OR IGNORE INTO mandate_revocations(customer_id,issuer,mandate_id,at) VALUES(?,?,?,?)", (claims["vc"]["credentialSubject"]["customer_id"], claims["iss"], mandate_id, audit.utcnow().isoformat()))
        audit.append(conn, p.user_id or p.id, "MANDATE_REVOKED", "Revoked a tenant mandate.", data={"tenant_id": p.tenant_id, "agent_id": agent_id, "jti_revoked": [mandate_id]})
    return {"revoked": True}


class Overlay(BaseModel):
    approval: dict = Field(default_factory=dict)
    reject: dict = Field(default_factory=dict)
    confirmation_token: str | None = Field(default=None, repr=False, exclude=True)


def tenant_policy(service, p):
    policy = service.policy.get().model_dump(mode="json")
    customers = set(service.accounts.customers(p.tenant_id))
    policy["agents"] = {k: v for k, v in policy["agents"].items() if set(v["customer_ids"]) <= customers}
    policy["customers"] = {k: v for k, v in policy["customers"].items() if k in customers}
    policy["identities"], policy["organisations"] = {}, {}
    return policy


@router.get("/v1/tenant/policy", response_model=dict)
def get_policy(request: Request, p: Principal = Depends(resolve_principal)):
    accounts(request).manage(p)
    return tenant_policy(request.app.state.gateway, p)


@router.put("/v1/tenant/policy", response_model=dict | DecisionPreview)
def set_policy(body: Overlay, request: Request, p: Principal = Depends(resolve_principal)):
    service = request.app.state.gateway
    service.accounts.manage(p)
    platform = service.policy.get(tenant_overlay=False)
    allowed = {"max_auto_total_eur", "max_auto_daily_total_eur", "min_fit_probability", "warnings_need_approval"}
    for k, v in body.approval.items():
        if k not in allowed or not hasattr(platform.approval, k):
            raise DomainError("VALIDATION_ERROR", "Unsupported tenant policy field", 422)
        ceiling = getattr(platform.approval, k)
        if isinstance(ceiling, list):
            valid = isinstance(v, list) and all(isinstance(x, str) for x in v) and set(ceiling) <= set(v)
        else:
            valid = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0 and (ceiling <= v <= 1 if k.startswith("min_") else v <= ceiling)
        if not valid:
            raise DomainError("POLICY_CEILING", "Tenant policy may only tighten platform policy", 403)
    if set(body.reject) - {"on", "on_hard_constraint_violation"} or "on" in body.reject and (
            not isinstance(body.reject["on"], list) or not all(isinstance(v, str) for v in body.reject["on"]) or
            not set(platform.reject.on) <= set(body.reject["on"])):
        raise DomainError("POLICY_CEILING", "Tenant policy may only add rejection rules", 403)
    if "on_hard_constraint_violation" in body.reject and (not isinstance(body.reject["on_hard_constraint_violation"], bool) or platform.reject.on_hard_constraint_violation and not body.reject["on_hard_constraint_violation"]):
        raise DomainError("POLICY_CEILING", "Tenant policy cannot relax hard constraints", 403)
    warnings = body.approval.get("warnings_need_approval", []) + body.reject.get("on", [])
    if any(code not in platform.known_warning_codes for code in warnings):
        raise DomainError("VALIDATION_ERROR", "Use the known_warning_codes from the policy resource", 422)
    payload = body.model_dump(exclude={"confirmation_token"})
    def apply():
        with service.db.connect(write=True) as conn:
            conn.execute("INSERT INTO tenant_policy_overlays(id,data,tenant_id) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data", (p.tenant_id, json.dumps(payload), p.tenant_id))
            audit.append(conn, p.user_id or p.id, "TENANT_POLICY_UPDATED", "Updated tenant policy overlay.", data={"tenant_id": p.tenant_id, "changes": payload})
        return tenant_policy(service, p)
    return confirm_rest(service, p, "policy", p.tenant_id, payload, body.confirmation_token, apply, force=True)


def install_aliases(app):
    """REST audience endpoints reuse the existing operations and validation models."""
    from .evidence import router as evidence_router
    # Included routers are lazy in current FastAPI; use their registered routes
    # directly when creating aliases for an included endpoint.
    for route in [*app.routes, *evidence_router.routes]:
        src = getattr(route, "path", "")
        if src in {"/v1/orders", "/v1/orders/{id}", "/v1/orders/{id}/receipt", "/v1/orders/{id}/ubl", "/v1/orders/{order_id}/timeline", "/v1/orders/{order_id}/evidence",
                   "/v1/approvals", "/v1/approvals/{order_id}/approve", "/v1/approvals/{order_id}/reject", "/v1/orders/{order_id}/cancel",
                   "/v1/agents/{agent_id}/autonomy", "/v1/agents/{agent_id}/track-record"}:
            for audience in ("tenant", "operator"):
                methods = route.methods
                # Tenant/operator consoles read purchases, but only buyer agents create them.
                if src == "/v1/orders" and "POST" in methods:
                    continue
                app.add_api_route(src.replace("/v1", "/v1/"+audience, 1), route.endpoint, methods=list(methods),
                                  response_model=route.response_model, response_class=route.response_class,
                                  responses=route.responses, summary=route.summary, description=route.description,
                                  tags=[audience], name=audience+"_"+route.name, dependencies=[Depends(resolve_principal)])
        elif src in {"/v1/stats", "/v1/audit", "/v1/audit/head", "/v1/audit/verify", "/v1/audit/anchors", "/v1/policy", "/v1/policy/reset", "/v1/suppliers", "/health"}:
            dest = "/v1/operator/health" if src == "/health" else src.replace("/v1", "/v1/operator", 1)
            app.add_api_route(dest, route.endpoint, methods=list(route.methods), response_model=route.response_model, response_class=route.response_class,
                              responses=route.responses, summary=route.summary, description=route.description,
                              tags=["operator"], name="operator_"+route.name, dependencies=[Depends(resolve_principal)])


@router.get("/v1/tenant/audit", response_model=list[AuditEntry])
def tenant_audit(request: Request, p: Principal = Depends(resolve_principal)):
    return request.app.state.gateway.audit()


@router.get("/v1/tenant/spend", response_model=list[dict])
def spend(request: Request, p: Principal = Depends(resolve_principal)):
    return request.app.state.gateway.buyer.spend(p)


@router.get("/v1/tenant/suggestions", response_model=list[dict])
def suggestions(request: Request, customer_id: str | None = None, p: Principal = Depends(resolve_principal)):
    service = request.app.state.gateway
    if customer_id and customer_id not in p.customer_ids:
        raise DomainError("CUSTOMER_NOT_IN_MANDATE", "Customer is outside this tenant", 403)
    return [s for s in service.memory.suggestions(p) if customer_id is None or s["customer_id"] == customer_id]
