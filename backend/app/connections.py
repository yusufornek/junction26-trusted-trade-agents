"""Operator-issued, one-time agent credentials over the shared command boundary."""
import json
from datetime import datetime, timedelta
from hashlib import sha256
from secrets import compare_digest, token_hex, token_urlsafe
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, Request
from typing import Annotated

from . import audit
from .auth import Principal, require_operator
from .db import DomainError
from .models import AgentMandate, Connection, ConnectionRequest, NewConnection
from .mandates import decode

router = APIRouter(prefix="/v1/connections", tags=["connections"])


def public_connection(row):
    data = {name: row[name] for name in Connection.model_fields}
    data["scopes"] = json.loads(data["scopes"])
    data["customer_ids"] = json.loads(data["customer_ids"])
    return Connection.model_validate(data)


def require_connection_admin(principal):
    if not principal.operator and not (principal.kind == "tenant_user" and principal.role in {"tenant_admin", "member", "approver"}):
        raise DomainError("TENANT_ADMIN_REQUIRED", "A member, tenant administrator or platform administrator is required", 403)
    return principal


class Connections:
    def __init__(self, gateway):
        self.gateway = gateway

    def _existing_mandate(self, conn, agent_id, policy):
        """Project known authority without granting or persisting purchasing rights.

        Successful VC history identifies an agent even after expiry/revocation.
        Requests must still present a valid VC; this projection never authorizes them.
        """
        row = policy.agents.get(agent_id)
        for stored in conn.execute("SELECT jwt FROM mandate_credentials ORDER BY last_used DESC"):
            subject = decode(stored[0])[1]["vc"]["credentialSubject"]
            if subject["agent_id"] != agent_id:
                continue
            grant = conn.execute("SELECT level FROM agent_autonomy WHERE agent_id=?", (agent_id,)).fetchone()
            buyer_level = subject.get("autonomy", policy.autonomy.default)
            operator_level = (row.autonomy or policy.autonomy.default) if row else (grant[0] if grant else buyer_level)
            customers = [subject["customer_id"]]
            if row and subject["customer_id"] not in row.customer_ids:
                raise DomainError("CUSTOMER_NOT_IN_MANDATE", "The current policy excludes the signed mandate customer", 403)
            return AgentMandate(customer_ids=customers,
                autonomy="approval_required" if "approval_required" in (buyer_level, operator_level) else "autonomous_within_limits",
                max_auto_total_eur=min(subject["max_auto_total_eur"], policy.approval.max_auto_total_eur,
                    row.max_auto_total_eur if row and row.max_auto_total_eur is not None else float("inf")),
                max_auto_daily_total_eur=min(subject["max_auto_daily_total_eur"],
                    row.max_auto_daily_total_eur if row else float("inf")))
        if row is None and conn.execute("SELECT 1 FROM connections WHERE agent_id=?", (agent_id,)).fetchone():
            # Removing a policy mandate must not be undone by credential issuance.
            raise DomainError("AGENT_NOT_REGISTERED", "The existing agent has no current mandate; restore authority explicitly", 403)
        return row

    def create(self, body: ConnectionRequest, principal: Principal, origin: str, mcp_served: bool):
        require_connection_admin(principal)
        if body.type == "webhook" and not body.webhook_url:
            raise DomainError("VALIDATION_ERROR", "webhook_url is required for a webhook connection", 422)
        if body.client not in {"claude_desktop", "claude_code", "cursor", "vscode", "chatgpt", "mcp_other", "rest", "curl", "python", "typescript", "openai_agents_sdk", "anthropic_api", "langchain", "a2a"}:
            raise DomainError("VALIDATION_ERROR", "Unsupported client; see /v1/connect/catalog", 422)
        tid = body.tenant_id if principal.operator and body.tenant_id else principal.tenant_id
        if not tid:
            from .tenancy import HOTEL
            tid = HOTEL
        if body.tenant_id and not principal.operator and body.tenant_id != principal.tenant_id:
            raise DomainError("TENANT_NOT_FOUND", "Tenant was not found", 404)
        if body.audience == "operator" and not principal.operator:
            raise DomainError("OPERATOR_REQUIRED", "Operator audience requires platform staff", 403)
        if body.audience == "operator":
            tid = "platform"
        if body.audience == "tenant" and principal.operator:
            raise DomainError("TENANT_ADMIN_REQUIRED", "Create tenant-audience keys while signed in as that tenant's administrator", 403)
        if body.audience != "buyer" and not principal.user_id:
            raise DomainError("HUMAN_REQUIRED", "Human audiences need a user identity", 403)
        if body.expires_at and body.expires_at <= audit.utcnow():
            raise DomainError("VALIDATION_ERROR", "Expiry must be in the future", 422)
        with self.gateway.db.connect(write=True) as conn:
            policy = self.gateway.policy.get(tenant_overlay=False)
            agent = conn.execute("SELECT * FROM tenant_agents WHERE agent_id=?", (body.agent_id,)).fetchone()
            if principal.operator and body.tenant_id is None and body.audience != "operator":
                customer = conn.execute("SELECT tenant_id FROM tenant_customers WHERE customer_id=?", (body.customer_ids[0],)).fetchone()
                tid = agent["tenant_id"] if agent else customer[0] if customer else tid
            if tid != "platform" and not conn.execute("SELECT 1 FROM tenants WHERE id=? AND disabled_at IS NULL", (tid,)).fetchone():
                raise DomainError("TENANT_NOT_FOUND", "Tenant was not found", 404)
            known = set(self.gateway.accounts.customers(tid))
            if agent and body.audience != "operator" and (agent["tenant_id"] != tid or agent["disabled_at"]):
                raise DomainError("AGENT_NOT_FOUND", "Agent was not found", 404)
            if agent and not principal.operator and principal.role != "tenant_admin":
                self.gateway.governance.target(conn, body.agent_id, principal)
            if not agent and body.audience != "operator":
                conn.execute("INSERT INTO tenant_agents(agent_id,tenant_id,name,disabled_at) VALUES(?,?,?,NULL)", (body.agent_id, tid, body.agent_id))
                self.gateway.governance.own_new_agent(conn, body.agent_id, tid, principal)
            if body.audience == "buyer":
                from .guardrail_profiles import register_owner
                register_owner(conn, body.agent_id, tid, principal.user_id)
            legacy_known = set(policy.customers)
            for mandate in policy.agents.values():
                legacy_known.update(mandate.customer_ids)
            for organisation in policy.organisations.values():
                legacy_known.update(organisation.customer_ids)
            legacy_known.update(row[0] for row in conn.execute("SELECT customer_id FROM tenant_customers"))
            if any(customer not in (legacy_known if body.audience == "operator" else known) for customer in body.customer_ids):
                raise DomainError("VALIDATION_ERROR", "customer_ids must contain known customer identifiers", 422)
            mandate = self._existing_mandate(conn, body.agent_id, policy)
            if mandate is None:
                mandate = AgentMandate(customer_ids=body.customer_ids, autonomy="approval_required",
                    max_auto_daily_total_eur=policy.approval.max_auto_daily_total_eur)
                # Enlist the policy projection in the enclosing command transaction.
                # Do not emit a policy diff containing optional free-text input.
                import yaml
                policy.agents[body.agent_id] = mandate
                self.gateway.policy._replace(yaml.safe_dump(
                    policy.model_dump(mode="json", exclude={"known_warning_codes", "tenant_anomaly_settings"}), sort_keys=False))
            if not agent and body.audience != "operator":
                owner = conn.execute("SELECT owner_user_id FROM tenant_agents WHERE agent_id=?", (body.agent_id,)).fetchone()[0]
                audit.append(conn, principal.user_id or principal.id, "AGENT_CREATED", "Created an owned connection agent.",
                    data={"agent_id": body.agent_id, "tenant_id": tid, "owner_user_id": owner, "status": "active"})
            connection_id, now = "con_" + token_hex(4), audit.utcnow()
            env = "sbx" if self.gateway.settings.environment == "sandbox" else "live"
            prefix = f"vtg_{env}_{connection_id}_"
            secret = prefix + token_urlsafe(32)
            conn.execute("INSERT INTO connections(connection_id,agent_id,client,label,key_prefix,key_digest,customer_ids,created_at,last_used_at,revoked_at,tenant_id,type,audience,scopes,created_by,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (connection_id, body.agent_id, body.client, body.label, secret[:8],
                 sha256(secret.encode()).hexdigest(), json.dumps(body.customer_ids), now.isoformat(), None, None,
                 tid, body.type, body.audience, json.dumps(body.scopes), principal.user_id or principal.id,
                 body.expires_at.isoformat() if body.expires_at else None))
            self.gateway.connection_trust.created(conn, connection_id)
            if body.type == "webhook":
                from dataclasses import replace
                from .webhooks import SubscriptionCreate
                subscriber = replace(principal, connection_id=connection_id, audience=body.audience,
                    tenant_id=None if body.audience == "operator" else tid,
                    agent_id=body.agent_id if body.audience == "buyer" else principal.agent_id,
                    kind="agent" if body.audience == "buyer" else principal.kind,
                    operator=principal.operator if body.audience != "buyer" else False,
                    user_id=principal.user_id if body.audience != "buyer" else None,
                    customer_ids=tuple(body.customer_ids))
                self.gateway.webhooks.create(SubscriptionCreate(url=body.webhook_url, customer_ids=body.customer_ids,
                    audience=body.audience, event_types=body.event_types or ["order.approval_required", "order.confirmed", "order.rejected"]), subscriber)
            if not conn.execute("SELECT 1 FROM connections WHERE agent_id=? AND client=? AND connection_id<>?", (body.agent_id, body.client, connection_id)).fetchone():
                self.gateway.governance.alert(conn, body.agent_id, "connection.new_client", {"connection_id": connection_id, "client": body.client},
                                              dedup=f"client:{body.agent_id}:{body.client}")
            audit.append(conn, "human:operator" if principal.user_id == "legacy-operator" else "human:" + (principal.user_id or principal.id), "CONNECTION_CREATED", "Issued an agent connection.",
                data={"connection_id": connection_id, "agent_id": body.agent_id,
                      "client": body.client, "tenant_id": tid, "key_prefix": secret[:8]})
            from .tenancy import TARGET_TENANT
            target_token = TARGET_TENANT.set(tid)
            try:
                effective = self.gateway.policy.get()
                mandate = mandate.model_copy(deep=True)
                if effective._tenant_daily_ceiling is not None:
                    mandate.max_auto_daily_total_eur = min(mandate.max_auto_daily_total_eur, effective._tenant_daily_ceiling)
            finally:
                TARGET_TENANT.reset(target_token)
            return NewConnection(connection_id=connection_id, agent_id=body.agent_id, client=body.client,
                key_prefix=secret[:8], created_at=now, key=secret, tenant_id=tid, type=body.type, audience=body.audience,
                scopes=body.scopes, created_by=principal.user_id or principal.id, expires_at=body.expires_at,
                customer_ids=body.customer_ids,
                mcp_url=origin + "/mcp" + ("" if body.audience == "buyer" else "/"+body.audience) if mcp_served and (self.gateway.settings.public_url or
                    urlsplit(origin).hostname in {"localhost", "127.0.0.1", "::1"}) else None,
                rest_base_url=origin + "/v1" + ("" if body.audience == "buyer" else "/"+body.audience),
                mandate=mandate.model_copy(update={"autonomy": "approval_required"}))

    def prepare_agent(self, conn, agent_id, customer_ids):
        """Shared API-key / OAuth consent authority rules, in the caller's transaction."""
        policy = self.gateway.policy.get()
        known = set(policy.customers)
        for mandate in policy.agents.values():
            known.update(mandate.customer_ids)
        for organisation in policy.organisations.values():
            known.update(organisation.customer_ids)
        if any(customer not in known for customer in customer_ids):
            raise DomainError("VALIDATION_ERROR", "customer_ids must contain known customer identifiers", 422)
        mandate = self._existing_mandate(conn, agent_id, policy)
        if mandate is None:
            mandate = AgentMandate(customer_ids=customer_ids, autonomy="approval_required",
                max_auto_daily_total_eur=policy.approval.max_auto_daily_total_eur)
            # Enlist the policy projection in the enclosing command transaction.
            # Do not emit a policy diff containing optional free-text input.
            import yaml
            policy.agents[agent_id] = mandate
            self.gateway.policy._replace(yaml.safe_dump(
                policy.model_dump(mode="json", exclude={"known_warning_codes", "tenant_anomaly_settings"}), sort_keys=False))
        return mandate

    def list(self, principal):
        require_connection_admin(principal)
        with self.gateway.db.connect() as conn:
            return [public_connection(row) for row in conn.execute(
                "SELECT * FROM connections WHERE (? IS NULL OR tenant_id=?) ORDER BY created_at DESC, rowid DESC",
                (None if principal.operator else principal.tenant_id, principal.tenant_id))
                if principal.operator or principal.role == "tenant_admin" or conn.execute("SELECT 1 FROM tenant_agents WHERE agent_id=? AND owner_user_id=?", (row["agent_id"], principal.user_id)).fetchone()]

    def revoke(self, connection_id, principal):
        require_connection_admin(principal)
        self.gateway.limiter.consume(principal.id, "mutation")
        with self.gateway.db.connect(write=True) as conn:
            self.get(connection_id, principal)
            row = conn.execute("SELECT * FROM connections WHERE connection_id=? AND (? IS NULL OR tenant_id=?)",
                               (connection_id, None if principal.operator else principal.tenant_id, principal.tenant_id)).fetchone()
            if row is None:
                raise DomainError("CONNECTION_NOT_FOUND", "Connection was not found", 404)
            if row["revoked_at"] is None:
                conn.execute("UPDATE connections SET revoked_at=? WHERE connection_id=?",
                             (audit.utcnow().isoformat(), connection_id))
                self.gateway.oauth.revoke_connection(conn, connection_id)
                audit.append(conn, "human:operator" if principal.user_id == "legacy-operator" else "human:" + (principal.user_id or principal.id), "CONNECTION_REVOKED", "Revoked an agent connection.",
                    data={"connection_id": connection_id, "agent_id": row["agent_id"],
                          "client": row["client"], "key_prefix": row["key_prefix"], "tenant_id": row["tenant_id"]})
            return public_connection(conn.execute("SELECT * FROM connections WHERE connection_id=?",
                                                  (connection_id,)).fetchone())

    def resolve(self, secret):
        digest = sha256(secret.encode()).hexdigest()
        with self.gateway.db.connect() as conn:
            row = conn.execute("SELECT * FROM connections WHERE key_digest=?", (digest,)).fetchone()
            if row is None:
                old = conn.execute("SELECT connection_id FROM connection_secrets WHERE digest=? AND expires_at>?", (digest, audit.utcnow().isoformat())).fetchone()
                row = conn.execute("SELECT * FROM connections WHERE connection_id=?", (old[0],)).fetchone() if old else None
        if row is None or row["credential_kind"] != "api_key" or row["revoked_at"] is not None or row["expires_at"] and datetime.fromisoformat(row["expires_at"]) <= audit.utcnow():
            raise DomainError("UNAUTHENTICATED", "A valid Bearer credential is required; expired or revoked credentials must be replaced by the issuer. See /developers#failures", 401)
        now = audit.utcnow()
        cutoff = (now - timedelta(minutes=1)).isoformat()
        if row["last_used_at"] is None or datetime.fromisoformat(row["last_used_at"]) <= now - timedelta(minutes=1):
            with self.gateway.db.connect(write=True, notify=False) as conn:
                # The conditional write also bounds updates from simultaneous requests.
                conn.execute("UPDATE connections SET last_used_at=? WHERE connection_id=? AND revoked_at IS NULL "
                             "AND (last_used_at IS NULL OR last_used_at<=?)", (now.isoformat(), row["connection_id"], cutoff))
                # A revoke that won the writer lock takes effect on this request too.
                if conn.execute("SELECT revoked_at FROM connections WHERE connection_id=?",
                                (row["connection_id"],)).fetchone()[0] is not None:
                    raise DomainError("UNAUTHENTICATED", "A valid Bearer credential is required", 401)
        return Principal(row["agent_id"], credential_ref=row["connection_id"], tenant_id=row["tenant_id"],
                         user_id=row["created_by"] if row["audience"] != "buyer" else None,
                         connection_id=row["connection_id"], customer_ids=tuple(json.loads(row["customer_ids"])),
                         audience=row["audience"], scopes=tuple(json.loads(row["scopes"])))

    def get(self, connection_id, principal):
        require_connection_admin(principal)
        with self.gateway.db.connect() as conn:
            row = conn.execute("SELECT * FROM connections WHERE connection_id=? AND (? IS NULL OR tenant_id=?)",
                               (connection_id, None if principal.operator else principal.tenant_id, principal.tenant_id)).fetchone()
        if row is None:
            raise DomainError("CONNECTION_NOT_FOUND", "Connection was not found", 404)
        if not principal.operator and principal.role != "tenant_admin":
            with self.gateway.db.connect() as conn:
                self.gateway.governance.target(conn, row["agent_id"], principal)
        return public_connection(row)

    def rotate(self, connection_id, principal, overlap_hours=0):
        require_connection_admin(principal)
        if not 0 <= overlap_hours <= 24:
            raise DomainError("VALIDATION_ERROR", "Overlap must be between zero and 24 hours", 422)
        with self.gateway.db.connect(write=True) as conn:
            record = self.get(connection_id, principal)
            row = conn.execute("SELECT * FROM connections WHERE connection_id=?", (connection_id,)).fetchone()
            if row["revoked_at"] or row["expires_at"] and datetime.fromisoformat(row["expires_at"]) <= audit.utcnow():
                raise DomainError("CONNECTION_REVOKED", "Create a new connection", 409)
            until = audit.utcnow() + timedelta(hours=overlap_hours)
            conn.execute("INSERT INTO connection_secrets VALUES(?,?,?)", (row["key_digest"], connection_id, until.isoformat()))
            key = f"vtg_{'sbx' if self.gateway.settings.environment == 'sandbox' else 'live'}_{connection_id}_" + token_urlsafe(32)
            conn.execute("UPDATE connections SET key_digest=?,key_prefix=? WHERE connection_id=?", (sha256(key.encode()).hexdigest(), key[:8], connection_id))
            audit.append(conn, principal.user_id or principal.id, "CONNECTION_ROTATED", "Rotated connection credential.", data={"connection_id": connection_id, "tenant_id": record.tenant_id})
            return {**record.model_dump(mode="json"), "key": key, "old_key_valid_until": until}


@router.post("", response_model=NewConnection, status_code=201,
             summary="Issue a connection key once; idempotent replays return key: null")
def create_connection(body: ConnectionRequest, request: Request,
                      idempotency_key: Annotated[str, Header(min_length=1)],
                      principal: Principal = Depends(require_operator)):
    service = request.app.state.gateway
    origin = service.settings.public_url or f"{request.url.scheme}://{request.url.netloc}"
    return service.connections.create(body, principal, origin,
        request.app.state.mcp_served, command_key=idempotency_key)


@router.get("", response_model=list[Connection], summary="List all connections newest first; no secrets or digests")
def list_connections(request: Request, principal: Principal = Depends(require_operator)):
    return request.app.state.gateway.connections.list(principal)


@router.delete("/{connection_id}", response_model=Connection,
               summary="Revoke a connection and its OAuth token families immediately; retain agent mandate and history")
def revoke_connection(connection_id: str, request: Request, principal: Principal = Depends(require_operator)):
    return request.app.state.gateway.connections.revoke(connection_id, principal)
