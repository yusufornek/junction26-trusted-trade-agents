"""Tenant governance REST contract and shared, confirmed transport operations."""
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request

from .auth import Principal, check_scope, resolve_principal
from .governance_models import EffectivePolicy, GovernanceDecision, PolicyChange, PolicySimulation, Transfer
from .tenant_api import DecisionPreview, confirm_rest

router = APIRouter(tags=["agent governance (spec 44)"])
Operation = Literal["register", "effective_policy", "activity", "alerts", "change_requests", "versions", "version",
    "simulate", "report", "change_policy", "rollback", "approve_change", "reject_change", "pause", "resume",
    "transfer", "pause_tenant", "resume_tenant", "guardrails", "change_guardrails", "guardrail_versions", "rollback_guardrails"]
MUTATIONS = {"change_policy", "rollback", "approve_change", "reject_change", "pause", "resume", "transfer",
             "pause_tenant", "resume_tenant", "change_guardrails", "rollback_guardrails"}


def invoke(service, p, operation, args, confirmation_token=None):
    """One authorization/confirmation boundary for REST, MCP and tenant A2A."""
    g = service.governance
    from .db import DomainError
    if operation not in Operation.__args__ or not isinstance(args, dict):
        raise DomainError("VALIDATION_ERROR", "Use an advertised governance operation and object arguments", 422)
    agent_ops = {"effective_policy", "activity", "versions", "version", "simulate", "change_policy", "rollback", "pause", "resume", "transfer"}
    required = ({"agent_id"} if operation in agent_ops else set())
    if operation in {"change_policy", "change_guardrails", "simulate"}:
        required.add("policy")
    if operation in {"rollback", "version", "rollback_guardrails"}:
        required.add("version_id")
    if operation in {"approve_change", "reject_change"}:
        required.add("request_id")
    if operation == "transfer":
        required.add("owner_user_id")
    if operation == "report":
        required |= {"from", "to"}
    if required - args.keys() or any(args.get(key) is None for key in required):
        raise DomainError("VALIDATION_ERROR", "Missing arguments: " + ", ".join(sorted(required)), 422)
    for field in ("agent_id", "owner_user_id", "version_id", "request_id", "reason", "from", "to", "status"):
        if field in args and not isinstance(args[field], str):
            raise DomainError("VALIDATION_ERROR", field + " must be a string", 422)
    if "policy" in args and not isinstance(args["policy"], dict):
        raise DomainError("VALIDATION_ERROR", "policy must be an object", 422)
    resource = "policy" if operation in {"change_policy", "rollback", "approve_change", "reject_change", "change_guardrails", "rollback_guardrails"} else "agents" if operation not in {"alerts", "report"} else operation
    check_scope(p, resource, "write" if operation in MUTATIONS else "read", operation)
    aid = args.get("agent_id")
    # Check hidden targets before issuing a confirmation token or revealing data.
    if aid:
        with service.db.connect() as conn:
            g.target(conn, aid, p)
    else:
        g.member(p)
    if operation in {"approve_change", "reject_change", "pause_tenant", "resume_tenant", "change_guardrails", "rollback_guardrails", "transfer"}:
        service.accounts.manage(p)
    if operation in {"approve_change", "reject_change"}:
        from .db import DomainError
        with service.db.connect() as conn:
            row = conn.execute("SELECT * FROM governance_requests WHERE id=?", (args.get("request_id"),)).fetchone()
            if not row:
                raise DomainError("POLICY_REQUEST_NOT_FOUND", "Change request was not found", 404)
            g.target(conn, row["agent_id"], p)
            if row["requester"] == (p.user_id or p.id):
                raise DomainError("SELF_APPROVAL_FORBIDDEN", "A requester cannot decide its own loosening request", 403)
    def apply():
        reason = args.get("reason", "")
        if operation == "register":
            return g.register(p, args.get("owner_user_id"), args.get("status"))
        if operation == "effective_policy":
            return g.effective(aid, p)
        if operation == "activity":
            return g.activity(aid, p)
        if operation == "alerts":
            return g.alerts(p)
        if operation == "change_requests":
            return g.requests(p, aid)
        if operation in {"versions", "version"}:
            return g.versions(aid, p, version_id=args.get("version_id"))
        if operation == "guardrail_versions":
            return g.versions(None, p, layer="tenant", version_id=args.get("version_id"))
        if operation == "guardrails":
            return g.versions(None, p, layer="tenant")[0]
        if operation == "simulate":
            return g.simulate(aid, args["policy"], p)
        if operation == "report":
            try:
                start = datetime.fromisoformat(args["from"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(args["to"].replace("Z", "+00:00"))
            except (ValueError, TypeError, AttributeError):
                raise DomainError("VALIDATION_ERROR", "from/to must be ISO timestamps", 422) from None
            return g.report(p, start.replace(tzinfo=timezone.utc) if start.tzinfo is None else start,
                            end.replace(tzinfo=timezone.utc) if end.tzinfo is None else end, args.get("signed", False))
        if operation in {"change_policy", "rollback"}:
            return g.change(aid, args.get("policy", {}), reason, p, rollback_version=args.get("version_id") if operation == "rollback" else None)
        if operation in {"change_guardrails", "rollback_guardrails"}:
            return g.change_guardrails(args.get("policy", {}), reason, p, rollback_version=args.get("version_id") if operation == "rollback_guardrails" else None)
        if operation in {"approve_change", "reject_change"}:
            return g.decide(args["request_id"], "approve" if operation == "approve_change" else "reject", reason, p)
        if operation in {"pause", "resume"}:
            return g.pause(aid, operation == "pause", reason, p)
        if operation in {"pause_tenant", "resume_tenant"}:
            return g.pause_tenant(p.tenant_id, operation == "pause_tenant", reason, p)
        if operation == "transfer":
            return g.transfer(aid, args["owner_user_id"], reason, p)
        raise DomainError("VALIDATION_ERROR", "Unknown governance operation", 422)
    if operation in MUTATIONS:
        from .db import DomainError
        if not str(args.get("reason", "")).strip():
            raise DomainError("VALIDATION_ERROR", "A reason is required", 422)
        if operation in {"change_policy", "rollback"}:
            g.validate_change(aid, args.get("policy", {}), p, args.get("version_id") if operation == "rollback" else None)
        if operation in {"change_guardrails", "rollback_guardrails"}:
            g.validate_guardrails(args.get("policy", {}), p, rollback_version=args.get("version_id") if operation == "rollback_guardrails" else None)
        bound_payload = dict(args)
        if operation in {"change_policy", "rollback"}:
            bound_payload["base_policy_version_id"] = g.effective(aid, p).version_id
        elif operation in {"change_guardrails", "rollback_guardrails"}:
            bound_payload["base_policy_version_id"] = g.versions(None, p, layer="tenant")[0]["id"]
        return confirm_rest(service, p, "governance:" + operation, aid or args.get("request_id") or p.tenant_id,
                            bound_payload, confirmation_token, apply, force=True)
    return apply()


def call(request, p, operation, **args):
    token = args.pop("confirmation_token", None)
    return invoke(request.app.state.gateway, p, operation, args, token)


@router.get("/v1/tenant/agents/{agent_id}/policy/effective", response_model=EffectivePolicy)
def effective_policy(agent_id: str, request: Request, p: Principal = Depends(resolve_principal)):
    return call(request, p, "effective_policy", agent_id=agent_id)


@router.get("/v1/tenant/agents/{agent_id}/policy", response_model=dict)
def agent_policy(agent_id: str, request: Request, p: Principal = Depends(resolve_principal)):
    return request.app.state.gateway.governance.versions(agent_id, p)[0]


@router.put("/v1/tenant/agents/{agent_id}/policy", response_model=dict | DecisionPreview)
@router.patch("/v1/tenant/agents/{agent_id}/policy", response_model=dict | DecisionPreview)
def change_policy(agent_id: str, body: PolicyChange, request: Request, p: Principal = Depends(resolve_principal)):
    return call(request, p, "change_policy", agent_id=agent_id, policy=body.policy.model_dump(exclude_unset=True), reason=body.reason, confirmation_token=body.confirmation_token)


@router.post("/v1/tenant/agents/{agent_id}/policy/simulate", response_model=dict)
def simulate(agent_id: str, body: PolicySimulation, request: Request, p: Principal = Depends(resolve_principal)):
    return call(request, p, "simulate", agent_id=agent_id, policy=body.policy.model_dump(exclude_unset=True))


@router.get("/v1/tenant/agents/{agent_id}/policy/versions", response_model=list[dict])
def versions(agent_id: str, request: Request, p: Principal = Depends(resolve_principal)):
    return call(request, p, "versions", agent_id=agent_id)


@router.get("/v1/tenant/agents/{agent_id}/policy/versions/{version_id}", response_model=dict)
def version(agent_id: str, version_id: str, request: Request, p: Principal = Depends(resolve_principal)):
    return call(request, p, "version", agent_id=agent_id, version_id=version_id)


@router.post("/v1/tenant/agents/{agent_id}/policy/versions/{version_id}/rollback", response_model=dict | DecisionPreview)
def rollback(agent_id: str, version_id: str, body: GovernanceDecision, request: Request, p: Principal = Depends(resolve_principal)):
    return call(request, p, "rollback", agent_id=agent_id, version_id=version_id, reason=body.reason, confirmation_token=body.confirmation_token)


@router.get("/v1/tenant/policy/change-requests", response_model=list[dict])
def change_requests(request: Request, agent_id: str | None = None, p: Principal = Depends(resolve_principal)):
    return call(request, p, "change_requests", **({"agent_id": agent_id} if agent_id else {}))


@router.get("/v1/tenant/policy/change-requests/{request_id}", response_model=dict)
def change_request(request_id: str, request: Request, p: Principal = Depends(resolve_principal)):
    from .db import DomainError
    found = next((r for r in request.app.state.gateway.governance.requests(p) if r["id"] == request_id), None)
    if not found:
        raise DomainError("POLICY_REQUEST_NOT_FOUND", "Change request was not found", 404)
    return found


@router.post("/v1/tenant/policy/change-requests/{request_id}/{decision}", response_model=dict | DecisionPreview)
def decide_change(request_id: str, decision: Literal["approve", "reject"], body: GovernanceDecision, request: Request, p: Principal = Depends(resolve_principal)):
    return call(request, p, decision + "_change", request_id=request_id, reason=body.reason, confirmation_token=body.confirmation_token)


@router.get("/v1/tenant/agents/{agent_id}/activity", response_model=list[dict])
def activity(agent_id: str, request: Request, p: Principal = Depends(resolve_principal)):
    return call(request, p, "activity", agent_id=agent_id)


@router.post("/v1/tenant/agents/{agent_id}/pause", response_model=dict | DecisionPreview)
def pause(agent_id: str, body: GovernanceDecision, request: Request, p: Principal = Depends(resolve_principal)):
    return call(request, p, "pause", agent_id=agent_id, reason=body.reason, confirmation_token=body.confirmation_token)


@router.post("/v1/tenant/agents/{agent_id}/resume", response_model=dict | DecisionPreview)
def resume(agent_id: str, body: GovernanceDecision, request: Request, p: Principal = Depends(resolve_principal)):
    return call(request, p, "resume", agent_id=agent_id, reason=body.reason, confirmation_token=body.confirmation_token)


@router.post("/v1/tenant/agents/{agent_id}/transfer", response_model=dict | DecisionPreview)
def transfer(agent_id: str, body: Transfer, request: Request, p: Principal = Depends(resolve_principal)):
    return call(request, p, "transfer", agent_id=agent_id, owner_user_id=body.owner_user_id, reason=body.reason, confirmation_token=body.confirmation_token)


@router.post("/v1/tenant/agents/pause", response_model=dict | DecisionPreview)
def pause_all(body: GovernanceDecision, request: Request, p: Principal = Depends(resolve_principal)):
    return call(request, p, "pause_tenant", reason=body.reason, confirmation_token=body.confirmation_token)


@router.post("/v1/tenant/agents/resume", response_model=dict | DecisionPreview)
def resume_all(body: GovernanceDecision, request: Request, p: Principal = Depends(resolve_principal)):
    return call(request, p, "resume_tenant", reason=body.reason, confirmation_token=body.confirmation_token)


@router.get("/v1/tenant/guardrails", response_model=dict)
def guardrails(request: Request, p: Principal = Depends(resolve_principal)):
    return call(request, p, "guardrails")


@router.put("/v1/tenant/guardrails", response_model=dict | DecisionPreview)
def change_guardrails(body: PolicyChange, request: Request, p: Principal = Depends(resolve_principal)):
    return call(request, p, "change_guardrails", policy=body.policy.model_dump(exclude_unset=True), reason=body.reason, confirmation_token=body.confirmation_token)


@router.get("/v1/tenant/guardrails/versions", response_model=list[dict])
def guardrail_versions(request: Request, p: Principal = Depends(resolve_principal)):
    return call(request, p, "guardrail_versions")


@router.get("/v1/tenant/guardrails/versions/{version_id}", response_model=dict)
def guardrail_version(version_id: str, request: Request, p: Principal = Depends(resolve_principal)):
    return call(request, p, "guardrail_versions", version_id=version_id)


@router.post("/v1/tenant/guardrails/versions/{version_id}/rollback", response_model=dict | DecisionPreview)
def rollback_guardrails(version_id: str, body: GovernanceDecision, request: Request, p: Principal = Depends(resolve_principal)):
    return call(request, p, "rollback_guardrails", version_id=version_id, reason=body.reason, confirmation_token=body.confirmation_token)


@router.get("/v1/tenant/alerts", response_model=list[dict])
def alerts(request: Request, p: Principal = Depends(resolve_principal)):
    return call(request, p, "alerts")


@router.get("/v1/tenant/governance/report", response_model=dict)
def report(request: Request, start: datetime = Query(alias="from"), end: datetime = Query(alias="to"), signed: bool = False, p: Principal = Depends(resolve_principal)):
    return call(request, p, "report", **{"from": start.isoformat(), "to": end.isoformat(), "signed": signed})


@router.get("/v1/operator/governance/policy", response_model=dict)
def platform_policy(request: Request, p: Principal = Depends(resolve_principal)):
    return request.app.state.gateway.governance.versions(None, p, layer="platform")[0]


@router.put("/v1/operator/governance/policy", response_model=dict | DecisionPreview)
def change_platform_policy(body: PolicyChange, request: Request, p: Principal = Depends(resolve_principal)):
    service = request.app.state.gateway
    service.accounts.manage(p, platform=True)
    patch = body.policy.model_dump(exclude_unset=True)
    service.governance.validate_guardrails(patch, p, layer="platform")
    payload = {"policy": patch, "reason": body.reason}
    return confirm_rest(service, p, "governance:platform_policy", "platform", payload, body.confirmation_token,
        lambda: service.governance.change_guardrails(patch, body.reason, p, layer="platform"), force=True)


@router.get("/v1/operator/governance/policy/versions", response_model=list[dict])
def platform_versions(request: Request, p: Principal = Depends(resolve_principal)):
    return request.app.state.gateway.governance.versions(None, p, layer="platform")


@router.get("/v1/operator/governance/policy/versions/{version_id}", response_model=dict)
def platform_version(version_id: str, request: Request, p: Principal = Depends(resolve_principal)):
    return request.app.state.gateway.governance.versions(None, p, layer="platform", version_id=version_id)


@router.post("/v1/operator/governance/policy/versions/{version_id}/rollback", response_model=dict | DecisionPreview)
def platform_rollback(version_id: str, body: GovernanceDecision, request: Request, p: Principal = Depends(resolve_principal)):
    service = request.app.state.gateway
    service.governance.validate_guardrails({}, p, layer="platform", rollback_version=version_id)
    return confirm_rest(service, p, "governance:platform_rollback", version_id, {"reason": body.reason}, body.confirmation_token,
        lambda: service.governance.change_guardrails({}, body.reason, p, layer="platform", rollback_version=version_id), force=True)


@router.post("/v1/operator/tenants/{tenant_id}/pause", response_model=dict | DecisionPreview)
@router.post("/v1/operator/tenants/{tenant_id}/resume", response_model=dict | DecisionPreview)
def platform_stop(tenant_id: str, body: GovernanceDecision, request: Request, p: Principal = Depends(resolve_principal)):
    service = request.app.state.gateway
    service.accounts.manage(p, platform=True)
    paused = request.url.path.endswith("/pause")
    return confirm_rest(service, p, "governance:tenant_pause", tenant_id, {"paused": paused, "reason": body.reason}, body.confirmation_token,
        lambda: service.governance.pause_tenant(tenant_id, paused, body.reason, p), force=True)
