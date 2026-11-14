"""Tenant business profiles and approval separation (spec 45, profile slice)."""
import json
from secrets import token_urlsafe
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import audit
from .auth import Principal, resolve_principal
from .db import DomainError
from .guardrail_presets import load_preset, signup_options
from .tenant_api import DecisionPreview

router = APIRouter(tags=["guardrail profiles"])


class BusinessProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    business_type: Literal["single_restaurant", "restaurant_group", "hotel_chain", "hospital_or_school_kitchen", "catering"]
    sites: int = Field(ge=1, le=100000, strict=True)
    buyers_and_approvers: int = Field(ge=1, le=100000, strict=True)
    monthly_purchase_volume_band: Literal["<5k", "5–25k", "25–100k", ">100k"]
    meals_per_day_band: Literal["<100", "100–500", "500–2000", ">2000"]
    required_claims: list[str] = Field(default_factory=list)
    exclude_allergens: list[str] = Field(default_factory=list)


class Signup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    password: str = Field(min_length=10, max_length=1024, repr=False)
    profile: BusinessProfile

    @field_validator("email")
    @classmethod
    def email_normalized(cls, value):
        return value.strip().lower()


class SourcedValue(BaseModel):
    value: Any
    source: str


class ProfileResponse(BaseModel):
    tenant_id: str
    profile: BusinessProfile
    preset: str
    guardrails: dict[str, SourcedValue]
    guardrail_version: int
    profile_revision: int
    author_user_id: str
    updated_at: str
    warnings: list[dict] = Field(default_factory=list)


class ProfileUpdate(BusinessProfile):
    confirmation_token: str | None = Field(default=None, repr=False, exclude=True)


class SignupResponse(BaseModel):
    tenant: dict
    user: dict
    customer_ids: list[str]
    guardrail_profile: ProfileResponse


def read_profile(conn, tenant_id):
    row = conn.execute("SELECT data FROM tenant_guardrail_profiles WHERE tenant_id=?", (tenant_id,)).fetchone()
    return json.loads(row[0]) if row else None


def normalize_profile(service, profile):
    from .catalog import Catalog
    data = profile.model_dump(exclude={"confirmation_token"})
    for field in ("required_claims", "exclude_allergens"):
        data[field] = Catalog(service.db).normalize_codes(data[field], field, location="profile." + field)
    return BusinessProfile(**data)


def save_profile(conn, tenant_id, profile, author_user_id):
    previous = read_profile(conn, tenant_id)
    # Profile edits must not silently loosen/reset an active spec 44 guardrail.
    # Business changes describe the tenant; applying a new preset is governance work.
    if previous:
        for field in ("required_claims", "exclude_allergens"):
            if not set(previous["profile"][field]) <= set(getattr(profile, field)):
                raise DomainError("GUARDRAIL_CHANGE_REQUIRED", "Removing standing constraints requires spec 44 change control", 409)
    preset = previous["preset"] if previous else profile.business_type
    guardrails = previous["guardrails"] if previous else load_preset(preset, profile.monthly_purchase_volume_band, profile.sites)
    revision = previous["profile_revision"] + 1 if previous else 1
    result = ProfileResponse(tenant_id=tenant_id, profile=profile, preset=preset, guardrails=guardrails,
        guardrail_version=previous["guardrail_version"] if previous else 1, profile_revision=revision,
        author_user_id=author_user_id, updated_at=audit.utcnow().isoformat(),
        warnings=[] if guardrails["four_eyes"]["value"] else [{"code": "SELF_APPROVAL_ALLOWED", "text": "Four-eyes is off; self-approvals are recorded with a warning."}]).model_dump(mode="json")
    encoded = json.dumps(result)
    conn.execute("INSERT INTO tenant_guardrail_profiles(tenant_id,data) VALUES(?,?) ON CONFLICT(tenant_id) DO UPDATE SET data=excluded.data", (tenant_id, encoded))
    conn.execute("INSERT INTO tenant_profile_versions(tenant_id,version,data) VALUES(?,?,?)", (tenant_id, revision, encoded))
    audit.append(conn, "human:" + author_user_id, "TENANT_PROFILE_UPDATED" if previous else "TENANT_PROFILE_CREATED",
        "Saved business profile and sourced tenant guardrail snapshot.",
        data={"tenant_id": tenant_id, "user_id": author_user_id, "profile_revision": revision,
              "guardrail_profile": result})
    return result


def require_constraints(conn, customer_id):
    row = conn.execute("SELECT tenant_id FROM tenant_customers WHERE customer_id=?", (customer_id,)).fetchone()
    snapshot = read_profile(conn, row[0]) if row else None
    if snapshot and (snapshot["preset"] == "hospital_or_school_kitchen" or snapshot["profile"]["business_type"] == "hospital_or_school_kitchen"):
        missing = [field for field in ("required_claims", "exclude_allergens") if not snapshot["profile"][field]]
        if missing:
            raise DomainError("CONSTRAINTS_REQUIRED", "Set required claims and excluded allergens before ordering for a hospital or school kitchen", 409,
                {"missing_fields": missing, "profile_url": "/v1/tenant/profile"})


def register_owner(conn, agent_id, tenant_id, user_id=None):
    if user_id is None or not conn.execute("SELECT 1 FROM users WHERE id=? AND tenant_id=?", (user_id, tenant_id)).fetchone():
        first = conn.execute("SELECT id FROM users WHERE tenant_id=? AND role='tenant_admin' ORDER BY created_at,id LIMIT 1", (tenant_id,)).fetchone()
        user_id = first[0] if first else None
    if user_id:
        conn.execute("INSERT OR IGNORE INTO profile_agent_owners(agent_id,owner_user_id,tenant_id) VALUES(?,?,?)", (agent_id, user_id, tenant_id))


def record_policy_loosening(conn, agent_id, user_id, tenant_id):
    """Spec 44 change approval hook; call in the policy approval transaction."""
    audit.append(conn, "human:" + user_id, "AGENT_POLICY_LOOSENED", "Approved a policy loosening.",
        data={"agent_id": agent_id, "user_id": user_id, "tenant_id": tenant_id})


def conflicted_users(conn, order, tenant_id):
    conflicts = {r[0] for r in conn.execute("SELECT created_by FROM connections WHERE agent_id=? AND tenant_id=?", (order.agent_id, tenant_id)) if r[0]}
    # The spec 44 owner column is authoritative after merging that lane.
    agent = conn.execute("SELECT * FROM tenant_agents WHERE agent_id=? AND tenant_id=?", (order.agent_id, tenant_id)).fetchone()
    if agent and "owner_user_id" in agent.keys() and agent["owner_user_id"]:
        conflicts.add(agent["owner_user_id"])
    else:
        owner = conn.execute("SELECT owner_user_id FROM profile_agent_owners WHERE agent_id=? AND tenant_id=?", (order.agent_id, tenant_id)).fetchone()
        if owner:
            conflicts.add(owner[0])
    # Only the most recent grant/loosening in each stream disqualifies its author.
    for action in ("AUTONOMY_CHANGED", "AGENT_POLICY_LOOSENED"):
        row = conn.execute("SELECT actor,data FROM audit WHERE action=? AND tenant_id=? AND json_extract(data,'$.agent_id')=? "
            "AND (? != 'AUTONOMY_CHANGED' OR json_extract(data,'$.level')='autonomous_within_limits') ORDER BY id DESC LIMIT 1", (action, tenant_id, order.agent_id, action)).fetchone()
        if row:
            data = json.loads(row["data"])
            if action == "AUTONOMY_CHANGED" and data.get("level") != "autonomous_within_limits":
                continue
            user = data.get("user_id") or row["actor"].removeprefix("human:")
            conflicts.add(user)
    return conflicts


def approval_requirements(conn, order):
    row = conn.execute("SELECT tenant_id FROM orders WHERE id=?", (order.id,)).fetchone()
    tid = row[0] if row else None
    snapshot = read_profile(conn, tid) if tid else None
    if not snapshot:
        return None
    values = {k: v["value"] for k, v in snapshot["guardrails"].items()}
    threshold = values["approver_amount_threshold_eur"]
    conflicts = conflicted_users(conn, order, tid)
    eligible = []
    for user in conn.execute("SELECT id,role FROM users WHERE tenant_id=? AND role IN ('approver','tenant_admin') AND password_hash IS NOT NULL AND disabled_at IS NULL AND removed_at IS NULL", (tid,)):
        if (user["role"] == "tenant_admin" or order.total <= threshold) and (not values["four_eyes"] or user["id"] not in conflicts):
            eligible.append(user["id"])
    return {"tenant_id": tid, "approver_amount_threshold_eur": threshold,
            "required_role": "tenant_admin" if order.total > threshold else "approver_or_tenant_admin",
            "four_eyes": values["four_eyes"], "eligible_approver_user_ids": sorted(eligible),
            "conflicted_user_ids": sorted(conflicts)}


def authorize_approval(conn, principal, order):
    requirements = approval_requirements(conn, order)
    if not requirements:
        return False
    if principal is None:
        raise DomainError("APPROVER_REQUIRED", "Profiled tenant orders require an authenticated human reviewer", 403)
    uid = principal.user_id or principal.id
    if requirements["required_role"] == "tenant_admin" and principal.role != "tenant_admin":
        raise DomainError("APPROVAL_LIMIT", "Only a tenant administrator can approve above this amount", 403,
            {"threshold_eur": requirements["approver_amount_threshold_eur"], "who_can_approve": requirements["eligible_approver_user_ids"], "required_role": "tenant_admin"})
    self_approval = uid in requirements["conflicted_user_ids"]
    if requirements["four_eyes"] and self_approval:
        raise DomainError("FOUR_EYES", "An independent approver is required for this agent", 403,
            {"who_can_approve": requirements["eligible_approver_user_ids"], "required_role": requirements["required_role"]})
    return self_approval


@router.get("/v1/auth/signup/options", response_model=dict, summary="Business profile choices and display labels")
def options():
    return signup_options()


@router.post("/v1/auth/signup", response_model=SignupResponse, status_code=201,
    description="Create a business, its first tenant admin and sourced preset snapshot atomically. Sign in through /v1/auth/login afterwards. Hospital/school orders require both standing claims and allergen exclusions.")
def signup(body: Signup, request: Request, response: Response):
    from .accounts import hash_password, public_user
    from .auth import check_source
    from .tenant_api import issuer_key
    service = request.app.state.gateway
    check_source(service, request.client.host if request.client else "unknown")
    profile = normalize_profile(service, body.profile)
    encoded = hash_password(body.password)
    tid, uid, customer = "ten_" + token_urlsafe(12), "usr_" + token_urlsafe(12), "buyer_" + token_urlsafe(12)
    with service.db.connect(write=True) as conn:
        if conn.execute("SELECT 1 FROM users WHERE email=?", (body.email,)).fetchone():
            raise DomainError("EMAIL_EXISTS", "Email already has an account", 409)
        now = audit.utcnow().isoformat()
        conn.execute("INSERT INTO tenants(id,name,created_at) VALUES(?,?,?)", (tid, body.name, now))
        conn.execute("INSERT INTO tenant_customers VALUES(?,?)", (customer, tid))
        conn.execute("INSERT INTO users(id,tenant_id,email,role,password_hash,created_at) VALUES(?,?,?,'tenant_admin',?,?)", (uid, tid, body.email, encoded, now))
        issuer_key(conn, tid)
        snapshot = save_profile(conn, tid, profile, uid)
        user = public_user(conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())
    response.headers["Cache-Control"] = "no-store"
    return {"tenant": {"id": tid, "name": body.name}, "user": user, "customer_ids": [customer], "guardrail_profile": snapshot}


@router.get("/v1/tenant/profile", response_model=ProfileResponse | None)
def get_profile(request: Request, p: Principal = Depends(resolve_principal)):
    with request.app.state.gateway.db.connect() as conn:
        return read_profile(conn, p.tenant_id)


@router.put("/v1/tenant/profile", response_model=ProfileResponse | DecisionPreview,
    description="Tenant admin updates the business profile after the REST confirmation handshake. Existing guardrail values remain versioned; applying another preset or removing constraints requires spec 44 change control.")
def update_profile(body: ProfileUpdate, request: Request, p: Principal = Depends(resolve_principal)):
    from .tenant_api import confirm_rest
    service = request.app.state.gateway
    service.accounts.manage(p)
    if not p.tenant_id:
        raise DomainError("TENANT_ADMIN_REQUIRED", "Sign in as a tenant administrator", 403)
    profile = normalize_profile(service, body)
    def apply():
        with service.db.connect(write=True) as conn:
            return save_profile(conn, p.tenant_id, profile, p.user_id or p.id)
    return confirm_rest(service, p, "profile", p.tenant_id, profile.model_dump(), body.confirmation_token, apply, force=True)
