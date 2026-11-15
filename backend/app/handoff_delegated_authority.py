"""Durable, single-holder handoffs and attenuated helper credentials (spec 64)."""
import json
import secrets
import sqlite3
from contextlib import closing
from contextvars import ContextVar
from dataclasses import asdict, replace
from datetime import datetime
from hashlib import sha256
from typing import Literal
from uuid import uuid4

from pydantic import Field, field_validator

from . import audit
from .auth import Principal, check_scope
from .db import DomainError
from .foundation import PRINCIPAL
from .models import Model
from .purchase_intent_integrity import TERMINAL

WORK = ContextVar("delegated_work", default=None)
RESEARCH = ["lookup", "product_detail", "rfq_read"]
OPERATIONS = Literal["lookup", "product_detail", "rfq_read", "quote", "order", "delegate"]
from .recovery import RECOVERY, RECOVERY_V2
for _code in ("INTENT_HANDED_OVER", "HANDOFF_MODE", "HANDOFF_NOT_FOUND", "HANDOFF_USED", "HANDOFF_EXPIRED",
              "HANDOFF_RECIPIENT", "UNKNOWN_PRINCIPAL_IN_CHAIN", "OUTSIDE_DELEGATION", "DELEGATION_REVOKED",
              "DELEGATION_EXPIRED", "DELEGATION_DEPTH", "DELEGATION_NOT_FOUND"):
    RECOVERY[_code] = RECOVERY_V2[_code] = (False, "CONTACT_ADMIN" if _code in {
        "HANDOFF_MODE", "UNKNOWN_PRINCIPAL_IN_CHAIN", "OUTSIDE_DELEGATION", "DELEGATION_REVOKED", "DELEGATION_EXPIRED"} else "STOP")


class ExpiringAgent(Model):
    to_agent_id: str = Field(min_length=1, max_length=200)
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def timezone(cls, value):
        if value.tzinfo is None:
            raise ValueError("expires_at must include a timezone")
        return value


class Handoff(ExpiringAgent):
    mode: Literal["prepare_only", "may_commit"] = "prepare_only"
    confirmation_token: str | None = Field(default=None, repr=False)


class Delegation(ExpiringAgent):
    intent_id: str
    operations: list[OPERATIONS] = Field(default_factory=lambda: list(RESEARCH), min_length=1)
    max_depth: int = Field(default=1, ge=1, le=16)
    confirmation_token: str | None = Field(default=None, repr=False)


class Confirmation(Model):
    confirmation_token: str | None = Field(default=None, repr=False)


def digest(value):
    return sha256(value.encode()).hexdigest()


class HandoffAuthority:
    def __init__(self, gateway):
        self.gateway, self.db = gateway, gateway.db
        self.check_source_connection = gateway.connection_trust.check
        self.calls_path = self.db.path.with_suffix(".authority-calls.sqlite3")
        # Transport reads must remain available while a purchasing writer is
        # waiting for supplier I/O. The append-only call ledger has its own lock.
        with closing(sqlite3.connect(self.calls_path)) as calls:
            calls.execute("PRAGMA journal_mode=WAL")
            calls.execute("""CREATE TABLE IF NOT EXISTS authority_calls(
                id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT, intent_id TEXT, data TEXT NOT NULL)""")
            calls.commit()
        with self.db.connect(write=True) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS authority_handoffs(
                id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, tenant_id TEXT NOT NULL,
                intent_id TEXT NOT NULL, data TEXT NOT NULL, used_at TEXT)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS authority_delegations(
                id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, tenant_id TEXT NOT NULL,
                intent_id TEXT NOT NULL, data TEXT NOT NULL, revoked_at TEXT)""")

    def record_call(self, p, operation, intent_id=None):
        if p.delegation_id:
            with self.db.connect() as conn:
                intent_id = self.valid_grant(conn, p.delegation_id, p.tenant_id)["intent_id"]
        with closing(sqlite3.connect(self.calls_path, timeout=5)) as conn:
            conn.execute("INSERT INTO authority_calls(tenant_id,intent_id,data) VALUES(?,?,?)", (p.tenant_id, intent_id,
                json.dumps({"at": audit.utcnow().isoformat(), "operation": operation,
                    "actor_chain": list(p.actor_chain or (p.id,)), "delegation_id": p.delegation_id})))
            conn.commit()

    def calls(self, intent_id):
        with closing(sqlite3.connect(self.calls_path)) as conn:
            return [json.loads(row[0]) for row in conn.execute("SELECT data FROM authority_calls WHERE intent_id=? ORDER BY id", (intent_id,))]

    def agent(self, conn, agent_id, tid):
        row = conn.execute("SELECT * FROM main.tenant_agents WHERE agent_id=? AND tenant_id=?", (agent_id, tid)).fetchone()
        if not row:
            raise DomainError("UNKNOWN_PRINCIPAL_IN_CHAIN", "Principal is not registered in this tenant", 403)
        self.gateway.governance.check_active(agent_id, conn)
        return row

    def owner(self, conn, obj, p):
        if p and p.operator:
            return True
        row = conn.execute("SELECT owner_user_id FROM main.tenant_agents WHERE agent_id=? AND tenant_id=?",
                           (obj["active_agent"], obj["tenant_id"])).fetchone()
        return bool(p and p.kind == "tenant_user" and p.tenant_id == obj["tenant_id"] and row and row[0] == p.user_id)

    def declared(self, p, names=None):
        if not p or p.kind != "agent":
            if names:
                raise DomainError("UNKNOWN_PRINCIPAL_IN_CHAIN", "Only buyer agents may declare a chain", 403)
            return p
        chain = list(p.actor_chain or (p.id,))
        if names:
            if isinstance(names, str):
                names = [x.strip() for x in names.split(",") if x.strip()]
            if not isinstance(names, (list, tuple)) or not names or any(not isinstance(x, str) or not x for x in names):
                raise DomainError("UNKNOWN_PRINCIPAL_IN_CHAIN", "Expected a list of same-tenant agent IDs", 403)
            # A declaration may add restrictions but cannot erase a credential's ancestry.
            chain = list(dict.fromkeys(x for x in [*names, *chain[:-1]] if x != p.id)) + [p.id]
        if len(chain) > 32:
            raise DomainError("UNKNOWN_PRINCIPAL_IN_CHAIN", "Actor chain is too long", 403)
        with self.db.connect() as conn:
            for name in chain if names or p.actor_chain or p.delegation_id else []:
                self.agent(conn, name, p.tenant_id)
            if p.delegation_id:
                self.valid_grant(conn, p.delegation_id, p.tenant_id)
        return replace(p, actor_chain=tuple(chain))

    def valid_grant(self, conn, id, tid, seen=None):
        seen = set() if seen is None else seen
        if id in seen:
            raise DomainError("OUTSIDE_DELEGATION", "Invalid delegation ancestry", 403)
        seen.add(id)
        row = conn.execute("SELECT * FROM main.authority_delegations WHERE id=? AND tenant_id=?", (id, tid)).fetchone()
        if not row or row["revoked_at"]:
            raise DomainError("DELEGATION_REVOKED", "Delegation was revoked", 403)
        grant = json.loads(row["data"])
        if datetime.fromisoformat(grant["expires_at"]) <= audit.utcnow():
            raise DomainError("DELEGATION_EXPIRED", "Delegation expired", 403)
        for name in grant["actor_chain"]:
            self.agent(conn, name, tid)
            from .buyer import check_suspension
            check_suspension(self.db, name)
        parent = grant.get("parent_id")
        if parent:
            self.valid_grant(conn, parent, tid, seen)
        source = grant.get("source_credential")
        if source:
            source_principal = Principal(**source)
            self.check_source_connection(source_principal, conn)
            if grant.get("source_fingerprint") and self.gateway.settings.keys.fingerprint(source_principal) != grant["source_fingerprint"]:
                raise DomainError("DELEGATION_REVOKED", "Delegating credential was removed or rotated", 403)
        token = self.gateway.mandates.stored(conn, "intent:" + grant["from_agent_id"], grant["intent_id"])
        if token:
            obj = self.gateway.intents.load(conn, grant["intent_id"])
            self.gateway.mandates.verify(conn, token, grant["from_agent_id"], obj["request"]["customer_id"],
                products=[l["product_id"] for l in obj["request"]["lines"] if l["product_id"]], supplier_id=obj["request"]["supplier_id"])
        return grant

    def resolve(self, secret):
        with self.db.connect() as conn:
            row = conn.execute("SELECT id,tenant_id FROM main.authority_delegations WHERE digest=?", (digest(secret),)).fetchone()
            if not row:
                raise DomainError("UNAUTHENTICATED", "Unknown delegation credential", 401)
            grant = self.valid_grant(conn, row["id"], row["tenant_id"])
            obj = self.gateway.intents.load(conn, grant["intent_id"])
        return self.gateway.accounts.bind(Principal(grant["to_agent_id"], tenant_id=row["tenant_id"],
            customer_ids=(obj["request"]["customer_id"],), credential_ref="delegation:" + row["id"],
            delegation_id=row["id"], actor_chain=tuple(grant["actor_chain"])))

    def guard(self, operation, intent_id=None):
        p = PRINCIPAL.get()
        if not p or not p.delegation_id:
            return
        with self.db.connect() as conn:
            grant = self.valid_grant(conn, p.delegation_id, p.tenant_id)
        if intent_id and intent_id != grant["intent_id"] or operation not in grant["operations"]:
            raise DomainError("OUTSIDE_DELEGATION", "Operation is outside the research or owner-approved grant", 403,
                              {"intent_id": grant["intent_id"], "operations": grant["operations"]})

    def holder(self, obj, agent, operation):
        self.guard(operation, obj["id"])
        p = PRINCIPAL.get()
        delegated_holder = p and p.delegation_id and obj["active_agent"] in p.actor_chain
        if obj["active_agent"] != agent and not delegated_holder:
            raise DomainError("INTENT_HANDED_OVER", "This intent has another holder", 409,
                              {"intent_id": obj["id"], "holder": obj["active_agent"], "new_holder": obj["active_agent"]})
        if operation == "order" and obj.get("handoff_mode") == "prepare_only":
            raise DomainError("HANDOFF_MODE", "This handoff permits preparation only", 403)

    def snapshot(self, conn, obj, agent, mandate=None):
        mandate = mandate or self.gateway.mandates.stored(conn, "intent:" + agent, obj["id"])
        policy, _, _ = self.gateway.mandates.authorize(conn, mandate, agent, obj["request"]["customer_id"],
            products=[l["product_id"] for l in obj["request"]["lines"] if l["product_id"]], supplier_id=obj["request"]["supplier_id"])
        fields = {k: v.value for k, v in policy._governance.fields.items()}
        row = policy.agents.get(agent)
        if row:
            from .governance import restrictive
            for key, value in {"per_order_ceiling_eur": row.max_auto_total_eur,
                               "autonomy_level": row.autonomy or policy.autonomy.default}.items():
                fields[key] = restrictive(key, fields[key], value)
            fields["_rolling_limits"] = {agent: row.max_auto_daily_total_eur}
        if mandate:
            from .mandates import decode
            subject = decode(mandate)[1]["vc"]["credentialSubject"]
            if subject.get("categories") is not None:
                from .governance import restrictive
                fields["allowed_categories"] = restrictive("allowed_categories", fields["allowed_categories"], subject["categories"])
        from .governance import restrictive
        for key, value in obj.get("authority_envelope", {}).items():
            if key == "_rolling_limits":
                budgets = fields.setdefault(key, {})
                for name, limit in value.items():
                    budgets[name] = min(budgets.get(name, limit), limit)
            elif key in fields:
                fields[key] = restrictive(key, fields[key], value)
        return fields

    def checkpoint(self, id, agent, mandate=None):
        p = PRINCIPAL.get()
        check_scope(p, "agents" if p and p.kind == "tenant_user" else "orders", "read", "checkpoint")
        self.guard("rfq_read", id)
        with self.db.connect() as conn:
            obj = self.gateway.intents.load(conn, id)
            if not self.owner(conn, obj, PRINCIPAL.get()):
                self.gateway.intents.authority(conn, obj, agent, mandate)
            result = self.gateway.intents.result(conn, obj)
            quote = result.get("quote") or {}
            pending = [obj["order_id"]] if (result.get("order") or {}).get("status") == "PENDING_APPROVAL" else []
            questions = [{"id": r["id"], "type": "handoff_commit_approval"} for r in conn.execute(
                "SELECT id,data FROM authority_handoffs WHERE intent_id=? AND tenant_id=? AND used_at IS NULL", (id, obj["tenant_id"]))
                if not json.loads(r["data"]).get("owner_approved") and datetime.fromisoformat(json.loads(r["data"])["expires_at"]) > audit.utcnow()]
            if obj.get("human_required"):
                questions.append({"type": "intent_review", "message": obj["human_required"]})
            return {"intent_id": id, "version": obj["version"], "state": obj["state"], "holder": obj["active_agent"],
                "context_id": obj.get("context_id", id), "pending_actions": result["next_actions"],
                "artifacts": {"offer_refs": [x for x in [quote.get("need_ref"), quote.get("basket_ref")] if x],
                    "quote": obj.get("quote_id"), "order": obj.get("order_id"), "pending_approvals": pending,
                    "open_questions": questions}, "request": obj["request"]}

    def open(self, p):
        check_scope(p, "orders", "read", "checkpoint")
        self.guard("rfq_read")
        with self.db.connect() as conn:
            if p.delegation_id:
                grant = self.valid_grant(conn, p.delegation_id, p.tenant_id)
                obj = self.gateway.intents.load(conn, grant["intent_id"])
                return {"intents": [self.checkpoint(obj["id"], p.id)] if obj["state"] not in TERMINAL else []}
            rows = conn.execute("SELECT data FROM purchase_intents WHERE tenant_id=? AND json_extract(data,'$.active_agent')=?",
                                (p.tenant_id, p.id)).fetchall()
        return {"intents": [self.checkpoint(o["id"], p.id) for r in rows if (o := json.loads(r[0]))["state"] not in TERMINAL]}

    def create_handoff(self, id, body, p, mandate=None):
        check_scope(p, "agents" if p.kind == "tenant_user" else "orders", "write", "handoff")
        self.guard("handoff", id)
        self.gateway.limiter.consume(p.id, "mutation")
        with self.db.connect(write=True) as conn:
            obj = self.gateway.intents.load(conn, id)
            owner = self.owner(conn, obj, p)
            if not owner:
                self.holder(obj, p.id, "handoff")
                self.gateway.intents.authority(conn, obj, p.id, mandate)
            self.agent(conn, body.to_agent_id, obj["tenant_id"])
            if obj["state"] in TERMINAL or body.to_agent_id == obj["active_agent"]:
                raise DomainError("INVALID_TRANSITION", "Handoff needs open work and a different recipient", 409)
            if body.expires_at <= audit.utcnow():
                raise DomainError("HANDOFF_EXPIRED", "Handoff expiry must be in the future", 409)
            code, gid = secrets.token_urlsafe(32), uuid4().hex
            grant = {"id": gid, "intent_id": id, "from_agent_id": obj["active_agent"], "to_agent_id": body.to_agent_id,
                "mode": body.mode, "expires_at": body.expires_at.isoformat(), "version": obj["version"],
                "owner_approved": body.mode == "prepare_only" or owner,
                "envelope": self.snapshot(conn, obj, obj["active_agent"], mandate),
                "actor_chain": list(obj.get("actor_chain") or (obj["active_agent"],))}
            conn.execute("INSERT INTO authority_handoffs(id,digest,tenant_id,intent_id,data) VALUES(?,?,?,?,?)",
                         (gid, digest(code), obj["tenant_id"], id, json.dumps(grant)))
            self.gateway.intents.log(conn, obj, "HANDOFF_GRANTED", p.id, grant=self.public(grant))
            return {"handoff_id": gid, "code": code, "owner_approval_required": not grant["owner_approved"],
                    "approval_url": f"/v1/handoffs/{gid}/approve", "expires_at": grant["expires_at"]}

    @staticmethod
    def public(grant):
        result = {k: v for k, v in grant.items() if k not in {"source_credential", "source_fingerprint", "envelope", "snapshots"}}
        if grant.get("envelope"):
            result["effective_policy"] = {k: v for k, v in grant["envelope"].items() if not k.startswith("_")}
            result["rolling_limits"] = grant["envelope"].get("_rolling_limits", {})
        return result

    def approve_handoff(self, id, p):
        check_scope(p, "agents" if p.kind == "tenant_user" else "orders", "write", "handoff")
        with self.db.connect(write=True) as conn:
            row = conn.execute("SELECT * FROM authority_handoffs WHERE id=?", (id,)).fetchone()
            if not row:
                raise DomainError("HANDOFF_NOT_FOUND", "Handoff was not found", 404)
            grant = json.loads(row["data"])
            obj = self.gateway.intents.load(conn, row["intent_id"])
            if not self.owner(conn, obj, p):
                raise DomainError("OWNER_REQUIRED", "Only the intent holder's human owner may allow commitment", 403)
            if row["used_at"] or datetime.fromisoformat(grant["expires_at"]) <= audit.utcnow():
                raise DomainError("HANDOFF_EXPIRED", "Handoff is used or expired", 409)
            self.gateway.intents.check_version(obj, grant["version"])
            grant["owner_approved"] = True
            grant["approved_by"] = p.user_id or p.id
            conn.execute("UPDATE authority_handoffs SET data=? WHERE id=?", (json.dumps(grant), id))
            self.gateway.intents.log(conn, obj, "HANDOFF_APPROVED", p.id, grant=self.public(grant))
            return {"handoff_id": id, "owner_approved": True}

    def accept(self, code, p, mandate=None):
        check_scope(p, "orders", "write", "handoff")
        self.guard("handoff")
        self.gateway.limiter.consume(p.id, "mutation")
        if p.kind != "agent":
            raise DomainError("BUYER_AGENT_REQUIRED", "Recipient must use its own agent credential", 403)
        with self.db.connect(write=True) as conn:
            row = conn.execute("SELECT * FROM authority_handoffs WHERE digest=? AND tenant_id=?", (digest(code), p.tenant_id)).fetchone()
            if not row:
                raise DomainError("HANDOFF_NOT_FOUND", "Handoff was not found", 404)
            grant = json.loads(row["data"])
            if row["used_at"]:
                raise DomainError("HANDOFF_USED", "Handoff code is single-use", 409)
            if datetime.fromisoformat(grant["expires_at"]) <= audit.utcnow():
                raise DomainError("HANDOFF_EXPIRED", "Handoff expired", 409)
            if p.id != grant["to_agent_id"]:
                raise DomainError("HANDOFF_RECIPIENT", "Handoff is bound to another recipient", 403)
            if not grant["owner_approved"]:
                raise DomainError("OWNER_REQUIRED", "Commit authority awaits the owner's confirmed decision", 403)
            obj = self.gateway.intents.load(conn, row["intent_id"])
            self.gateway.intents.check_version(obj, grant["version"])
            if obj["active_agent"] != grant["from_agent_id"]:
                raise DomainError("INTENT_HANDED_OVER", "Holder changed before acceptance", 409, {"holder": obj["active_agent"]})
            work = WORK.set({**obj, "authority_envelope": grant["envelope"]})
            try:
                policy = self.gateway.intents.authority(conn, obj, p.id, mandate, limits=grant["mode"] == "may_commit")
                products = [self.gateway.suppliers.get(obj["request"]["supplier_id"]).catalog.get(l["product_id"])
                            for l in obj["request"]["lines"] + obj.get("resolved_lines", []) if l["product_id"]]
                self.gateway.governance.check_operation(conn, p.id, "quote", obj["request"]["customer_id"], products=products,
                                                        effective=policy._governance)
            finally:
                WORK.reset(work)
            obj.update(active_agent=p.id, handoff_mode=grant["mode"], handoff_id=grant["id"],
                       authority_envelope=grant["envelope"], actor_chain=[*grant["actor_chain"], p.id], holder_credential=asdict(p))
            self.gateway.intents.bind_authority(conn, obj, p.id, mandate)
            self.gateway.intents.save(conn, obj, grant["version"])
            conn.execute("UPDATE authority_handoffs SET used_at=? WHERE id=? AND used_at IS NULL", (audit.utcnow().isoformat(), row["id"]))
            self.transfer_tasks(conn, obj)
            self.gateway.intents.log(conn, obj, "INTENT_HANDOVER", p.id, previous_agent=grant["from_agent_id"],
                                     actor_chain=obj["actor_chain"], grant=self.public(grant))
            return self.checkpoint(obj["id"], p.id, mandate)

    def transfer_tasks(self, conn, obj):
        from .a2a import proto, t, metadata
        from google.protobuf.json_format import MessageToDict
        for row in conn.execute("SELECT id,data FROM a2a_tasks WHERE json_extract(data,'$.metadata.intent_id')=?", (obj["id"],)).fetchall():
            task = proto(t.Task, json.loads(row["data"]))
            task.context_id = obj.get("context_id", task.context_id)
            task.metadata.update({"owner": obj["active_agent"], "owner_scope": "agent:" + obj["active_agent"],
                                  "actor_chain": obj.get("actor_chain", []), "intent_version": obj["version"]})
            conn.execute("UPDATE a2a_tasks SET owner=?,data=? WHERE id=?", (metadata(task)["owner_scope"], json.dumps(MessageToDict(task)), row["id"]))

    def delegate(self, body, p, mandate=None):
        check_scope(p, "agents" if p.kind == "tenant_user" else "orders", "write", "delegate")
        self.gateway.limiter.consume(p.id, "mutation")
        with self.db.connect(write=True) as conn:
            obj = self.gateway.intents.load(conn, body.intent_id)
            parent = self.valid_grant(conn, p.delegation_id, p.tenant_id) if p.delegation_id else None
            owner = self.owner(conn, obj, p)
            if parent:
                if parent["remaining_depth"] <= 0:
                    raise DomainError("DELEGATION_DEPTH", "Delegation depth exhausted", 403)
                # Subdelegation is depth-gated independently of the research operations.
                if body.intent_id != parent["intent_id"] or not set(body.operations) <= set(parent["operations"]):
                    raise DomainError("OUTSIDE_DELEGATION", "A child cannot widen its parent's scope", 403)
            elif not owner:
                self.holder(obj, p.id, "delegate")
                self.gateway.intents.authority(conn, obj, p.id, mandate)
            self.agent(conn, body.to_agent_id, obj["tenant_id"])
            if "order" in body.operations and not owner:
                raise DomainError("OWNER_REQUIRED", "Order delegation requires explicit human owner approval", 403)
            if body.expires_at <= audit.utcnow() or parent and body.expires_at > datetime.fromisoformat(parent["expires_at"]):
                raise DomainError("DELEGATION_EXPIRED", "A child must expire no later than its parent", 403)
            if parent and body.max_depth > parent["remaining_depth"]:
                raise DomainError("DELEGATION_DEPTH", "Child depth exceeds its parent", 403)
            chain = list(p.actor_chain or (obj["active_agent"] if owner else p.id,))
            if body.to_agent_id in chain:
                raise DomainError("OUTSIDE_DELEGATION", "Delegation cannot form a cycle", 403)
            source_agent = obj["active_agent"] if owner else p.id
            envelope = self.snapshot(conn, obj, source_agent, mandate)
            if mandate:
                self.gateway.intents.bind_authority(conn, obj, source_agent, mandate)
            gid, secret = uuid4().hex, "dg64_" + secrets.token_urlsafe(32)
            operations = sorted(set(body.operations))
            grant = {"id": gid, "intent_id": obj["id"], "from_agent_id": source_agent, "to_agent_id": body.to_agent_id,
                "operations": operations, "expires_at": body.expires_at.isoformat(), "parent_id": p.delegation_id,
                "remaining_depth": body.max_depth - 1, "actor_chain": [*chain, body.to_agent_id],
                "envelope": envelope, "source_credential": asdict(p) if p.kind == "agent" else None,
                "source_fingerprint": self.gateway.settings.keys.fingerprint(p) if p.kind == "agent" and not p.connection_id and self.gateway.settings.keys else None,
                "owner_approved_by": (p.user_id or p.id) if "order" in operations and owner else None}
            conn.execute("INSERT INTO authority_delegations(id,digest,tenant_id,intent_id,data) VALUES(?,?,?,?,?)",
                         (gid, digest(secret), obj["tenant_id"], obj["id"], json.dumps(grant)))
            self.gateway.intents.log(conn, obj, "DELEGATION_GRANTED", p.id, actor_chain=grant["actor_chain"], grant=self.public(grant))
            return {"delegation_id": gid, "credential": secret, **self.public(grant)}

    def revoke(self, id, p):
        check_scope(p, "agents" if p.kind == "tenant_user" else "orders", "write", "delegate")
        self.gateway.limiter.consume(p.id, "mutation")
        with self.db.connect(write=True) as conn:
            row = conn.execute("SELECT * FROM authority_delegations WHERE id=?", (id,)).fetchone()
            if not row or not p.operator and row["tenant_id"] != p.tenant_id:
                raise DomainError("DELEGATION_NOT_FOUND", "Delegation was not found", 404)
            grant = json.loads(row["data"])
            obj = self.gateway.intents.load(conn, row["intent_id"])
            if p.id != grant["from_agent_id"] and not self.owner(conn, obj, p):
                raise DomainError("OWNER_REQUIRED", "Only the delegator or owner may revoke", 403)
            conn.execute("UPDATE authority_delegations SET revoked_at=? WHERE id=?", (audit.utcnow().isoformat(), id))
            self.gateway.intents.log(conn, obj, "DELEGATION_REVOKED", p.id, grant=self.public(grant))
            return {"delegation_id": id, "revoked": True}
