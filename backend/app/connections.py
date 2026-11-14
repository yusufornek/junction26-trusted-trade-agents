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
    return Connection.model_validate({name: row[name] for name in Connection.model_fields})


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
        require_operator(principal)
        with self.gateway.db.connect(write=True) as conn:
            mandate = self.prepare_agent(conn, body.agent_id, body.customer_ids)
            secret = "vak_" + token_urlsafe(32)
            connection_id, now = "con_" + token_hex(4), audit.utcnow()
            conn.execute("INSERT INTO connections (connection_id,agent_id,client,label,key_prefix,key_digest,"
                         "customer_ids,created_at,last_used_at,revoked_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (connection_id, body.agent_id, body.client, body.label, secret[:8],
                 sha256(secret.encode()).hexdigest(), json.dumps(body.customer_ids), now.isoformat(), None, None))
            audit.append(conn, "human:operator", "CONNECTION_CREATED", "Issued an agent connection.",
                data={"connection_id": connection_id, "agent_id": body.agent_id,
                      "client": body.client, "key_prefix": secret[:8]})
            return NewConnection(connection_id=connection_id, agent_id=body.agent_id, client=body.client,
                key_prefix=secret[:8], created_at=now, key=secret,
                mcp_url=origin + "/mcp" if mcp_served and (self.gateway.settings.public_url or
                    urlsplit(origin).hostname in {"localhost", "127.0.0.1", "::1"}) else None,
                rest_base_url=origin + "/v1",
                mandate=mandate.model_copy(update={"autonomy": mandate.autonomy or self.gateway.policy.get().autonomy.default}))

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
                policy.model_dump(mode="json", exclude={"known_warning_codes"}), sort_keys=False))
        return mandate

    def list(self, principal):
        require_operator(principal)
        with self.gateway.db.connect() as conn:
            return [public_connection(row) for row in conn.execute(
                "SELECT * FROM connections ORDER BY created_at DESC, rowid DESC")]

    def revoke(self, connection_id, principal):
        require_operator(principal)
        self.gateway.limiter.consume(principal.id, "mutation")
        with self.gateway.db.connect(write=True) as conn:
            row = conn.execute("SELECT * FROM connections WHERE connection_id=?", (connection_id,)).fetchone()
            if row is None:
                raise DomainError("CONNECTION_NOT_FOUND", "Connection was not found", 404)
            if row["revoked_at"] is None:
                conn.execute("UPDATE connections SET revoked_at=? WHERE connection_id=?",
                             (audit.utcnow().isoformat(), connection_id))
                self.gateway.oauth.revoke_connection(conn, connection_id)
                audit.append(conn, "human:operator", "CONNECTION_REVOKED", "Revoked an agent connection.",
                    data={"connection_id": connection_id, "agent_id": row["agent_id"],
                          "client": row["client"], "key_prefix": row["key_prefix"]})
            return public_connection(conn.execute("SELECT * FROM connections WHERE connection_id=?",
                                                  (connection_id,)).fetchone())

    def resolve(self, secret):
        digest = sha256(secret.encode()).hexdigest()
        with self.gateway.db.connect() as conn:
            row = conn.execute("SELECT * FROM connections WHERE key_digest=?", (digest,)).fetchone()
        if row is None or row["credential_kind"] != "api_key" or not compare_digest(digest, row["key_digest"]) or row["revoked_at"] is not None:
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
        return Principal(row["agent_id"], credential_ref=row["connection_id"])


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
