"""Connection-local authority, publisher identity and durable lifecycle decisions (62)."""
import hashlib
import json
import os
import re
import unicodedata
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from . import audit
from .auth import resolve_principal
from .db import DomainError
from .foundation import PRINCIPAL, EVENT_ACTIONS

CONTEXT = ContextVar("connection_trust_context", default=None)
INTERNAL = ContextVar("connection_trust_internal", default=False)
EVENTS = {
    "CONNECTION_REAUTH_REQUIRED": "connection.reauth_required",
    "CONCURRENT_CONTEXT": "connection.concurrent_context",
    "CONNECTION_CONTINUITY_CONFIRMED": "connection.continuity_confirmed",
    "CONNECTION_CONTINUITY_REQUIRED": "connection.continuity_required",
}
from .recovery import RECOVERY, RECOVERY_V2
for _code in ("CONCURRENT_CONTEXT", "OWNER_REQUIRED", "CONTEXT_REQUIRED", "CONFIRMATION_REQUIRED"):
    RECOVERY[_code] = RECOVERY_V2[_code] = (False, "CONTACT_ADMIN")
SCHEMA = """
CREATE TABLE IF NOT EXISTS connection_trust_clients(
 id TEXT PRIMARY KEY, registration TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS connection_trust(
 connection_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, tier TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'ACTIVE', confirmed INTEGER NOT NULL DEFAULT 0,
 order_granted INTEGER NOT NULL DEFAULT 1, confirmed_context TEXT,
 notification_consent TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS connection_trust_contexts(
 connection_id TEXT NOT NULL, id TEXT NOT NULL, network TEXT NOT NULL,
 protocol TEXT NOT NULL, client TEXT NOT NULL, first_seen TEXT NOT NULL,
 last_seen TEXT NOT NULL, PRIMARY KEY(connection_id,id));
CREATE TABLE IF NOT EXISTS connection_trust_requests(
 id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS connection_trust_refresh(
 digest TEXT PRIMARY KEY, context TEXT NOT NULL, client TEXT NOT NULL,
 used_at TEXT NOT NULL, expires_at TEXT NOT NULL, successor BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS connection_trust_notifications(
 id INTEGER PRIMARY KEY AUTOINCREMENT, connection_id TEXT NOT NULL,
 tenant_id TEXT, owner_user_id TEXT, event TEXT NOT NULL, created_at TEXT NOT NULL,
 resolved_at TEXT);
"""


def normalized(value):
    # Common Greek/Cyrillic visual confusables, applied to every publisher/name.
    confusables = str.maketrans({
        "А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H", "І": "I", "Ј": "J", "К": "K",
        "М": "M", "О": "O", "Р": "P", "Ѕ": "S", "Т": "T", "Х": "X", "У": "Y",
        "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y", "і": "i",
        "ј": "j", "ѕ": "s", "ӏ": "l", "ԁ": "d", "ԛ": "q", "ԝ": "w",
        "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M",
        "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X", "ο": "o", "ρ": "p", "ι": "i", "ϲ": "c",
    })
    value = unicodedata.normalize("NFKD", value.translate(confusables)).translate(confusables).casefold()
    return re.sub(r"[^a-z0-9]", "", value)


def resembles(name, publisher):
    name, publisher = normalized(name), normalized(publisher)
    if not publisher:
        return False
    if publisher in name:
        return True
    return any(SequenceMatcher(None, publisher, name[start:start+length]).ratio() >= .8
               for length in range(max(1, len(publisher)-1), len(publisher)+2)
               for start in range(max(0, len(name)-length+1)))


def context(network="local", protocol="internal", client="local"):
    values = {"network": str(network)[:200], "protocol": str(protocol)[:80], "client": str(client)[:300]}
    return {**values, "id": hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()}


class ConnectionTrust:
    grace = 30

    def __init__(self, gateway):
        self.gateway, self.db = gateway, gateway.db
        self.clock = lambda: datetime.now(timezone.utc)
        self.seeded_agents = {"hotel-chain-agent", "hospital-kitchen-agent", "web-console"}
        if gateway.settings.keys:
            self.seeded_agents.update(gateway.settings.keys.agent_ids)
        # Publisher membership is deployment-controlled, never client metadata.
        self.publishers = {"openai.com": "OpenAI", "chatgpt.com": "OpenAI", "anthropic.com": "Anthropic", "claude.ai": "Anthropic"}
        configured = os.getenv("OAUTH_VERIFIED_PUBLISHERS", "")
        if configured:
            self.publishers = json.loads(configured)
        with self.db.connect(write=True, notify=False) as conn:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute("INSERT OR IGNORE INTO workspace VALUES('connection_trust_seeded_agents',?)",
                         (json.dumps({a: {"confirmed": True, "client_id": "deployment"} for a in sorted(self.seeded_agents)}),))
            for row in conn.execute("SELECT client_id,created_at FROM oauth_clients").fetchall():
                cid = row["client_id"]
                kind = "metadata" if cid.startswith("https://") else "dynamic" if cid.startswith("cli_") else "pre_registered"
                conn.execute("INSERT OR IGNORE INTO connection_trust_clients VALUES(?,?,?)", (cid, kind, row["created_at"]))
            for row in conn.execute("SELECT id FROM audience_oauth_clients").fetchall():
                cid = row[0]
                kind = "metadata" if cid.startswith("https://") else "dynamic" if cid.startswith(("cli_", "client_")) else "pre_registered"
                conn.execute("INSERT OR IGNORE INTO connection_trust_clients VALUES(?,?,?)", (cid, kind, audit.utcnow().isoformat()))
            for connection in conn.execute("SELECT * FROM connections").fetchall():
                cid, client_id, tier = connection["connection_id"], connection["client"], "verified"
                if connection["credential_kind"] == "oauth":
                    grant = conn.execute("SELECT data FROM audience_oauth_tokens WHERE connection_id=? UNION ALL SELECT data FROM audience_oauth_codes WHERE connection_id=? LIMIT 1", (cid, cid)).fetchone()
                    legacy = conn.execute("SELECT f.client_id,r.redirect_uri FROM oauth_families f LEFT JOIN oauth_codes c ON c.family_id=f.id LEFT JOIN oauth_requests r ON r.id=c.request_id WHERE f.connection_id=? LIMIT 1", (cid,)).fetchone()
                    data = json.loads(grant[0]) if grant else dict(legacy) if legacy else {}
                    client_id = data.get("client_id", client_id)
                    stored = conn.execute("SELECT data FROM audience_oauth_clients WHERE id=? UNION ALL SELECT metadata FROM oauth_clients WHERE client_id=? LIMIT 1", (client_id, client_id)).fetchone()
                    metadata = json.loads(stored[0]) if stored else {"client_name": client_id}
                    tier = self.client_info(client_id, metadata, data.get("redirect_uri") or "")["client_tier"]
                self.created(conn, cid, client_id, tier)
            # Separate encryption key: rotation of receipt keys must not break a
            # still-live refresh grace window. Creation is under the DB writer lock.
            key_path = self.db.path.parent / "connection-trust-refresh.key"
            if not key_path.exists():
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(Fernet.generate_key())
            self.cipher = Fernet(key_path.read_bytes())
        EVENT_ACTIONS.update(EVENTS)

    def register_client(self, client_id, registration):
        with self.db.connect(write=True, notify=False) as conn:
            conn.execute("INSERT INTO connection_trust_clients VALUES(?,?,?) ON CONFLICT(id) DO NOTHING",
                         (client_id, registration, audit.utcnow().isoformat()))

    def client_info(self, client_id, metadata, redirect):
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM connection_trust_clients WHERE id=?", (client_id,)).fetchone()
        host = urlsplit(redirect).hostname
        document_host = urlsplit(client_id).hostname
        verified = bool(row and row["registration"] == "pre_registered" or
                        row and row["registration"] == "metadata" and document_host in self.publishers and document_host == host)
        name = metadata["client_name"]
        publisher = next((publisher for publisher in dict.fromkeys(self.publishers.values()) if resembles(name, publisher)), None)
        return {"client_tier": "verified" if verified else "unverified", "redirect_host": host,
                "registered_at": row["created_at"] if row else None,
                "verification_label": "verified" if verified else "not verified" + (" — not " + publisher if publisher else ""),
                "logo": None}

    def created(self, conn, cid, client_id=None, tier="verified", order_granted=True, consent=None):
        row = conn.execute("SELECT * FROM connections WHERE connection_id=?", (cid,)).fetchone()
        conn.execute("INSERT OR IGNORE INTO connection_trust(connection_id,client_id,tier,order_granted,notification_consent) VALUES(?,?,?,?,?)",
                     (cid, client_id or row["client"], tier, int(order_granted), json.dumps(consent or {})))

    def bind_request(self, conn, rid, data):
        """Persist the decision's full binding before deriving a connection or code."""
        conn.execute("INSERT INTO connection_trust_requests VALUES(?,?)", (rid, json.dumps({"request_id": rid, **data})))
        return json.loads(conn.execute("SELECT data FROM connection_trust_requests WHERE id=?", (rid,)).fetchone()[0])

    def event(self, conn, cid, name, actor="system:connection-trust"):
        row = conn.execute("SELECT c.agent_id,c.tenant_id,a.owner_user_id FROM connections c LEFT JOIN main.tenant_agents a ON a.agent_id=c.agent_id WHERE c.connection_id=?", (cid,)).fetchone()
        if not row:
            return
        data = {"connection_id": cid, **dict(row)}
        audit.append(conn, actor, name, name.replace("_", " ").capitalize() + ".", data=data)
        conn.execute("INSERT INTO connection_trust_notifications(connection_id,tenant_id,owner_user_id,event,created_at) VALUES(?,?,?,?,?)",
                     (cid, row["tenant_id"], row["owner_user_id"], name, audit.utcnow().isoformat()))

    def reauth(self, conn, cid):
        row = conn.execute("SELECT state FROM connection_trust WHERE connection_id=?", (cid,)).fetchone()
        if row and row[0] != "REAUTH_REQUIRED":
            conn.execute("UPDATE connection_trust SET state='REAUTH_REQUIRED',confirmed=0 WHERE connection_id=?", (cid,))
            self.cancel_push(conn, cid=cid)
            self.event(conn, cid, "CONNECTION_REAUTH_REQUIRED")

    def check(self, p, conn=None, *, mutation=False):
        if p is None:
            return
        if conn is None:
            with self.db.connect() as conn:
                return self.check(p, conn, mutation=mutation)
        if p.kind == "agent":
            self.gateway.governance.check_active(p.agent_id, conn)
        if not p.connection_id:
            return
        row = conn.execute("SELECT c.*,t.state,t.confirmed FROM connections c LEFT JOIN connection_trust t ON t.connection_id=c.connection_id WHERE c.connection_id=?", (p.connection_id,)).fetchone()
        if (not row or row["revoked_at"] or row["expires_at"] and row["expires_at"] <= audit.utcnow().isoformat() or
                row["agent_id"] != p.agent_id or row["state"] == "REAUTH_REQUIRED"):
            raise DomainError("UNAUTHENTICATED", "Connection requires fresh consent", 401)
        if mutation and row["state"] == "READ_ONLY":
            raise DomainError("CONCURRENT_CONTEXT", "Connection is read-only until its owner confirms the runtime", 409)
        if (not set(p.customer_ids) <= set(json.loads(row["customer_ids"])) or
                         "*" not in json.loads(row["scopes"]) and not set(p.scopes) <= set(json.loads(row["scopes"]))):
            raise DomainError("INSUFFICIENT_SCOPE", "Connection authority changed before commit", 403)
        return row

    def seen(self, p, source=None):
        if not p.connection_id or p.kind != "agent":
            self.check(p)
            return
        ctx = CONTEXT.get() or context(source or "local")
        failure = None
        internal = INTERNAL.set(True)
        try:
            with self.db.connect(write=True) as conn:
                self.check(p, conn)
                self.created(conn, p.connection_id)
                trust = conn.execute("SELECT * FROM connection_trust WHERE connection_id=?", (p.connection_id,)).fetchone()
                old = conn.execute("SELECT * FROM connection_trust_contexts WHERE connection_id=? AND id=?", (p.connection_id, ctx["id"])).fetchone()
                others = conn.execute("SELECT * FROM connection_trust_contexts WHERE connection_id=? AND id<>? AND last_seen>=?", (p.connection_id, ctx["id"], (self.clock()-timedelta(seconds=self.grace)).isoformat())).fetchall()
                had_context = conn.execute("SELECT 1 FROM connection_trust_contexts WHERE connection_id=?", (p.connection_id,)).fetchone()
                now = self.clock().isoformat()
                conn.execute("INSERT INTO connection_trust_contexts VALUES(?,?,?,?,?,?,?) ON CONFLICT(connection_id,id) DO UPDATE SET last_seen=excluded.last_seen",
                             (p.connection_id, ctx["id"], ctx["network"], ctx["protocol"], ctx["client"], now, now))
                if others and trust["state"] != "READ_ONLY":
                    conn.execute("UPDATE connection_trust SET state='READ_ONLY',confirmed=0 WHERE connection_id=?", (p.connection_id,))
                    self.event(conn, p.connection_id, "CONCURRENT_CONTEXT")
                    failure = DomainError("CONCURRENT_CONTEXT", "Credential is active in two contexts; owner confirmation required", 409)
                elif not old and had_context:
                    conn.execute("UPDATE connection_trust SET confirmed=0 WHERE connection_id=?", (p.connection_id,))
                    self.event(conn, p.connection_id, "CONNECTION_CONTINUITY_REQUIRED")
                elif not had_context and trust["confirmed"]:
                    conn.execute("UPDATE connection_trust SET confirmed_context=? WHERE connection_id=?", (ctx["id"], p.connection_id))
        finally:
            INTERNAL.reset(internal)
        if failure:
            raise failure

    def is_live(self, p):
        try:
            self.check(p)
            return True
        except DomainError:
            return False

    def heartbeat(self, p):
        ctx = CONTEXT.get()
        if not ctx or not p or not p.connection_id or p.kind != "agent":
            return
        with self.db.connect() as conn:
            row = conn.execute("SELECT last_seen FROM connection_trust_contexts WHERE connection_id=? AND id=?", (p.connection_id, ctx["id"])).fetchone()
        if row and row[0] <= (self.clock()-timedelta(seconds=10)).isoformat():
            self.seen(p)

    def approval_required(self, agent_id, conn=None):
        p = PRINCIPAL.get()
        if not p or p.kind != "agent" or p.agent_id != agent_id:
            return False
        if not p.connection_id:
            return agent_id not in self.seeded_agents
        with self.db.connect() as read:
            row = read.execute("SELECT * FROM connection_trust WHERE connection_id=?", (p.connection_id,)).fetchone()
        return not row or not row["confirmed"] or row["tier"] == "unverified"

    def require_order(self, p, conn):
        self.check(p, conn, mutation=True)
        if p and p.connection_id:
            row = conn.execute("SELECT order_granted FROM connection_trust WHERE connection_id=?", (p.connection_id,)).fetchone()
            if row and not row[0]:
                raise DomainError("TENANT_ADMIN_REQUIRED", "A tenant administrator must grant order scope to this client", 403)

    def epoch(self, conn):
        return tuple(tuple(row) for row in conn.execute("SELECT layer,target,version_id FROM main.governance_heads ORDER BY layer,target"))

    def before_commit(self, conn, epoch, *, mutation=True):
        if INTERNAL.get():
            return
        p = PRINCIPAL.get()
        if p and p.kind == "agent":
            self.check(p, conn, mutation=mutation and conn.total_changes > 0)
            if epoch is not None and self.epoch(conn) != epoch:
                raise DomainError("POLICY_CHANGED", "Policy epoch changed before commit; retry under current authority", 409)
        conn.execute("DELETE FROM connection_trust_refresh WHERE expires_at<?", (self.clock().isoformat(),))
        self.cancel_push(conn)

    def cancel_push(self, conn, cid=None):
        for row in conn.execute("SELECT id,data FROM main.webhook_subscriptions WHERE json_extract(data,'$.enabled')=1").fetchall():
            sub = json.loads(row["data"])
            if cid and sub.get("connection_id") != cid:
                continue
            invalid = False
            if sub.get("connection_id"):
                target = conn.execute("SELECT c.*,t.state FROM main.connections c LEFT JOIN connection_trust t ON t.connection_id=c.connection_id WHERE c.connection_id=?", (sub["connection_id"],)).fetchone()
                invalid = not target or bool(target["revoked_at"]) or target["state"] == "REAUTH_REQUIRED" or bool(target["expires_at"] and target["expires_at"] <= audit.utcnow().isoformat())
            if sub.get("owner_kind") == "agent":
                try:
                    self.gateway.governance.check_active(sub["owner"], conn)
                except DomainError:
                    invalid = True
            if invalid:
                sub["enabled"] = False
                conn.execute("UPDATE main.webhook_subscriptions SET data=? WHERE id=?", (json.dumps(sub), row["id"]))
                conn.execute("UPDATE main.event_consumers SET enabled=0 WHERE name=?", ("webhook:"+row["id"],))
                conn.execute("UPDATE main.outbox SET status='skipped' WHERE consumer=? AND status='pending'", ("webhook:"+row["id"],))
                conn.execute("DELETE FROM main.a2a_push_configs WHERE subscription_id=?", (row["id"],))

    def refresh_context(self):
        return (CONTEXT.get() or context())["id"]

    def grace_pair(self, conn, token_digest, client):
        conn.execute("DELETE FROM connection_trust_refresh WHERE expires_at<?", (self.clock().isoformat(),))
        row = conn.execute("SELECT * FROM connection_trust_refresh WHERE digest=?", (token_digest,)).fetchone()
        if row and row["client"] == client and row["context"] == self.refresh_context() and row["expires_at"] >= self.clock().isoformat():
            return json.loads(self.cipher.decrypt(row["successor"]))

    def save_pair(self, conn, token_digest, client, pair):
        now = self.clock()
        conn.execute("INSERT INTO connection_trust_refresh VALUES(?,?,?,?,?,?)", (token_digest, self.refresh_context(), client,
                     now.isoformat(), (now+timedelta(seconds=self.grace)).isoformat(), self.cipher.encrypt(json.dumps(pair).encode())))

    def health(self, cid):
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM connection_trust WHERE connection_id=?", (cid,)).fetchone()
            c = conn.execute("SELECT * FROM connections WHERE connection_id=?", (cid,)).fetchone()
            history = [dict(r) for r in conn.execute("SELECT * FROM connection_trust_contexts WHERE connection_id=? ORDER BY first_seen,id", (cid,))]
            notes = [dict(r) for r in conn.execute("SELECT id,event,created_at,resolved_at FROM connection_trust_notifications WHERE connection_id=? ORDER BY id", (cid,))]
        data = dict(row) if row else {"client_id": c["client"], "tier": "verified", "state": "ACTIVE", "confirmed": False}
        data["confirmed"] = bool(data["confirmed"])
        if "notification_consent" in data:
            data["notification_consent"] = json.loads(data["notification_consent"])
        if c["revoked_at"]:
            data["state"] = "REVOKED"
        elif c["expires_at"] and c["expires_at"] <= audit.utcnow().isoformat():
            data["state"] = "EXPIRED"
        else:
            try:
                self.gateway.governance.check_active(c["agent_id"])
            except DomainError as error:
                data["state"] = "PAUSED" if error.status == 423 else "REVOKED"
        return {**data, "context_history": history, "notifications": notes}

    def inbox(self, p):
        with self.db.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT id,connection_id,event,created_at,resolved_at FROM connection_trust_notifications "
                "WHERE (?=1 OR owner_user_id=? AND tenant_id=? OR connection_id=?) ORDER BY id DESC",
                (int(p.operator), p.user_id, p.tenant_id, p.connection_id))]

    def after_commit(self):
        s = getattr(self.gateway, "agent_surface", None)
        if s is not None:
            for entry in list(s.subscribers):
                try:
                    self.check(entry[2])
                except DomainError:
                    if entry in s.subscribers:
                        s.subscribers.remove(entry)

    def attach_order(self, order):
        p = PRINCIPAL.get()
        if p and p.kind == "agent" and p.connection_id and p.agent_id == order.agent_id:
            order.context = {**(order.context or {}), "connection_authority": p.public()}

    def confirm(self, cid, p, context_id=None):
        with self.db.connect(write=True) as conn:
            c = self.gateway.connections.get(cid, p)
            owner = conn.execute("SELECT owner_user_id FROM main.tenant_agents WHERE agent_id=?", (c.agent_id,)).fetchone()
            if not p.operator and (not owner or owner[0] != p.user_id):
                raise DomainError("OWNER_REQUIRED", "Only the governed agent's owner confirms continuity", 403)
            self.created(conn, cid)
            state = conn.execute("SELECT state FROM connection_trust WHERE connection_id=?", (cid,)).fetchone()[0]
            if c.revoked_at or c.expires_at and c.expires_at <= audit.utcnow() or state == "REAUTH_REQUIRED":
                raise DomainError("UNAUTHENTICATED", "Re-consent is required before confirming continuity", 401)
            contexts = [r[0] for r in conn.execute("SELECT id FROM connection_trust_contexts WHERE connection_id=?", (cid,))]
            if context_id is None and len(contexts) > 1 or context_id is not None and context_id not in contexts:
                raise DomainError("CONTEXT_REQUIRED", "Select an observed runtime context", 422)
            selected = context_id or (contexts[0] if contexts else None)
            conn.execute("UPDATE connection_trust SET state='ACTIVE',confirmed=1,confirmed_context=? WHERE connection_id=?", (selected, cid))
            conn.execute("UPDATE connection_trust_contexts SET last_seen=? WHERE connection_id=? AND id<>?", ((self.clock()-timedelta(seconds=self.grace+1)).isoformat(), cid, selected))
            conn.execute("UPDATE connection_trust_notifications SET resolved_at=? WHERE connection_id=? AND resolved_at IS NULL", (audit.utcnow().isoformat(), cid))
            self.event(conn, cid, "CONNECTION_CONTINUITY_CONFIRMED", p.user_id or p.id)
        return self.health(cid)


class ContinuityConfirmation(BaseModel):
    confirmed: bool
    context_id: str | None = None


router = APIRouter(tags=["connection trust"])


@router.get("/v1/connect/connection-notifications")
@router.get("/v1/connection-notifications", include_in_schema=False)
def connection_notifications(request: Request, p=Depends(resolve_principal)) -> dict:
    return {"notifications": request.app.state.gateway.connection_trust.inbox(p)}


@router.post("/v1/connect/connections/{connection_id}/trust/confirm")
@router.post("/v1/connections/{connection_id}/trust/confirm", include_in_schema=False)
def confirm_continuity(connection_id: str, body: ContinuityConfirmation, request: Request, p=Depends(resolve_principal)) -> dict:
    if not body.confirmed:
        raise DomainError("CONFIRMATION_REQUIRED", "Explicitly confirm this is the same governed agent", 422)
    return request.app.state.gateway.connection_trust.confirm(connection_id, p, body.context_id)


@router.post("/v1/connect/connections/{connection_id}/trust/order-scope")
@router.post("/v1/connections/{connection_id}/trust/order-scope", include_in_schema=False)
def grant_orders(connection_id: str, request: Request, p=Depends(resolve_principal)) -> dict:
    g = request.app.state.gateway
    g.accounts.manage(p)
    g.connections.get(connection_id, p)
    with g.db.connect(write=True) as conn:
        g.connection_trust.created(conn, connection_id)
        conn.execute("UPDATE connection_trust SET order_granted=1 WHERE connection_id=?", (connection_id,))
        row = conn.execute("SELECT scopes FROM connections WHERE connection_id=?", (connection_id,)).fetchone()
        scopes = list(dict.fromkeys([*json.loads(row[0]), "orders"]))
        conn.execute("UPDATE connections SET scopes=? WHERE connection_id=?", (json.dumps(scopes), connection_id))
        audit.append(conn, p.user_id or p.id, "CONNECTION_ORDER_SCOPE_GRANTED", "Tenant administrator granted order scope.", data={"connection_id": connection_id})
    return g.connection_trust.health(connection_id)
