"""Lifecycle adapters around the existing OAuth servers; protocol behavior stays shared."""
import json
from dataclasses import replace
from datetime import timedelta
from secrets import token_urlsafe
from urllib.parse import urlsplit

from . import audit
from .db import DomainError
from .models import ConnectionRequest
from .connection_notifications_models import NotificationConsent
from .oauth import OAuth as OperatorOAuth, OAuthError as OperatorError, digest
from .oauth import now as oauth_now
from .oauth_audiences import OAuth as AudienceOAuth, OAuthError as AudienceError, RESOURCE_PATHS, redirect_to


def allowed_scopes(info, scopes, p):
    if (info["client_tier"] == "unverified" and RESOURCE_PATHS[urlsplit(info["resource"]).path] == "buyer"
            and not (p.operator or p.role == "tenant_admin")):
        return [s for s in ("lookup", "quotes", "offline_access") if s in scopes or "*" in scopes or "write" in scopes or "read" in scopes]
    return scopes


def recovery(gateway, conn, binding, principal):
    """Re-consent restores the same principal; purchasing objects never move or reset."""
    for row in conn.execute("SELECT c.* FROM connections c JOIN connection_trust t ON t.connection_id=c.connection_id WHERE t.state='REAUTH_REQUIRED' AND t.client_id=? AND c.agent_id=? AND c.tenant_id=? AND c.created_by=? AND c.revoked_at IS NULL",
                            (binding["client_id"], binding["agent_id"], binding["tenant_id"], binding["user_id"])).fetchall():
        resources = {r[0] for r in conn.execute("SELECT json_extract(data,'$.resource') FROM audience_oauth_tokens WHERE connection_id=? UNION SELECT resource FROM oauth_families WHERE connection_id=?", (row["connection_id"], row["connection_id"]))}
        expected_scopes = {"*"} if "mcp" in binding["scopes"] else set(s for s in binding["scopes"] if s != "offline_access")
        if binding["resource"] in resources and set(json.loads(row["customer_ids"])) == set(binding["customer_ids"]) and set(json.loads(row["scopes"])) == expected_scopes:
            cid = row["connection_id"]
            conn.execute("UPDATE connection_trust SET state='ACTIVE',confirmed=0,notification_consent=? WHERE connection_id=?", (json.dumps(binding["notification_consent"]), cid))
            preferences = gateway.notifications.prefs(cid, conn).model_copy(update={
                "consent": NotificationConsent.model_validate(binding["notifications"]), "reconsent_channels": ["mcp"]})
            gateway.notifications.configure(cid, preferences, principal)
            conn.execute("UPDATE supplier_execution_outbox SET status='pending' WHERE status='blocked' AND (json_extract(payload,'$.credential.connection_id')=? OR json_extract(payload,'$.order.context.connection_authority.connection_id')=?)", (cid, cid))
            return cid


def refresh(oauth, values, authorization=None, *, operator=False):
    g, trust = oauth.gateway, oauth.gateway.connection_trust
    is_refresh = values.get("grant_type") == "refresh_token"
    if not is_refresh:
        return super(TrustedOperatorOAuth if operator else TrustedAudienceOAuth, oauth).token(values, authorization) if operator else super(TrustedAudienceOAuth, oauth).token(values)
    client = oauth.authenticate_client(values, authorization)[0] if operator else values.get("client_id", "")
    token_digest = digest(values.get("refresh_token", ""))
    failure = result = None
    with g.db.connect(write=True) as conn:
        if operator:
            row = conn.execute("SELECT t.*,f.client_id,f.connection_id,f.resource,f.revoked_at AS family_revoked FROM oauth_tokens t JOIN oauth_families f ON f.id=t.family_id WHERE t.digest=? AND t.kind='refresh'", (token_digest,)).fetchone()
            valid = row and row["client_id"] == client and row["resource"] == values.get("resource")
        else:
            row = conn.execute("SELECT * FROM audience_oauth_tokens WHERE digest=? AND kind='refresh'", (token_digest,)).fetchone()
            data = json.loads(row["data"]) if row else {}
            valid = row and data["client_id"] == client and data["resource"] == values.get("resource")
        if not row:
            raise OperatorError("invalid_grant") if operator else AudienceError("invalid_grant")
        state = conn.execute("SELECT state FROM connection_trust WHERE connection_id=?", (row["connection_id"],)).fetchone()
        connection = conn.execute("SELECT revoked_at,expires_at FROM connections WHERE connection_id=?", (row["connection_id"],)).fetchone()
        live = connection and not connection[0] and (not connection[1] or connection[1] > audit.utcnow().isoformat()) and (not state or state[0] != "REAUTH_REQUIRED")
        if row["used_at"]:
            pair = trust.grace_pair(conn, token_digest, client) if valid and live and not row["revoked_at"] else None
            if operator and row["family_revoked"]:
                pair = None
            if pair:
                return pair
            if operator:
                oauth.revoke_family(conn, row["family_id"])
            else:
                conn.execute("UPDATE audience_oauth_tokens SET revoked_at=? WHERE family=?", (audit.utcnow().isoformat(), row["family"]))
            trust.reauth(conn, row["connection_id"])
            failure = OperatorError("invalid_grant") if operator else AudienceError("invalid_grant", "Refresh reuse requires re-consent")
        elif not valid or not live:
            raise OperatorError("invalid_grant") if operator else AudienceError("invalid_grant")
        else:
            if operator:
                result = super(TrustedOperatorOAuth, oauth).token(values, authorization)
            else:
                conn.execute("UPDATE audience_oauth_tokens SET revoked_at=? WHERE family=? AND kind='access'", (audit.utcnow().isoformat(), row["family"]))
                result = super(TrustedAudienceOAuth, oauth).token(values)
            trust.save_pair(conn, token_digest, client, result)
    if failure:
        raise failure
    return result


class TrustedAudienceOAuth(AudienceOAuth):
    def _connection(self, connection_id, data):
        p = super()._connection(connection_id, data)
        self.gateway.connection_trust.check(p)
        return p

    def register(self, client_id, metadata, *, dynamic=False):
        value = super().register(client_id, metadata, dynamic=dynamic)
        self.gateway.connection_trust.register_client(client_id, "dynamic" if dynamic else "pre_registered")
        return value

    async def client(self, client_id):
        value = await super().client(client_id)
        self.gateway.connection_trust.register_client(client_id, "metadata")
        return value

    def request(self, request_id, p):
        data = super().request(request_id, p)
        return {**data, **self.gateway.connection_trust.client_info(data["client"]["client_id"], data["client"], data["redirect_uri"])}

    def decide(self, request_id, p, body):
        if not body.approve:
            return super().decide(request_id, p, body)
        g, trust = self.gateway, self.gateway.connection_trust
        with self.db.connect(write=True) as conn:
            data = self.request(request_id, p)
            audience = RESOURCE_PATHS[urlsplit(data["resource"]).path]
            if audience == "operator" and not p.operator or audience != "operator" and p.operator:
                raise DomainError("HUMAN_REQUIRED", "Sign in to the requested audience", 403)
            scopes = body.scopes if body.scopes is not None else data["scope"].split()
            if not scopes or not set(scopes) <= set(data["scope"].split()):
                raise DomainError("INSUFFICIENT_SCOPE", "Consent can only narrow requested scopes", 403)
            scopes = allowed_scopes(data, scopes, p)
            customers = body.customer_ids if body.customer_ids is not None else list(p.customer_ids)
            if not p.operator and (not customers or not set(customers) <= set(p.customer_ids)):
                raise DomainError("CUSTOMER_NOT_IN_MANDATE", "Consent customers exceed tenant scope", 403)
            agent = body.agent_id
            if audience == "buyer":
                g.governance.member(p)
                if not agent:
                    raise DomainError("VALIDATION_ERROR", "Choose an agent", 422)
                known = conn.execute("SELECT * FROM tenant_agents WHERE agent_id=?", (agent,)).fetchone()
                if known:
                    g.governance.target(conn, agent, p)
                    if known["disabled_at"]:
                        raise DomainError("AGENT_NOT_FOUND", "Agent was not found", 404)
                elif not p.operator and p.role != "tenant_admin":
                    raise DomainError("TENANT_ADMIN_REQUIRED", "An administrator creates new agents", 403)
            else:
                agent = "oauth-human-" + token_urlsafe(8).lower().replace("_", "-")
            if audience == "operator" and not customers:
                customers = [r[0] for r in conn.execute("SELECT customer_id FROM tenant_customers")]
            binding = trust.bind_request(conn, request_id, {
                **{k: data[k] for k in ("redirect_uri", "challenge", "resource", "issuer")},
                "client_id": data["client"]["client_id"], "client": data["client"], "user_id": p.user_id,
                "tenant_id": p.tenant_id or "platform", "agent_id": agent, "customer_ids": customers,
                "scopes": scopes, "notification_consent": body.notification_consent,
                "notifications": body.notifications.model_dump(mode="json"),
            })
            issuer = replace(p, role="tenant_admin") if audience == "tenant" else p
            cid = recovery(g, conn, binding, issuer)
            if cid is None:
                c = g.connections.create(ConnectionRequest(agent_id=binding["agent_id"], client="mcp_other" if "/mcp" in binding["resource"] else "a2a",
                    type="mcp" if "/mcp" in binding["resource"] else "a2a", audience=audience, customer_ids=binding["customer_ids"],
                    scopes=[s for s in binding["scopes"] if s != "offline_access"], label=binding["client"]["client_name"],
                    notifications=NotificationConsent.model_validate(binding["notifications"])), issuer, binding["issuer"], True)
                cid = c.connection_id
            conn.execute("UPDATE connections SET type='oauth',credential_kind='oauth',key_digest=?,key_prefix='oauth' WHERE connection_id=?", (digest(token_urlsafe(48)), cid))
            trust.created(conn, cid)
            conn.execute("UPDATE connection_trust SET client_id=?,tier=?,order_granted=?,notification_consent=? WHERE connection_id=?",
                         (binding["client_id"], data["client_tier"], int(p.operator or p.role == "tenant_admin" or data["client_tier"] == "verified"), json.dumps(binding["notification_consent"]), cid))
            code = "vtg_code_" + token_urlsafe(32)
            grant = {**binding, "scope": " ".join(binding["scopes"])}
            conn.execute("INSERT INTO audience_oauth_codes VALUES(?,?,?,?,NULL)", (digest(code), cid, json.dumps(grant), (audit.utcnow()+timedelta(minutes=5)).isoformat()))
            conn.execute("UPDATE audience_oauth_requests SET data=?,used_at=? WHERE id=?", (json.dumps({**data, **binding}), audit.utcnow().isoformat(), request_id))
            audit.append(conn, p.user_id, "CONSENT_APPROVED", "Approved OAuth connection.", data={"tenant_id": binding["tenant_id"], "connection_id": cid, "audience": audience})
            return {"connection_id": cid, "redirect_url": redirect_to(binding["redirect_uri"], code=code, state=data.get("state"), iss=binding["issuer"])}

    def token(self, form):
        return refresh(self, form)


class TrustedOperatorOAuth(OperatorOAuth):
    def register(self, value, metadata=None, *, client_id=None):
        result = super().register(value, metadata, client_id=client_id)
        if metadata is None:
            self.gateway.connection_trust.register_client(result["client_id"], "pre_registered" if client_id else "dynamic")
        return result

    def client(self, client_id, *, discover=False):
        result = super().client(client_id, discover=discover)
        if discover and client_id.startswith("https://"):
            self.gateway.connection_trust.register_client(client_id, "metadata")
        return result

    def view(self, request_id):
        result = super().view(request_id)
        with self.gateway.db.connect() as conn:
            row = self.request_row(conn, request_id)
        return {**result, **self.gateway.connection_trust.client_info(row["client_id"], self.client(row["client_id"]), row["redirect_uri"])}

    def decide(self, request_id, body, principal):
        from .auth import require_operator
        from .tenancy import HOTEL
        require_operator(principal)
        with self.gateway.db.connect(write=True) as conn:
            row = self.request_row(conn, request_id, pending=True)
            if body.decision == "approve":
                agent = conn.execute("SELECT tenant_id FROM main.tenant_agents WHERE agent_id=?", (body.agent_id,)).fetchone()
                if body.agent_id and body.customer_ids and not agent:
                    tenants = {r[0] for customer in body.customer_ids for r in conn.execute("SELECT tenant_id FROM tenant_customers WHERE customer_id=?", (customer,))}
                    if len(tenants) != 1:
                        raise DomainError("CUSTOMER_NOT_IN_MANDATE", "One OAuth connection belongs to exactly one tenant", 403)
                    tid = next(iter(tenants))
                    conn.execute("INSERT INTO tenant_agents(agent_id,tenant_id,name,disabled_at) VALUES(?,?,?,NULL)", (body.agent_id, tid, body.agent_id))
                    self.gateway.governance.own_new_agent(conn, body.agent_id, tid, principal)
                    agent = (tid,)
                binding = self.gateway.connection_trust.bind_request(conn, request_id, {**dict(row), "user_id": principal.user_id or principal.id,
                    "tenant_id": agent[0] if agent else HOTEL, "agent_id": body.agent_id, "customer_ids": body.customer_ids,
                    "scopes": body.scopes if body.scopes is not None else row["scope"].split(), "notification_consent": body.notification_consent,
                    "notifications": body.notifications.model_dump(mode="json")})
                body = body.model_copy(update={k: binding[k] for k in ("agent_id", "customer_ids", "scopes", "notification_consent")})
                if row["resource"] != self.resource:
                    raise OperatorError("invalid_target")
                if not binding["agent_id"] or not binding["customer_ids"] or "mcp" not in binding["scopes"] or not set(binding["scopes"]) <= set(row["scope"].split()):
                    raise OperatorError("invalid_scope")
                self.gateway.connections.prepare_agent(conn, binding["agent_id"], binding["customer_ids"])
                recovered = recovery(self.gateway, conn, binding, principal)
                if recovered:
                    family, code = "of_" + token_urlsafe(24), token_urlsafe(32)
                    conn.execute("INSERT INTO oauth_families VALUES(?,?,?,?,?,?,NULL)", (family, recovered, binding["client_id"], binding["resource"], " ".join(binding["scopes"]), (oauth_now()+timedelta(days=30)).isoformat()))
                    conn.execute("INSERT INTO oauth_codes VALUES(?,?,?,?,NULL)", (digest(code), request_id, family, (oauth_now()+timedelta(minutes=5)).isoformat()))
                    conn.execute("UPDATE oauth_requests SET status='approved' WHERE id=?", (request_id,))
                    conn.execute("DELETE FROM oauth_browser_sessions WHERE request_id=?", (request_id,))
                    audit.append(conn, principal.user_id or principal.id, "OAUTH_CONSENT_APPROVED", "Re-consent restored the existing connection.", data={"connection_id": recovered})
                    return {"status": "approved", "connection_id": recovered,
                            "redirect_url": redirect_to(binding["redirect_uri"], code=code, iss=self.issuer, state=row["state"])}
            result = super().decide(request_id, body, principal)
            if result.get("connection_id"):
                cid = result["connection_id"]
                conn.execute("UPDATE connections SET created_by=? WHERE connection_id=?", (principal.user_id or principal.id, cid))
                info = self.gateway.connection_trust.client_info(row["client_id"], self.client(row["client_id"]), row["redirect_uri"])
                self.gateway.connection_trust.created(conn, cid, row["client_id"], info["client_tier"], consent=body.notification_consent)
            return result

    def token(self, values, authorization):
        return refresh(self, values, authorization, operator=True)
