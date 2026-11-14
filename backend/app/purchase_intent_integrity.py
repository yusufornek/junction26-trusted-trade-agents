"""One tenant buying decision across transports, agents and supplier failures.

Intent versions are CAS revisions (including amendments). Commercial evidence
retains the revision at which it was issued. Supplier sequence numbers are local
to an intent's stable submission reference and start at one.
"""
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import wraps
from hashlib import sha256
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from . import audit
from .canonical import canonical_json
from .db import DomainError
from .foundation import PRINCIPAL
from .models import Model, QuoteRequest, RuleHit, Execution


class IntentLine(Model):
    product_id: str | None = Field(default=None, min_length=1, max_length=200)
    functional_need: str | None = Field(default=None, min_length=1, max_length=1000)
    quantity_sales_units: float = Field(gt=0, le=1_000_000, strict=True)
    essential: bool = False

    @model_validator(mode="after")
    def identity(self):
        if not self.product_id and not self.functional_need:
            raise ValueError("A product or functional need is required")
        return self


class FulfilmentPolicy(Model):
    mode: Literal["all_or_nothing", "allow_partial"] = "all_or_nothing"
    minimum_share: float = Field(default=1, ge=0, le=1)


class ConsumptionWindow(Model):
    start: date
    end: date

    @model_validator(mode="after")
    def ordered(self):
        if self.end < self.start:
            raise ValueError("Consumption window end must follow start")
        return self


class IntentRequest(Model):
    client_intent_id: str = Field(min_length=1, max_length=200)
    customer_id: str = Field(min_length=1, max_length=200)
    supplier_id: str = "valio"
    lines: list[IntentLine] = Field(min_length=1, max_length=10)
    deliver_by: date
    constraints: dict = Field(default_factory=dict)
    delivery_point: str = Field(default="default", min_length=1, max_length=200)
    consumption_window: ConsumptionWindow | None = None
    fulfilment_policy: FulfilmentPolicy = Field(default_factory=FulfilmentPolicy)
    supersedes: list[str] = Field(default_factory=list, max_length=100)
    additional: bool = False

    @model_validator(mode="after")
    def normalize(self):
        if self.consumption_window is None:
            self.consumption_window = ConsumptionWindow(start=self.deliver_by, end=self.deliver_by)
        keys = [line.product_id or line.functional_need.casefold().strip() for line in self.lines]
        if len(keys) != len(set(keys)):
            raise ValueError("Combine repeated demand lines")
        if self.additional and self.supersedes:
            raise ValueError("Choose supersedes or additional")
        return self


class Amendment(Model):
    expected_version: int = Field(ge=1)
    request: IntentRequest


class Revision(Model):
    expected_version: int = Field(ge=1)


class Transition(Revision):
    state: Literal["CANCELLED", "CONFIRMED", "CLOSED", "EXPIRED"]
    confirmed_lines: dict[str, float] | None = None


class SupplierEvent(Model):
    submission_reference: str | None = None
    event_id: str = Field(min_length=1, max_length=200)
    sequence: int = Field(ge=1)
    kind: Literal["accepted", "cancelled", "rejected"]
    confirmed_lines: dict[str, float] | None = None


class Faults(Model):
    drop_order_response: bool = False
    confirmation_delay_seconds: int = Field(default=0, ge=0, le=86400)
    confirmed_lines: dict[str, float] | None = None


class IntentReview(Revision):
    approver: str = Field(min_length=1, max_length=200)
    quote_id: str | None = None
    confirmation_token: str | None = None


TERMINAL = {"CLOSED", "CANCELLED", "EXPIRED"}
EDGES = {
    "DRAFT": {"QUOTED", "CANCELLED", "EXPIRED", "PENDING_APPROVAL"},
    "QUOTED": {"DRAFT", "PENDING_APPROVAL", "ORDERED", "CANCELLED", "EXPIRED"},
    "PENDING_APPROVAL": {"DRAFT", "QUOTED", "ORDERED", "CANCELLED", "EXPIRED"},
    "ORDERED": {"CONFIRMED", "PARTIALLY_CONFIRMED", "PENDING_APPROVAL", "SUBMISSION_UNKNOWN", "CANCEL_REQUESTED", "CANCELLED", "EXPIRED"},
    "SUBMISSION_UNKNOWN": {"ORDERED", "CONFIRMED", "PARTIALLY_CONFIRMED", "PENDING_APPROVAL", "CANCEL_REQUESTED", "CANCELLED"},
    "CONFIRMED": {"CLOSED", "CANCEL_REQUESTED", "CANCELLED"},
    "PARTIALLY_CONFIRMED": {"CLOSED", "CANCEL_REQUESTED", "CANCELLED", "PENDING_APPROVAL"},
    "CANCEL_REQUESTED": {"CANCELLED", "CANCEL_DISPUTED"},
    "CANCEL_DISPUTED": {"CANCELLED", "CLOSED"},
    # Only the human-approved cancel-and-replace amendment can leave CANCELLED.
    "CANCELLED": {"QUOTED"},
}


def payload(request):
    data = request.model_dump(mode="json")
    data["supplier_id"] = request.supplier_id
    data.pop("client_intent_id")
    data["lines"] = sorted(data["lines"], key=lambda l: l["product_id"] or l["functional_need"])
    data["supersedes"] = sorted(data["supersedes"])
    return data


def difference(before, after, prefix=""):
    """Field-level evidence, including all changed quantities and nested constraints."""
    if isinstance(before, dict) and isinstance(after, dict):
        return [item for key in sorted(before.keys() | after.keys())
                for item in difference(before.get(key), after.get(key), f"{prefix}.{key}".strip("."))]
    if isinstance(before, list) and isinstance(after, list):
        return [item for i in range(max(len(before), len(after)))
                for item in difference(before[i] if i < len(before) else None,
                                       after[i] if i < len(after) else None, f"{prefix}[{i}]")]
    return [] if before == after else [{"field": prefix, "before": before, "after": after}]


class PurchaseIntents:
    def __init__(self, gateway):
        self.gateway, self.db = gateway, gateway.db
        with self.db.connect(write=True) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS purchase_intents (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, client_intent_id TEXT NOT NULL,
                version INTEGER NOT NULL, data TEXT NOT NULL, UNIQUE(tenant_id,client_intent_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS purchase_intent_events (
                tenant_id TEXT NOT NULL, supplier_id TEXT NOT NULL, intent_id TEXT NOT NULL, event_id TEXT NOT NULL,
                submission_reference TEXT NOT NULL, sequence INTEGER NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(tenant_id,supplier_id,event_id), UNIQUE(intent_id,submission_reference,sequence))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS purchase_intent_commands (
                tenant_id TEXT NOT NULL, agent_id TEXT NOT NULL, key TEXT NOT NULL, intent_id TEXT NOT NULL,
                PRIMARY KEY(tenant_id,agent_id,key))""")
            # Older startup recovery may have queued these orders in both stores.
            # Only the intent dispatcher understands partials and supplier sequences.
            conn.execute("DELETE FROM supplier_execution_outbox WHERE order_id IN "
                         "(SELECT id FROM orders WHERE json_extract(data,'$.intent_id') IN "
                         "(SELECT id FROM purchase_intents))")

    def tenant(self, conn, customer=None):
        principal = PRINCIPAL.get()
        if principal and principal.tenant_id and not principal.operator:
            return principal.tenant_id
        if customer:
            row = conn.execute("SELECT tenant_id FROM tenant_customers WHERE customer_id=?", (customer,)).fetchone()
            if row:
                return row[0]
        raise DomainError("CUSTOMER_REQUIRED", "A tenant-bound customer is required", 422)

    def load(self, conn, id):
        row = conn.execute("SELECT data,tenant_id FROM purchase_intents WHERE id=?", (id,)).fetchone()
        p = PRINCIPAL.get()
        if not row or (p and not p.operator and p.tenant_id != row[1]):
            raise DomainError("INTENT_NOT_FOUND", "Intent was not found", 404)
        return json.loads(row[0])

    def authority(self, conn, obj, agent, mandate=None, *, limits=False):
        request = obj["request"]
        mandate = mandate or self.gateway.mandates.stored(conn, "intent:" + agent, obj["id"])
        policy, record, _ = self.gateway.mandates.authorize(conn, mandate, agent, request["customer_id"],
            products={l["product_id"] for l in request["lines"] + obj.get("resolved_lines", []) if l["product_id"]}, supplier_id=request["supplier_id"])
        # A handover cannot silently inherit another agent's spending authority.
        if limits and obj.get("quote_id"):
            quote = self.gateway.orders._quote(conn, obj["quote_id"])
            original = obj.get("order_id") and self.gateway.orders._order(conn, obj["order_id"])
            result = self.evaluate(conn, quote, agent, policy, original)
            reviewed = {(h.rule, h.detail) for h in original.policy.hits} if original and original.approver else set()
            approved = bool(original and original.approver and {(h.rule, h.detail) for h in result.hits} <= reviewed)
            if result.outcome == "REJECTED" or (result.outcome != "CONFIRMED" and not approved and not (original and original.status == "PENDING_APPROVAL")):
                raise DomainError("MANDATE_INVALID", "Handover exceeds the receiving agent's authority", 403,
                                  {"check": "limits", "rule_hits": [h.model_dump() for h in result.hits]})
        return policy

    def evaluate(self, conn, quote, agent, policy, order=None):
        """Rechecking a durable commitment must not count that purchase twice."""
        spent = self.gateway.policy.autonomous_total(conn, agent)
        if (order and order.agent_id == agent and order.approver is None and
                order.status in {"CONFIRMED", "PENDING_EXECUTION"} and
                audit.utcnow() - timedelta(hours=24) <= order.created_at <= audit.utcnow() and
                conn.execute("SELECT 1 FROM orders WHERE id=? AND rowid>COALESCE("
                    "(SELECT CAST(value AS INTEGER) FROM workspace WHERE key='budget_start'),0)", (order.id,)).fetchone()):
            spent = max(Decimal(0), spent - Decimal(str(order.total)))
        evidence = order or quote
        return self.gateway.policy.evaluate(evidence.total, evidence.lines, self.gateway.orders._violations(conn, quote),
            agent_id=agent, customer_id=quote.customer_id, policy=policy, warnings=evidence.warnings,
            autonomous_total=spent)

    def bind_authority(self, conn, obj, agent, mandate):
        if mandate is not None:
            digest = self.gateway.mandates.used(conn, mandate)
            conn.execute("INSERT INTO object_mandates(kind,object_id,digest) VALUES(?,?,?) ON CONFLICT(kind,object_id) DO UPDATE SET digest=excluded.digest",
                         ("intent:" + agent, obj["id"], digest))

    def check_version(self, obj, expected):
        if obj["version"] != expected:
            raise DomainError("INTENT_VERSION_CONFLICT", "Another writer advanced the intent", 409,
                              {"winning_state": obj["state"], "version": obj["version"], "intent_id": obj["id"]})

    def save(self, conn, obj, expected, state=None):
        self.check_version(obj, expected)
        if state is not None and state != obj["state"]:
            if state not in EDGES.get(obj["state"], set()):
                raise DomainError("INVALID_TRANSITION", f"Cannot move {obj['state']} to {state}", 409)
            obj["state"] = state
        if obj["state"] in TERMINAL and not obj.get("pending_amendment"):
            obj["human_required"] = None
        obj["version"] = expected + 1
        cursor = conn.execute("UPDATE purchase_intents SET version=?,data=? WHERE id=? AND tenant_id=? AND version=?",
            (obj["version"], json.dumps(obj), obj["id"], obj["tenant_id"], expected))
        if cursor.rowcount != 1:
            self.check_version(self.load(conn, obj["id"]), expected)
        if obj.get("order_id"):
            row = conn.execute("SELECT data FROM orders WHERE id=?", (obj["order_id"],)).fetchone()
            if row:
                order = json.loads(row[0])
                order["intent_state"] = obj["state"]
                order["delivery_notice"] = "delivery may still arrive" if obj["state"] == "CANCEL_DISPUTED" else None
                conn.execute("UPDATE orders SET data=? WHERE id=?", (json.dumps(order), obj["order_id"]))

    @staticmethod
    def fingerprints(request):
        return [sha256(canonical_json({"customer": request["customer_id"], "delivery_point": request["delivery_point"],
            "consumption_window": request["consumption_window"], "demand": l["product_id"] or l["functional_need"].casefold().strip()})).hexdigest()
            for l in request["lines"]]

    def log(self, conn, obj, action, agent, **data):
        from .foundation import correlation_id
        aid = audit.append(conn, f"agent:{agent}", action, action.replace("_", " ").capitalize(),
            order_id=obj.get("order_id"), quote_id=obj.get("quote_id"),
            data={"intent_id": obj["id"], "intent_version": obj["version"], **data})
        if action not in {"INTENT_AMENDED", "INTENT_HANDOVER", "INTENT_PARTIALLY_CONFIRMED"}:
            return
        audit_id = conn.execute("SELECT MAX(id) FROM audit").fetchone()[0]
        event_id, now = str(uuid4()), audit.utcnow().isoformat()
        revision = conn.execute("SELECT COALESCE(MAX(aggregate_revision),0)+1 FROM events WHERE aggregate_type='intent' AND aggregate_id=?", (obj["id"],)).fetchone()[0]
        conn.execute("INSERT INTO events(id,schema_version,type,customer_id,environment,aggregate_type,aggregate_id,aggregate_revision,actor,ts,correlation_id,causation_id,payload,audit_id,tenant_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, 1, action, obj["request"]["customer_id"], self.gateway.settings.environment, "intent", obj["id"], revision,
             f"agent:{agent}", now, correlation_id(), None, json.dumps({"intent_id": obj["id"], "state": obj["state"], **data}), audit_id, obj["tenant_id"]))
        conn.execute("INSERT INTO outbox(event_id,consumer,status,attempts,next_attempt_at,tenant_id) SELECT ?,name,'pending',0,?,? FROM event_consumers WHERE enabled=1", (event_id, now, obj["tenant_id"]))

    @staticmethod
    def overlaps(a, b):
        if a["customer_id"] != b["customer_id"] or a["delivery_point"] != b["delivery_point"]:
            return False
        aw, bw = a["consumption_window"], b["consumption_window"]
        return aw["start"] <= bw["end"] and bw["start"] <= aw["end"]

    def duplicates(self, conn, request, tid, exclude=None):
        matches = []
        for row in conn.execute("SELECT data FROM purchase_intents WHERE tenant_id=?", (tid,)):
            old = json.loads(row[0])
            if old["id"] == exclude or old["state"] in TERMINAL or not self.overlaps(request, old["request"]):
                continue
            for line in request["lines"]:
                # A pending amendment and the unchanged commercial order both
                # protect their demand until the human approves the replacement.
                others = old["request"]["lines"] + old.get("resolved_lines", [])
                if any((line["product_id"] and line["product_id"] == other["product_id"]) or
                        (line["functional_need"] and other["functional_need"] and
                         line["functional_need"].casefold().strip() == other["functional_need"].casefold().strip())
                       for other in others):
                    matches.append({"intent_id": old["id"], "order_id": old.get("order_id"), "line": line, "state": old["state"]})
        # Legacy orders also represent demand; they need not have an intent record.
        for row in conn.execute("SELECT data FROM orders WHERE tenant_id=? AND status NOT IN ('CANCELLED','EXPIRED','REJECTED')", (tid,)):
            order = json.loads(row[0])
            if order.get("intent_id"):
                continue
            quote = self.gateway.orders._quote(conn, order["quote_id"])
            context = order.get("context") or {}
            old = {"customer_id": order.get("customer_id") or quote.customer_id,
                   "delivery_point": context.get("delivery_point", "default"),
                   "consumption_window": context.get("consumption_window") or {"start": quote.deliver_by.isoformat(), "end": quote.deliver_by.isoformat()}}
            if self.overlaps(request, old):
                for line in request["lines"]:
                    if any(line["product_id"] == l["product_id"] for l in order["lines"]):
                        matches.append({"order_id": order["id"], "line": line, "state": order["status"]})
        return matches

    def resolve_duplicates(self, conn, request, tid, exclude=None):
        overlaps = self.duplicates(conn, request, tid, exclude)
        covered = set(request["supersedes"])
        remaining = [m for m in overlaps if (m.get("intent_id") or m.get("order_id")) not in covered]
        if remaining and not request["additional"]:
            raise DomainError("POSSIBLE_DUPLICATE_DEMAND", "Demand overlaps an open purchase; declare supersedes or additional", 409, {"overlaps": remaining})
        for ref in covered:
            if not conn.execute("SELECT 1 FROM purchase_intents WHERE id=? AND tenant_id=?", (ref, tid)).fetchone():
                old_order = self.gateway.orders._order(conn, ref)
                if old_order.status not in {"CANCELLED", "REJECTED", "EXPIRED"}:
                    raise DomainError("INTENT_AMENDMENT_REQUIRED", "Cancel and acknowledge the superseded order first", 409,
                                      {"order_id": ref, "state": old_order.status})
                continue
            old = self.load(conn, ref)
            if old.get("order_id") and old["state"] not in TERMINAL:
                # A supplier commitment cannot disappear just because a new agent retries.
                raise DomainError("INTENT_AMENDMENT_REQUIRED", "Cancel and acknowledge the existing commitment before superseding it", 409,
                                  {"intent_id": ref, "state": old["state"]})
            if old["state"] not in TERMINAL:
                self.save(conn, old, old["version"], "CANCELLED")
        return request["additional"]

    def create(self, request, agent, mandate=None):
        with self.db.connect(write=True) as conn:
            tid = self.tenant(conn, request.customer_id)
            data = payload(request)
            digest = sha256(canonical_json(data)).hexdigest()
            row = conn.execute("SELECT data FROM purchase_intents WHERE tenant_id=? AND client_intent_id=?", (tid, request.client_intent_id)).fetchone()
            if row:
                obj = json.loads(row[0])
                self.authority(conn, obj, agent, mandate)
                if obj["digest"] != digest:
                    raise DomainError("INTENT_AMENDMENT_REQUIRED", "Use an amendment for changed buying intent", 409,
                                      {"intent_id": obj["id"], "version": obj["version"], "difference": difference(obj["request"], data)})
                return self.result(conn, obj)
            obj = {"id": str(uuid4()), "tenant_id": tid, "client_intent_id": request.client_intent_id,
                   "request": data, "digest": digest, "version": 1, "state": "DRAFT", "active_agent": agent,
                   "quote_id": None, "order_id": None, "remainder": [], "committed_lines": {}, "supplier_sequence": 0,
                   "submission_reference": None, "submission_attempts": 0, "faults": {}, "amendments": []}
            obj["demand_fingerprints"] = self.fingerprints(data)
            self.authority(conn, obj, agent, mandate)
            obj["additional_approval"] = self.resolve_duplicates(conn, data, tid)
            conn.execute("INSERT INTO purchase_intents(id,tenant_id,client_intent_id,version,data) VALUES(?,?,?,?,?)",
                         (obj["id"], tid, request.client_intent_id, 1, json.dumps(obj)))
            self.bind_authority(conn, obj, agent, mandate)
            self.log(conn, obj, "INTENT_CREATED", agent)
            return self.result(conn, obj)

    def result(self, conn, obj):
        out = dict(obj)
        out["quote"] = self.gateway.orders._quote(conn, obj["quote_id"]).model_dump(mode="json") if obj.get("quote_id") else None
        out["order"] = self.gateway.orders._order(conn, obj["order_id"]).model_dump(mode="json") if obj.get("order_id") else None
        state = obj["state"]
        action = ("WAIT_FOR_APPROVAL" if state == "PENDING_APPROVAL" or obj.get("pending_amendment") else
                  "RETRY_SAME_COMMAND" if state in {"ORDERED", "SUBMISSION_UNKNOWN", "CANCEL_REQUESTED"} else
                  "CONTACT_ADMIN" if state == "CANCEL_DISPUTED" else "LOOKUP_REQUIRED" if state == "DRAFT" else
                  "STOP" if state in TERMINAL else None)
        if obj.get("human_required"):
            action = "WAIT_FOR_APPROVAL"
        out["next_action"] = action
        out["next_actions"] = ([{"action": action, "intent_id": obj["id"], "version": obj["version"]}] if action else [])
        if state == "QUOTED":
            out["next_actions"].append({"action": "PLACE_ORDER" if out["quote"]["agent_id"] == obj["active_agent"] else "LOOKUP_REQUIRED", "quote_id": obj["quote_id"]})
        if obj.get("remainder") and state not in TERMINAL:
            out["next_actions"].append({"action": "LOOKUP_REQUIRED", "remainder": obj["remainder"], "intent_id": obj["id"]})
        out["kitchen_message"] = "delivery may still arrive" if state == "CANCEL_DISPUTED" else None
        return out

    def get(self, id, agent, mandate=None):
        self.gateway.orders.expire()
        with self.db.connect(write=True) as conn:
            obj = self.load(conn, id)
            self.authority(conn, obj, agent, mandate)
            if obj["state"] == "QUOTED" and self.gateway.orders._quote(conn, obj["quote_id"]).status == "EXPIRED":
                self.save(conn, obj, obj["version"], "EXPIRED")
            return self.result(conn, obj)

    def amend(self, id, amendment, agent, mandate=None):
        with self.db.connect(write=True) as conn:
            obj = self.load(conn, id)
            self.authority(conn, obj, agent, mandate)
            self.check_version(obj, amendment.expected_version)
            if obj["state"] in TERMINAL or amendment.request.customer_id != obj["request"]["customer_id"] or amendment.request.client_intent_id != obj["client_intent_id"]:
                raise DomainError("INVALID_TRANSITION", "Amend an open intent for the same customer and client id", 409)
            new = payload(amendment.request)
            self.authority(conn, {**obj, "request": new}, agent, mandate)
            obj["additional_approval"] = self.resolve_duplicates(conn, new, obj["tenant_id"], id)
            delta = difference(obj["request"], new)
            obj["amendments"].append({"version": obj["version"], "request": obj["request"], "difference": delta})
            if obj.get("order_id"):
                obj["pending_amendment"] = new
                obj["human_required"] = "Order changes require approval and a fresh quote; original commitment remains valid"
            else:
                obj["quote_id"] = None
            obj.update(request=new, digest=sha256(canonical_json(new)).hexdigest(), demand_fingerprints=self.fingerprints(new))
            self.save(conn, obj, amendment.expected_version, None if obj.get("order_id") else "DRAFT")
            self.log(conn, obj, "INTENT_AMENDED", agent, difference=delta)
            return self.result(conn, obj)

    def resume(self, id, expected, agent, mandate=None):
        with self.db.connect(write=True) as conn:
            obj = self.load(conn, id)
            self.authority(conn, obj, agent, mandate, limits=True)
            self.check_version(obj, expected)
            previous = obj["active_agent"]
            obj["active_agent"] = agent
            self.bind_authority(conn, obj, agent, mandate)
            self.save(conn, obj, expected)
            self.log(conn, obj, "INTENT_HANDOVER", agent, previous_agent=previous)
            return self.result(conn, obj)

    def quote_check(self, conn, request, agent, mandate):
        obj = self.load(conn, request.intent_id)
        self.authority(conn, obj, agent, mandate)
        self.check_version(obj, request.intent_version)
        if obj.get("order_id") or obj["state"] not in {"DRAFT", "QUOTED"}:
            raise DomainError("INVALID_TRANSITION", "Use the existing order or an approved amendment", 409)
        if request.customer_id != obj["request"]["customer_id"] or request.deliver_by.isoformat() != obj["request"]["deliver_by"]:
            raise DomainError("NEED_MISMATCH", "Quote must match the purchase intent", 422)
        # Do not permit an intent to bypass the stored request's hard filters.
        need = self.gateway.orders.lookup_need(conn, request.need_ref).need
        if need.supplier_id != obj["request"]["supplier_id"]:
            raise DomainError("NEED_MISMATCH", "Intent and quote suppliers differ", 422)
        for key, value in obj["request"]["constraints"].items():
            actual = getattr(need, key, None)
            if isinstance(value, list) and actual is not None:
                valid = set(value) <= set(actual)
            else:
                valid = actual == value
            if not valid:
                raise DomainError("NEED_MISMATCH", f"Stored lookup does not enforce intent constraint {key}", 422)
        return obj

    def resolve_quote(self, conn, obj, quote):
        """Validate replacement evidence before changing the existing commitment."""
        if (quote.customer_id != obj["request"]["customer_id"] or quote.deliver_by.isoformat() != obj["request"]["deliver_by"] or
                quote.supplier_id != obj["request"]["supplier_id"]):
            raise DomainError("NEED_MISMATCH", "Quote customer, delivery and supplier must match the intent", 422)
        need = self.gateway.orders.lookup_need(conn, quote.need_ref).need
        for key, value in obj["request"]["constraints"].items():
            actual = getattr(need, key, None)
            if not ((isinstance(value, list) and actual is not None and set(value) <= set(actual)) or actual == value):
                raise DomainError("NEED_MISMATCH", f"Replacement lookup does not enforce intent constraint {key}", 422)
        requested = obj["request"]["lines"]
        actual = {line.product_id: line.units_needed for line in quote.lines}
        fixed = {l["product_id"]: l["quantity_sales_units"] for l in requested if l["product_id"]}
        if any(actual.get(k) != v for k, v in fixed.items()) or len(actual) != len(requested):
            raise DomainError("NEED_MISMATCH", "Quote quantities and lines must match the intent", 422)
        remaining = {pid: qty for pid, qty in actual.items() if pid not in fixed}
        resolved = []
        for line in requested:
            pid = line["product_id"]
            if not pid:
                pid = next((pid for pid, qty in remaining.items() if qty == line["quantity_sales_units"]), None)
                if pid is None:
                    raise DomainError("NEED_MISMATCH", "Resolved functional demand quantities must match the quote", 422)
                del remaining[pid]
            resolved.append({**line, "product_id": pid})
        return resolved

    def bind_quote(self, conn, obj, quote):
        obj["resolved_lines"] = self.resolve_quote(conn, obj, quote)
        if obj.get("quote_id") and obj["quote_id"] != quote.id:
            previous = self.gateway.orders._quote(conn, obj["quote_id"])
            if previous.status == "OPEN":
                previous.status, previous.expiration_reason = "EXPIRED", "intent_requoted"
                self.gateway.orders._save_quote(conn, previous)
        quote.intent_id, quote.intent_version = obj["id"], obj["version"]
        quote.signature = self.gateway.signing.sign_quote(quote)
        self.gateway.orders._save_quote(conn, quote)
        obj["quote_id"] = quote.id
        self.save(conn, obj, obj["version"], "QUOTED")

    def order_changed(self, conn, order):
        if not order.intent_id:
            return
        obj = self.load(conn, order.intent_id)
        if obj.get("order_id") not in {None, order.id}:
            raise DomainError("INTENT_VERSION_CONFLICT", "This intent already has an order", 409,
                              {"winning_state": obj["state"], "version": obj["version"]})
        newly_linked = obj.get("order_id") is None
        obj["order_id"] = order.id
        obj["submission_reference"] = obj.get("submission_reference") or order.id
        if newly_linked:
            p = PRINCIPAL.get()
            if p and p.id == order.agent_id:
                obj["dispatch_credential"] = p.public()
            elif (obj.get("dispatch_credential") or {}).get("id") != order.agent_id:
                obj["dispatch_credential"] = None
            if p and p.id == order.agent_id and self.gateway.settings.keys:
                obj["dispatch_credential"]["fingerprint"] = self.gateway.settings.keys.fingerprint(p)
        state = {"PENDING_APPROVAL": "PENDING_APPROVAL", "PENDING_EXECUTION": "ORDERED", "CONFIRMED": "ORDERED",
                 "CANCELLED": "CANCELLED", "REJECTED": "CANCELLED", "EXPIRED": "EXPIRED"}[order.status]
        if obj["state"] in TERMINAL or obj["state"] in {"CONFIRMED", "PARTIALLY_CONFIRMED", "CANCEL_REQUESTED", "CANCEL_DISPUTED", "SUBMISSION_UNKNOWN"}:
            return
        if state != obj["state"] or newly_linked:
            self.save(conn, obj, obj["version"], state)

    def stage_submission(self, conn, order, quote):
        order.status = "PENDING_EXECUTION"
        order.execution = Execution(mode="supplier_backed", status="not_submitted", supplier_ref=order.id)
        self.gateway.orders._sign_receipt(order, quote, order.decided_by_kind or "agent")
        self.gateway.orders._save_order(conn, order)

    def confirm(self, conn, obj, confirmed=None, *, human_override=False):
        requested = obj.get("resolved_lines", obj["request"]["lines"])
        quantities = {l["product_id"]: l["quantity_sales_units"] for l in requested}
        confirmed = quantities if confirmed is None else confirmed
        if any(pid not in quantities or isinstance(q, bool) or not isinstance(q, (int, float)) or not 0 <= q <= quantities[pid] or q != int(q) for pid, q in confirmed.items()):
            raise DomainError("VALIDATION_ERROR", "Confirmation quantities must be finite, nonnegative and within the requested lines", 422)
        share = sum(min(confirmed.get(pid, 0) / qty, 1) for pid, qty in quantities.items()) / len(quantities)
        essential_missing = any(l["essential"] and confirmed.get(l["product_id"], 0) < l["quantity_sales_units"] for l in requested)
        policy = obj["request"]["fulfilment_policy"]
        allowed = human_override or (not essential_missing and (share == 1 or (policy["mode"] == "allow_partial" and share >= policy["minimum_share"])))
        obj["supplier_confirmed_lines"] = confirmed
        obj["committed_lines"] = {pid: q for pid, q in confirmed.items() if q > 0} if allowed else {}
        obj["remainder"] = [{**l, "quantity_sales_units": l["quantity_sales_units"] - obj["committed_lines"].get(l["product_id"], 0), "state": "OPEN", "next_action": "LOOKUP_REQUIRED"}
            for l in requested if obj["committed_lines"].get(l["product_id"], 0) < l["quantity_sales_units"]]
        obj["human_required"] = None if allowed else "Essential lines missing or fulfilment policy minimum not met"
        self.save(conn, obj, obj["version"], "CONFIRMED" if allowed and share == 1 else "PARTIALLY_CONFIRMED" if allowed else "PENDING_APPROVAL")
        if obj.get("order_id"):
            order = self.gateway.orders._order(conn, obj["order_id"])
            order.status = "CONFIRMED" if allowed else "PENDING_APPROVAL"
            order.order_revision += 1
            if allowed and share < 1:
                order.lines = [l.model_copy(update={"units_needed": obj["committed_lines"][l.product_id]}) for l in order.lines if l.product_id in obj["committed_lines"]]
                order.total = round(sum(l.unit_price * l.units_needed for l in order.lines), 2)
            order.execution = Execution(mode=order.execution.mode if order.execution else "simulated", status="accepted" if allowed else "held",
                                        supplier_ref=obj.get("supplier_reference") or obj["submission_reference"], acknowledged_at=audit.utcnow())
            if allowed and order.execution.mode == "supplier_backed":
                order.commitment = "supplier-acknowledged"
            self.gateway.orders._sign_receipt(order, self.gateway.orders._quote(conn, order.quote_id), "system")
            self.gateway.orders._save_order(conn, order)
            self.gateway.orders._reserve(conn, order)
        if allowed and share < 1:
            self.log(conn, obj, "INTENT_PARTIALLY_CONFIRMED", obj["active_agent"], remainder=obj["remainder"])

    def transition(self, id, body, agent, mandate=None):
        with self.db.connect(write=True) as conn:
            obj = self.load(conn, id)
            self.authority(conn, obj, agent, mandate)
            self.check_version(obj, body.expected_version)
            if body.state == "CONFIRMED":
                if not obj.get("order_id") or obj["state"] not in {"ORDERED", "SUBMISSION_UNKNOWN"}:
                    raise DomainError("INVALID_TRANSITION", "Only a submitted order can be confirmed", 409)
                p = PRINCIPAL.get()
                if not p or not p.operator:
                    raise DomainError("OPERATOR_REQUIRED", "Supplier confirmations require an operator", 403)
                self.confirm(conn, obj, body.confirmed_lines)
            elif body.state == "CANCELLED":
                self.cancel_locked(conn, obj, agent, mandate)
            else:
                if body.state == "EXPIRED" and obj.get("order_id"):
                    order = self.gateway.orders._order(conn, obj["order_id"])
                    if order.status != "EXPIRED":
                        raise DomainError("INVALID_TRANSITION", "An existing commercial order cannot be expired by the buyer", 409)
                if body.state == "CLOSED" and obj.get("remainder"):
                    raise DomainError("INVALID_TRANSITION", "Resolve or explicitly amend the open remainder first", 409)
                self.save(conn, obj, body.expected_version, body.state)
            return self.result(conn, obj)

    def cancel_locked(self, conn, obj, agent, mandate):
        if not obj.get("order_id"):
            self.save(conn, obj, obj["version"], "CANCELLED")
            return
        order = self.gateway.orders._order(conn, obj["order_id"])
        if any(l.availability.closing_time and l.availability.closing_time <= audit.utcnow() for l in order.lines):
            raise DomainError("CUTOFF_PASSED", "Cancellation cutoff has passed", 409)
        if order.execution and order.execution.mode == "supplier_backed":
            obj["cancel_reference"] = obj["submission_reference"]
            # Save the durable request before the adapter is contacted by recover().
            self.save(conn, obj, obj["version"], "CANCEL_REQUESTED")
        else:
            self.save(conn, obj, obj["version"], "CANCELLED")
            self.cancel_order_projection(conn, obj)
        self.log(conn, obj, "INTENT_CANCEL_REQUESTED", agent)

    def cancel_order_projection(self, conn, obj):
        if obj.get("order_id"):
            order = self.gateway.orders._order(conn, obj["order_id"])
            order.status = "CANCELLED"
            order.order_revision += 1
            self.gateway.orders._sign_receipt(order, self.gateway.orders._quote(conn, order.quote_id), "system")
            self.gateway.orders._save_order(conn, order)
            self.gateway.orders._reserve(conn, order)

    def event(self, id, event):
        with self.db.connect(write=True) as conn:
            obj = self.load(conn, id)
            if not obj.get("order_id"):
                raise DomainError("INVALID_TRANSITION", "A supplier event requires a submitted order", 409)
            reference = event.submission_reference or obj["submission_reference"]
            supplier = self.gateway.orders._order(conn, obj["order_id"]).supplier_id
            old = conn.execute("SELECT intent_id,data FROM purchase_intent_events WHERE tenant_id=? AND supplier_id=? AND event_id=?", (obj["tenant_id"], supplier, event.event_id)).fetchone()
            encoded = event.model_dump_json()
            if old:
                if old[0] != id or json.loads(old[1]) != json.loads(encoded):
                    raise DomainError("IDEMPOTENCY_CONFLICT", "Supplier event id was reused", 409)
                return self.result(conn, obj)
            if reference != obj["submission_reference"]:
                raise DomainError("INTENT_VERSION_CONFLICT", "Supplier event belongs to a previous order revision", 409,
                                  {"winning_state": obj["state"], "version": obj["version"]})
            if conn.execute("SELECT 1 FROM purchase_intent_events WHERE intent_id=? AND submission_reference=? AND sequence=?", (id, reference, event.sequence)).fetchone():
                raise DomainError("IDEMPOTENCY_CONFLICT", "Supplier sequence was reused", 409)
            conn.execute("INSERT INTO purchase_intent_events(tenant_id,supplier_id,intent_id,event_id,submission_reference,sequence,data) VALUES(?,?,?,?,?,?,?)", (obj["tenant_id"], supplier, id, event.event_id, reference, event.sequence, encoded))
            while True:
                row = conn.execute("SELECT data FROM purchase_intent_events WHERE intent_id=? AND submission_reference=? AND sequence=?", (id, reference, obj["supplier_sequence"] + 1)).fetchone()
                if not row:
                    break
                incoming = SupplierEvent.model_validate_json(row[0])
                obj["supplier_sequence"] += 1
                if obj["state"] in TERMINAL:
                    self.save(conn, obj, obj["version"])
                elif incoming.kind == "cancelled":
                    self.save(conn, obj, obj["version"], "CANCELLED")
                    self.cancel_order_projection(conn, obj)
                elif incoming.kind == "accepted" and obj["state"] == "CANCEL_REQUESTED":
                    self.save(conn, obj, obj["version"], "CANCEL_DISPUTED")
                elif incoming.kind == "accepted" and obj["state"] != "CANCEL_DISPUTED":
                    self.confirm(conn, obj, incoming.confirmed_lines)
                elif incoming.kind == "rejected":
                    self.save(conn, obj, obj["version"], "CANCELLED")
                    self.cancel_order_projection(conn, obj)
                else:
                    self.save(conn, obj, obj["version"])
            return self.result(conn, obj)

    def set_faults(self, id, faults):
        if self.gateway.settings.environment != "sandbox":
            raise DomainError("SANDBOX_ONLY", "Fault injection is sandbox-only", 403)
        with self.db.connect(write=True) as conn:
            obj = self.load(conn, id)
            obj["faults"] = faults.model_dump()
            self.save(conn, obj, obj["version"])
            return self.result(conn, obj)

    def after_placement(self, conn, obj, order):
        if order.status != "PENDING_APPROVAL" and obj["state"] == "ORDERED":
            delay = obj["faults"].get("confirmation_delay_seconds", 0)
            if order.execution and order.execution.mode == "simulated":
                if delay:
                    obj["confirm_after"] = (audit.utcnow() + timedelta(seconds=delay)).isoformat()
                    order.status = "PENDING_EXECUTION"
                    self.gateway.orders._sign_receipt(order, self.gateway.orders._quote(conn, order.quote_id), "system")
                    self.gateway.orders._save_order(conn, order)
                    self.save(conn, obj, obj["version"])
                else:
                    self.confirm(conn, obj, obj["faults"].get("confirmed_lines"))

    def recover(self, id, agent, mandate=None):
        """Persist ambiguity before I/O; never infer absence from eventual consistency.

        Each invocation reconciles, then makes at most one same-reference retry.
        A second ambiguous submit exhausts automation and leaves a human action.
        """
        with self.db.connect(write=True) as conn:
            obj = self.load(conn, id)
            self.authority(conn, obj, agent, mandate)
            if (obj.get("human_required") and obj["state"] != "CANCEL_REQUESTED") or obj["state"] not in {"ORDERED", "SUBMISSION_UNKNOWN", "CANCEL_REQUESTED"}:
                return self.result(conn, obj)
            order = self.gateway.orders._order(conn, obj["order_id"])
            executor = self.gateway.suppliers.get(order.supplier_id).execution
            if obj.get("deferred_supplier_event") and obj["state"] != "CANCEL_REQUESTED":
                if audit.utcnow() < datetime.fromisoformat(obj["confirm_after"]):
                    return self.result(conn, obj)
                event = SupplierEvent.model_validate(obj.pop("deferred_supplier_event"))
                self.save(conn, obj, obj["version"])
                return self.event(id, event)
            if executor.mode == "simulated":
                if obj.get("confirm_after") and audit.utcnow() < datetime.fromisoformat(obj["confirm_after"]):
                    return self.result(conn, obj)
                self.confirm(conn, obj, obj["faults"].get("confirmed_lines"))
                return self.result(conn, obj)
            cancel = obj["state"] == "CANCEL_REQUESTED"
            if not cancel:
                obj["first_submitted_at"] = obj.get("first_submitted_at") or audit.utcnow().isoformat()
                self.save(conn, obj, obj["version"], "SUBMISSION_UNKNOWN")
        # The adapter receives the SAME gateway reference on every query and submit.
        stable = order.model_copy(update={"id": obj["submission_reference"]})
        answer = None
        try:
            if cancel:
                answer = executor.cancel(order.execution.supplier_ref or stable.id)
            else:
                answer = executor.reconcile(stable)
                window = getattr(executor, "consistency_window_seconds", None)
                elapsed = (audit.utcnow() - datetime.fromisoformat(obj["first_submitted_at"])).total_seconds()
                if answer is None and obj["submission_attempts"] and (window is None or elapsed < window):
                    return self.get(id, agent, mandate)
                if answer is None and obj["submission_attempts"] < 2:
                    with self.db.connect(write=True) as conn:
                        current = self.load(conn, id)
                        # The claim serializes concurrent dispatchers, and survives a crash.
                        if current["version"] != obj["version"]:
                            return self.result(conn, current)
                        self.authority(conn, current, agent, mandate, limits=True)
                        current["submission_attempts"] += 1
                        self.save(conn, current, current["version"])
                        obj = current
                    answer = executor.submit(stable)
        except Exception as error:
            if isinstance(error, DomainError) and error.code not in {"SUPPLIER_UNAVAILABLE", "UPSTREAM_BUSY", "UPSTREAM_RATE_LIMITED"}:
                raise
            if not isinstance(error, (DomainError, TimeoutError, OSError)) and error.__class__.__module__.split(".")[0] != "httpx":
                raise
        with self.db.connect(write=True) as conn:
            obj = self.load(conn, id)
            if obj["state"] in TERMINAL:
                return self.result(conn, obj)
            if answer and answer.status in {"accepted", "cancelled", "rejected"}:
                obj["supplier_reference"] = answer.supplier_ref
                self.save(conn, obj, obj["version"])
                seq = obj["supplier_sequence"] + 1
                event = SupplierEvent(event_id=f"reconcile:{stable.id}:{answer.status}", sequence=seq,
                    kind=answer.status, confirmed_lines=(obj["faults"].get("confirmed_lines")
                        if obj["faults"].get("confirmed_lines") is not None else getattr(answer, "confirmed_lines", None)))
                delay = obj["faults"].get("confirmation_delay_seconds", 0)
                if answer.status == "accepted" and delay:
                    obj["deferred_supplier_event"] = event.model_dump()
                    obj["confirm_after"] = (audit.utcnow() + timedelta(seconds=delay)).isoformat()
                    self.save(conn, obj, obj["version"])
                    return self.result(conn, obj)
                return self.event(id, event)
            window = getattr(executor, "consistency_window_seconds", None)
            elapsed = (audit.utcnow() - datetime.fromisoformat(obj["first_submitted_at"])).total_seconds() if obj.get("first_submitted_at") else 0
            if (obj["submission_attempts"] >= 2 or (not cancel and window is None) or
                    (not cancel and answer is not None and window is not None and elapsed >= window)):
                obj["human_required"] = "Supplier outcome remains unknown; reconcile with the supplier using the stable reference"
                self.save(conn, obj, obj["version"])
            elif not cancel:
                obj["next_reconcile_at"] = (datetime.fromisoformat(obj["first_submitted_at"]) + timedelta(seconds=executor.consistency_window_seconds)).isoformat()
                self.save(conn, obj, obj["version"])
            return self.result(conn, obj)

    def dispatch_pending(self):
        """The normal gateway worker reconciles durable intent submissions."""
        from .auth import Principal
        with self.db.connect() as conn:
            pending = [json.loads(row[0]) for row in conn.execute("SELECT data FROM purchase_intents WHERE json_extract(data,'$.state') IN ('ORDERED','SUBMISSION_UNKNOWN','CANCEL_REQUESTED')")]
        for obj in pending:
            if obj.get("human_required") and obj["state"] != "CANCEL_REQUESTED":
                continue
            due = obj.get("confirm_after") or obj.get("next_reconcile_at")
            if due and audit.utcnow() < datetime.fromisoformat(due):
                continue
            with self.db.connect() as conn:
                order = self.gateway.orders._order(conn, obj["order_id"])
            credential = obj.get("dispatch_credential") or {}
            p = Principal(order.agent_id, tenant_id=obj["tenant_id"], customer_ids=(order.customer_id,),
                          credential_ref=credential.get("credential_ref"), connection_id=credential.get("connection_id"), scopes=tuple(credential.get("scopes", ["*"])))
            token = PRINCIPAL.set(p)
            try:
                with self.db.connect(write=True) as conn:
                    self.gateway.orders._dispatch_authority(conn, order, self.gateway.orders._quote(conn, order.quote_id), obj.get("dispatch_credential"))
                self.recover(obj["id"], order.agent_id)
            except DomainError as error:
                with self.db.connect(write=True) as conn:
                    current = self.load(conn, obj["id"])
                    current["human_required"] = error.message
                    self.save(conn, current, current["version"])
                    self.log(conn, current, "INTENT_DISPATCH_HELD", order.agent_id, code=error.code)
            finally:
                PRINCIPAL.reset(token)


def install(gateway):
    """Small core hooks; legacy quotes/orders keep their original paths."""
    intents = gateway.intents = PurchaseIntents(gateway)
    orders = gateway.orders
    original_quote, original_place = orders.create_quote, orders.place_order
    original_save, original_execute, original_cancel = orders._save_order, orders._execute, orders.cancel

    @wraps(original_quote)
    def create_quote(request, agent_id="external", *, operator=False, mandate=None, **kwargs):
        if not request.intent_id:
            return original_quote(request, agent_id, operator=operator, mandate=mandate, **kwargs)
        with gateway.db.connect(write=True) as conn:
            obj = intents.quote_check(conn, request, agent_id, mandate)
            quote = original_quote(request, agent_id, operator=operator, mandate=mandate, **kwargs)
            intents.bind_quote(conn, obj, quote)
            return quote

    @wraps(original_place)
    def place_order(quote_id, idempotency_key, agent_id="external", *, operator=False, mandate=None, **kwargs):
        with gateway.db.connect() as conn:
            row = conn.execute("SELECT data FROM quotes WHERE id=?", (quote_id,)).fetchone()
            linked = row and json.loads(row[0]).get("intent_id")
        if not linked:
            return original_place(quote_id, idempotency_key, agent_id, operator=operator, mandate=mandate, **kwargs)
        with gateway.db.connect() as conn:
            quote = orders._quote(conn, quote_id)
        dropped = False
        with gateway.db.connect(write=True) as conn:
            obj = intents.load(conn, quote.intent_id)
            intents.authority(conn, obj, agent_id, mandate)
            if not idempotency_key or not idempotency_key.strip():
                raise DomainError("IDEMPOTENCY_REQUIRED", "A non-empty idempotency key is required", 422)
            previous = conn.execute("SELECT intent_id FROM purchase_intent_commands WHERE tenant_id=? AND agent_id=? AND key=?", (obj["tenant_id"], agent_id, idempotency_key)).fetchone()
            if previous and previous[0] != obj["id"]:
                raise DomainError("IDEMPOTENCY_CONFLICT", "Idempotency key belongs to another buying decision", 409)
            if obj.get("order_id"):
                conn.execute("INSERT OR IGNORE INTO purchase_intent_commands(tenant_id,agent_id,key,intent_id) VALUES(?,?,?,?)", (obj["tenant_id"], agent_id, idempotency_key, obj["id"]))
                return orders._order(conn, obj["order_id"])
            if obj["state"] != "QUOTED" or obj["quote_id"] != quote.id:
                raise DomainError("INTENT_VERSION_CONFLICT", "Quote no longer represents the intent", 409,
                                  {"winning_state": obj["state"], "version": obj["version"]})
            order = original_place(quote_id, "intent:" + obj["id"] + ":" + quote_id, agent_id, operator=operator, mandate=mandate, **kwargs)
            conn.execute("INSERT OR IGNORE INTO purchase_intent_commands(tenant_id,agent_id,key,intent_id) VALUES(?,?,?,?)", (obj["tenant_id"], agent_id, idempotency_key, obj["id"]))
            if obj.get("additional_approval") and order.status != "PENDING_APPROVAL":
                # Never turn a hard refusal into an approvable purchase.
                if order.status != "REJECTED":
                    order.status = "PENDING_APPROVAL"
                order.policy.hits.append(RuleHit(rule="additional_demand", detail="Overlapping additional demand requires human approval"))
                order.policy.outcome = order.status
                orders._sign_receipt(order, quote, order.decided_by_kind or "agent")
            orders._save_order(conn, order)
            orders._reserve(conn, order)
            obj = intents.load(conn, obj["id"])
            intents.after_placement(conn, obj, order)
            order = orders._order(conn, order.id)
            if obj["faults"].get("drop_order_response"):
                dropped = True
                obj = intents.load(conn, obj["id"])
                obj["faults"]["drop_order_response"] = False
                intents.save(conn, obj, obj["version"])
        if dropped:
            raise DomainError("INTENT_RESPONSE_DROPPED", "Sandbox dropped the response after the order committed", 503)
        return order

    def save_order(conn, order):
        original_save(conn, order)
        intents.order_changed(conn, order)

    def execute(conn, order, quote):
        if quote.intent_id:
            return intents.stage_submission(conn, order, quote)
        return original_execute(conn, order, quote)

    @wraps(original_cancel)
    def cancel(order_id, actor="system", reason="", **kwargs):
        with gateway.db.connect() as conn:
            row = conn.execute("SELECT data FROM orders WHERE id=?", (order_id,)).fetchone()
            linked = row and json.loads(row[0]).get("intent_id")
        if not linked:
            return original_cancel(order_id, actor, reason, **kwargs)
        with gateway.db.connect() as conn:
            order = orders._order(conn, order_id)
        agent = actor.removeprefix("agent:")
        with gateway.db.connect(write=True) as conn:
            obj = intents.load(conn, order.intent_id)
            p = PRINCIPAL.get()
            if not p or not p.operator:
                intents.authority(conn, obj, agent, kwargs.get("mandate"))
            intents.cancel_locked(conn, obj, agent, kwargs.get("mandate"))
            return orders._order(conn, order_id)

    orders.create_quote, orders.place_order = create_quote, place_order
    orders._save_order, orders._execute, orders.cancel = save_order, execute, cancel

    # Human decisions retain the original approval and original buying mandate.
    original_approve = orders.approve

    @wraps(original_approve)
    def approve(order_id, *args, **kwargs):
        with gateway.db.connect() as conn:
            row = conn.execute("SELECT data FROM orders WHERE id=?", (order_id,)).fetchone()
            linked = row and json.loads(row[0]).get("intent_id")
            if linked:
                obj = intents.load(conn, linked)
                if obj.get("pending_amendment") or obj.get("human_required"):
                    raise DomainError("INTENT_AMENDMENT_REQUIRED", "Review the intent change or supplier partial outcome explicitly before approval", 409,
                                      {"intent_id": obj["id"], "state": obj["state"]})
        # The existing approval boundary preserves refreshed evidence on 409.
        order = original_approve(order_id, *args, **kwargs)
        if linked:
            with gateway.db.connect(write=True) as conn:
                obj = intents.load(conn, linked)
                intents.after_placement(conn, obj, order)
                order = orders._order(conn, order_id)
        return order

    orders.approve = approve

    def review(id, body, principal, *, partial=False):
        """Human-approved partial acceptance or cancel-and-replace amendment.

        Supplier-backed changes wait for cancellation acknowledgement before a
        replacement can be created. All writes, fresh policy/evidence checks and
        the approval commit together; refusal rolls the whole change back.
        """
        from .autonomy import authorize_review
        with gateway.db.connect(write=True) as conn:
            obj = intents.load(conn, id)
            intents.check_version(obj, body.expected_version)
            order = orders._order(conn, obj["order_id"])
            authorize_review(gateway.policy.get(), principal, order)
            if partial:
                if obj.get("pending_amendment") or obj["state"] != "PENDING_APPROVAL" or not obj.get("supplier_confirmed_lines"):
                    raise DomainError("INVALID_TRANSITION", "There is no held supplier confirmation to review", 409)
                confirmed = obj["supplier_confirmed_lines"]
                obj["human_required"] = None
                intents.save(conn, obj, obj["version"])
                original_approve(order.id, body.approver, evidence_revision=order.evidence_revision, actor="human:" + (principal.user_id or principal.id))
                obj = intents.load(conn, id)
                intents.confirm(conn, obj, confirmed, human_override=True)
                intents.log(conn, obj, "INTENT_PARTIAL_APPROVED", principal.id)
                return intents.result(conn, obj)
            if not obj.get("pending_amendment") or not body.quote_id:
                raise DomainError("VALIDATION_ERROR", "A pending amendment and a fresh replacement quote_id are required", 422)
            replacement = orders._quote(conn, body.quote_id)
            token = gateway.mandates.stored(conn, "quote", replacement.id)
            intents.authority(conn, obj, replacement.agent_id, token)
            # Validate immutable quote evidence and the replacement demand before cancelling anything.
            orders._verified_quote(conn, replacement.id, replacement.agent_id)
            if replacement.status != "OPEN" or replacement.expires_at <= audit.utcnow():
                raise DomainError("EXPIRED_QUOTE", "Replacement quote must be open and unexpired", 409)
            intents.resolve_quote(conn, obj, replacement)
            if order.execution and order.execution.mode == "supplier_backed" and obj["state"] != "CANCELLED":
                if obj["state"] != "CANCEL_REQUESTED":
                    intents.cancel_locked(conn, obj, principal.id, None)
                return intents.result(conn, obj)
            if obj["state"] != "CANCELLED":
                intents.cancel_locked(conn, obj, principal.id, None)
            obj.setdefault("previous_order_ids", []).append(order.id)
            obj.update(order_id=None, submission_reference=None, supplier_reference=None, submission_attempts=0,
                       supplier_sequence=0, human_required=None, pending_amendment=None, quote_id=None,
                       committed_lines={}, remainder=[])
            for key in ("first_submitted_at", "confirm_after", "next_reconcile_at", "deferred_supplier_event",
                        "cancel_reference", "supplier_confirmed_lines", "dispatch_credential"):
                obj.pop(key, None)
            intents.bind_quote(conn, obj, replacement)
            new_order = orders.place_order(replacement.id, "amendment:" + id + ":" + str(obj["version"]), replacement.agent_id, mandate=token)
            if new_order.status == "PENDING_APPROVAL":
                new_order = original_approve(new_order.id, body.approver, evidence_revision=new_order.evidence_revision, actor="human:" + (principal.user_id or principal.id))
                obj = intents.load(conn, id)
                intents.after_placement(conn, obj, new_order)
            obj = intents.load(conn, id)
            intents.log(conn, obj, "INTENT_CHANGE_APPROVED", principal.id, previous_order_id=order.id)
            return intents.result(conn, obj)

    intents.review = review
