"""Customer-scoped buyer control plane over the existing procurement ledger."""
import json
from datetime import timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field

from . import audit
from .auth import Principal, resolve_principal
from .autonomy import buyer_scope
from .db import DomainError
from .mandates import decode
from .models import ApprovalRequest, Order


def check_suspension(db, agent_id):
    with db.connect() as conn:
        row = conn.execute("SELECT data FROM buyer_suspensions WHERE agent_id=?", (agent_id,)).fetchone()
    if row and json.loads(row[0])["suspended"]:
        raise DomainError("AGENT_SUSPENDED", "Buyer suspended this agent; contact the buyer administrator", 403)


class Reason(BaseModel):
    reason: str = Field(default="Buyer emergency control", min_length=1, max_length=4000)


class Buyer:
    def __init__(self, gateway):
        self.gateway, self.db = gateway, gateway.db

    def scope(self, principal, *, admin=False):
        if not principal.operator and (principal.kind != "buyer_user" or principal.role not in {
                "buyer_viewer", "buyer_approver", "buyer_admin"}):
            raise DomainError("OPERATOR_REQUIRED", "A buyer user credential is required", 403)
        if admin and (principal.operator or principal.role != "buyer_admin"):
            raise DomainError("BUYER_ADMIN_REQUIRED", "A buyer administrator credential is required", 403)
        policy = self.gateway.policy.get()
        customers = sorted(set(principal.customer_ids)) if not principal.operator else sorted(
            set(policy.customers) | {c for row in policy.agents.values() for c in row.customer_ids} |
            {c for org in policy.organisations.values() for c in org.customer_ids})
        if principal.operator:
            with self.db.connect() as conn:
                historical = {r[0] for r in conn.execute("SELECT DISTINCT json_extract(data,'$.customer_id') FROM orders") if r[0]}
            customers = sorted(set(customers) | historical)
        return {"organisation_id": principal.organisation_id, "customer_ids": customers}

    def envelope(self, principal, **data):
        return {**self.scope(principal), **data}

    def visible(self, principal, agent_id, customers):
        return principal.operator or buyer_scope(self.gateway.policy.get(), principal, agent_id, customers)

    def mandates(self, principal):
        self.scope(principal)
        result = []
        with self.db.connect() as conn:
            revocations = [dict(r) for r in conn.execute("SELECT actor,data FROM audit WHERE action='MANDATE_REVOKED' ORDER BY id DESC")]
            for row in conn.execute("SELECT jwt FROM mandate_credentials ORDER BY last_used DESC"):
                claims = decode(row[0])[1]
                subject = claims["vc"]["credentialSubject"]
                if not self.visible(principal, subject["agent_id"], [subject["customer_id"]]):
                    continue
                revoked = conn.execute("SELECT at FROM mandate_revocations WHERE customer_id IN ('',?) AND issuer=? AND mandate_id=?",
                    (subject["customer_id"], claims["iss"], claims["jti"])).fetchone()
                provenance = None
                if revoked:
                    provenance = {"at": revoked[0], "source": "legacy_revocation"}
                    for event in revocations:
                        data = json.loads(event["data"])
                        if (claims["jti"] in data.get("jti_revoked", []) and
                                subject["customer_id"] in data.get("customer_ids", []) and
                                data.get("issuer", claims["iss"]) == claims["iss"]):
                            provenance.update(source=data.get("source", "buyer_signed"), actor=event["actor"],
                                              organisation_id=data.get("organisation_id"))
                            break
                result.append({"jti": claims["jti"], "issuer": claims["iss"], **subject,
                               "valid_from": claims["nbf"], "valid_until": claims["exp"],
                               "revoked_at": revoked[0] if revoked else None, "revocation": provenance, "source": "buyer_vc"})
        return result

    def agents(self, principal):
        self.scope(principal)
        policy = self.gateway.policy.get()
        rows = {agent: row.model_dump(mode="json") for agent, row in policy.agents.items()}
        mandates = self.mandates(principal)
        for mandate in mandates:
            row = rows.setdefault(mandate["agent_id"], {"customer_ids": []})
            if mandate["customer_id"] not in row["customer_ids"]:
                row["customer_ids"].append(mandate["customer_id"])
        with self.db.connect() as conn:
            # Keep agents with historical purchases visible after their authority is removed.
            for stored in conn.execute("SELECT data FROM orders"):
                order = Order.model_validate_json(stored[0])
                agent = rows.setdefault(order.agent_id, {"customer_ids": []})
                if order.customer_id not in agent["customer_ids"]:
                    agent["customer_ids"].append(order.customer_id)
            suspensions = {r["agent_id"]: json.loads(r["data"]) for r in conn.execute("SELECT * FROM buyer_suspensions")}
        return [{"agent_id": agent, **row, "suspension": suspensions.get(agent),
                 "mandates": [m for m in mandates if m["agent_id"] == agent]}
                for agent, row in sorted(rows.items()) if self.visible(principal, agent, row["customer_ids"])]

    def orders(self, principal, *, pending=False):
        self.scope(principal)
        return [o for o in self.gateway.orders.list_orders(pending_only=pending)
                if self.visible(principal, o.agent_id, [o.customer_id])]

    def spend(self, principal):
        self.scope(principal)
        self.gateway.orders.expire()
        now = audit.utcnow()
        start = now - timedelta(hours=24)
        agents = self.agents(principal)
        mandates = self.mandates(principal)
        result = []
        with self.db.connect() as conn:
            conn.execute("BEGIN")
            orders = [Order.model_validate_json(r[0]) for r in conn.execute("SELECT data FROM orders")]
            for agent in agents:
                agent_id = agent["agent_id"]
                # The latest successfully used VC is the same authority used for the next headerless bound command.
                vc = None
                for stored in conn.execute("SELECT jwt FROM mandate_credentials ORDER BY last_used DESC"):
                    try:
                        claims = decode(stored[0])[1]
                        customer = claims["vc"]["credentialSubject"]["customer_id"]
                        if claims["sub"] != agent_id:
                            continue
                        _, subject = self.gateway.mandates.verify(conn, stored[0], agent_id, customer)
                        if self.visible(principal, agent_id, [customer]):
                            vc = subject
                            break
                    except DomainError:
                        continue
                policy_row = self.gateway.policy.get().agents.get(agent_id)
                policy_limit = policy_row.max_auto_daily_total_eur if policy_row else None
                vc_limit = vc["max_auto_daily_total_eur"] if vc else None
                limit = min(v for v in (policy_limit, vc_limit) if v is not None) if any(
                    v is not None for v in (policy_limit, vc_limit)) else 0
                source = "mandate" if vc_limit is not None and (policy_limit is None or vc_limit <= policy_limit) else "policy"
                autonomous = self.gateway.policy.autonomous_total(conn, agent_id)
                own = [o for o in orders if o.agent_id == agent_id and o.currency == "EUR" and start <= o.created_at <= now]
                result.append({"agent_id": agent_id, "window_start": start.isoformat(), "window_end": now.isoformat(),
                    "currency": "EUR", "limit": limit, "limit_source": source,
                    "autonomous_spend": float(autonomous), "remaining": float(max(Decimal(0), Decimal(str(limit)) - autonomous)),
                    "pending_commitments": float(sum((Decimal(str(o.total)) for o in own if o.status == "PENDING_APPROVAL"), Decimal(0))),
                    "human_approved_spend": float(sum((Decimal(str(o.total)) for o in own if o.status == "CONFIRMED" and o.approver is not None), Decimal(0)))})
        return result

    def authorize_control(self, agent_id, principal):
        self.scope(principal, admin=True)
        target = next((a for a in self.agents(principal) if a["agent_id"] == agent_id), None)
        if target is None:
            raise DomainError("AGENT_NOT_FOUND", "Agent was not found", 404)
        return target

    def control(self, agent_id, suspended, reason, principal):
        target = self.authorize_control(agent_id, principal)
        data = {"agent_id": agent_id, "suspended": suspended, "reason": reason,
                "actor": f"human:buyer_admin:{principal.credential_ref}", "at": audit.utcnow().isoformat(),
                "organisation_id": principal.organisation_id, "customer_ids": target["customer_ids"], "source": "buyer_local"}
        with self.db.connect(write=True) as conn:
            conn.execute("INSERT INTO buyer_suspensions VALUES(?,?) ON CONFLICT(agent_id) DO UPDATE SET data=excluded.data",
                         (agent_id, json.dumps(data)))
            audit.append(conn, data["actor"], "AGENT_SUSPENDED" if suspended else "AGENT_RESUMED",
                         f"Buyer {'suspended' if suspended else 'resumed'} agent {agent_id}.", data=data)
        return self.envelope(principal, suspension=data)

    def authorize_revocation(self, jti, principal):
        self.scope(principal, admin=True)
        matches = [m for m in self.mandates(principal) if m["jti"] == jti]
        if not matches:
            raise DomainError("AGENT_NOT_FOUND", "Mandate was not found", 404)
        return matches

    def revoke(self, jti, principal):
        matches = self.authorize_revocation(jti, principal)
        with self.db.connect(write=True) as conn:
            for m in matches:
                conn.execute("INSERT OR IGNORE INTO mandate_revocations VALUES(?,?,?,?)",
                    (m["customer_id"], m["issuer"], jti, audit.utcnow().isoformat()))
            for issuer in sorted({m["issuer"] for m in matches}):
                audit.append(conn, f"human:buyer_admin:{principal.credential_ref}", "MANDATE_REVOKED",
                    "Buyer administrator revoked purchasing authority.", data={"jti_revoked": [jti], "issuer": issuer,
                    "organisation_id": principal.organisation_id,
                    "customer_ids": sorted({m["customer_id"] for m in matches if m["issuer"] == issuer}),
                    "source": "buyer_local"})
        return self.envelope(principal, revoked=[jti])

    def activity(self, principal):
        self.scope(principal)
        from .visibility import event_visible
        with self.db.connect() as conn:
            result = []
            for row in conn.execute("SELECT e.*,a.summary FROM events e JOIN audit a ON a.id=e.audit_id ORDER BY e.rowid DESC"):
                event = dict(row)
                event["payload"] = json.loads(event["payload"])
                if event_visible(self.gateway, conn, event, principal):
                    result.append(event)
        return result


router = APIRouter(prefix="/v1/buyer", tags=["Buyer"])


@router.get("/agents", response_model=dict, summary="Buyer-scoped agents, local suspensions and VC revocations")
def agents(request: Request, principal: Principal = Depends(resolve_principal)):
    buyer = request.app.state.gateway.buyer
    return buyer.envelope(principal, agents=buyer.agents(principal))


@router.get("/mandates", response_model=dict, summary="Buyer-scoped successfully used mandates and revocations")
def mandates(request: Request, principal: Principal = Depends(resolve_principal)):
    buyer = request.app.state.gateway.buyer
    return buyer.envelope(principal, mandates=buyer.mandates(principal))


@router.get("/spend", response_model=dict, summary="Rolling 24-hour spend from the policy ledger, with effective limits")
def spend(request: Request, principal: Principal = Depends(resolve_principal)):
    buyer = request.app.state.gateway.buyer
    return buyer.envelope(principal, spend=buyer.spend(principal))


@router.get("/orders", response_model=dict)
def orders(request: Request, principal: Principal = Depends(resolve_principal)):
    buyer = request.app.state.gateway.buyer
    return buyer.envelope(principal, orders=buyer.orders(principal))


@router.get("/approvals", response_model=dict)
def approvals(request: Request, principal: Principal = Depends(resolve_principal)):
    buyer = request.app.state.gateway.buyer
    return buyer.envelope(principal, approvals=[{"order": o, "quote": request.app.state.gateway.orders.get_quote(o.quote_id)}
                                             for o in buyer.orders(principal, pending=True)])


@router.get("/activity", response_model=dict, summary="Buyer-scoped domain events, newest first, with actors and summaries")
def activity(request: Request, principal: Principal = Depends(resolve_principal)):
    buyer = request.app.state.gateway.buyer
    return buyer.envelope(principal, activity=buyer.activity(principal))


@router.post("/agents/{agent_id}/suspend", response_model=dict)
def suspend(agent_id: str, request: Request, body: Reason = Reason(), principal: Principal = Depends(resolve_principal),
            idempotency_key: Annotated[str | None, Header(min_length=1)] = None):
    buyer = request.app.state.gateway.buyer
    buyer.scope(principal, admin=True)
    return buyer.control(agent_id, True, body.reason, principal, command_key=idempotency_key)


@router.post("/agents/{agent_id}/resume", response_model=dict)
def resume(agent_id: str, request: Request, body: Reason = Reason(), principal: Principal = Depends(resolve_principal),
           idempotency_key: Annotated[str | None, Header(min_length=1)] = None):
    buyer = request.app.state.gateway.buyer
    buyer.scope(principal, admin=True)
    return buyer.control(agent_id, False, body.reason, principal, command_key=idempotency_key)


@router.post("/mandates/{jti}/revoke", response_model=dict)
def revoke(jti: str, request: Request, principal: Principal = Depends(resolve_principal),
           idempotency_key: Annotated[str | None, Header(min_length=1)] = None):
    buyer = request.app.state.gateway.buyer
    buyer.scope(principal, admin=True)
    return buyer.revoke(jti, principal, command_key=idempotency_key)


@router.post("/approvals/{order_id}/approve", response_model=dict, summary="Approve the current evidence revision within buyer scope")
def approve(order_id: str, body: ApprovalRequest, request: Request, principal: Principal = Depends(resolve_principal),
            idempotency_key: Annotated[str | None, Header(min_length=1)] = None):
    buyer = request.app.state.gateway.buyer
    buyer.scope(principal)
    return buyer.envelope(principal, order=request.app.state.gateway.orders.approve(
        order_id, body.approver, body.comment, body.evidence_revision, command_key=idempotency_key))


@router.post("/approvals/{order_id}/reject", response_model=dict)
def reject(order_id: str, body: ApprovalRequest, request: Request, principal: Principal = Depends(resolve_principal),
           idempotency_key: Annotated[str | None, Header(min_length=1)] = None):
    buyer = request.app.state.gateway.buyer
    buyer.scope(principal)
    return buyer.envelope(principal, order=request.app.state.gateway.orders.reject(
        order_id, body.approver, body.comment, command_key=idempotency_key))
