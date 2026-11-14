"""Three-layer authority, immutable policy history and buyer-business governance.

Resolution and field comparisons live here only. Procurement adapters project
these results into the existing mandate/evaluator without inventing authority.
"""
import json
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4

from . import audit
from .canonical import canonical_json
from .db import DomainError
from .governance_models import AgentPolicy, EffectivePolicy, PolicyField

ALLOW_SETS = {"allowed_customers", "allowed_operations", "allowed_categories", "ordering_hours",
              "approval_routing.approver_user_ids"}
REQUIRE_SETS = {"blocked_categories", "required_claims", "excluded_allergens"}
AUTONOMY = ["approval_required", "autonomous_within_limits"]
ALERT_TYPES = {"budget.80", "budget.100", "refusal.burst", "activity.outside_hours",
               "connection.new_client", "connection.new_address", "policy.change_pending"}


def migrate(conn):
    for table, fields in {
        "tenant_agents": {"owner_user_id": "TEXT", "policy_author_user_id": "TEXT", "status": "TEXT NOT NULL DEFAULT 'active'",
                          "paused_by": "TEXT", "paused_at": "TEXT", "pause_reason": "TEXT"},
        "tenants": {"paused_by": "TEXT", "paused_at": "TEXT", "pause_reason": "TEXT"},
        "connections": {"last_address": "TEXT"},
    }.items():
        columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, declaration in fields.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
    # Keep tenant_id explicit for both tenant-scoped and platform versions.
    for statement in [
        "CREATE TABLE IF NOT EXISTS governance_versions(id TEXT PRIMARY KEY,layer TEXT NOT NULL,target TEXT NOT NULL,data TEXT NOT NULL,author TEXT NOT NULL,created_at TEXT NOT NULL,reason TEXT NOT NULL,diff TEXT NOT NULL,tenant_id TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS governance_heads(layer TEXT NOT NULL,target TEXT NOT NULL,version_id TEXT NOT NULL REFERENCES governance_versions(id),tenant_id TEXT NOT NULL,PRIMARY KEY(layer,target))",
        "CREATE TABLE IF NOT EXISTS governance_requests(id TEXT PRIMARY KEY,agent_id TEXT NOT NULL,requester TEXT NOT NULL,base_version TEXT NOT NULL,status TEXT NOT NULL,data TEXT NOT NULL,tenant_id TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS governance_effective(id TEXT PRIMARY KEY,data TEXT NOT NULL,tenant_id TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS governance_alerts(id TEXT PRIMARY KEY,agent_id TEXT NOT NULL,type TEXT NOT NULL,created_at TEXT NOT NULL,dedup TEXT NOT NULL UNIQUE,data TEXT NOT NULL,tenant_id TEXT NOT NULL)",
    ]:
        conn.execute(statement)
    for table in ("governance_versions", "governance_effective"):
        for operation in ("UPDATE", "DELETE"):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'governance history is immutable'); END")


def flatten(value, prefix=""):
    result = {}
    for key, val in value.items():
        name = f"{prefix}.{key}" if prefix else key
        result.update(flatten(val, name) if isinstance(val, dict) else {name: val})
    return result


def expand(fields):
    result = {}
    for field, value in fields.items():
        obj = result
        parts = field.split(".")
        for part in parts[:-1]:
            obj = obj.setdefault(part, {})
        obj[parts[-1]] = value
    return result


def restrictive(field, before, after):
    """Meet in the permission lattice; null bounds and allowlists are unbounded."""
    if field in ALLOW_SETS:
        if before is None:
            return sorted(set(after)) if after is not None else None
        if after is None:
            return sorted(set(before))
        return sorted(set(before) & set(after))
    if field in REQUIRE_SETS:
        return sorted(set(before or []) | set(after or []))
    if field == "autonomy_level":
        return min((before, after), key=AUTONOMY.index)
    if field == "substitutes_allowed":
        return before and after
    if field == "delivery_day_window.min_days":
        if before is None:
            return after
        if after is None:
            return before
        return max(before, after)
    if before is None:
        return after
    if after is None:
        return before
    return min(before, after)


def classify_change(before, after):
    """One comparator for proposals, guardrails and rollbacks, field by field.

    Incomparable set changes contain both directions and require review.
    """
    old, new = flatten(before), flatten(after)
    result = {}
    for field, value in new.items():
        previous = old.get(field)
        if previous == value or field in ALLOW_SETS | REQUIRE_SETS and previous is not None and value is not None and set(previous) == set(value):
            result[field] = "unchanged"
            continue
        meet = restrictive(field, previous, value)
        result[field] = "tightening" if meet == value else "loosening" if meet == previous else "mixed"
    return result


def merge_policy(before, patch):
    # Validate the complete merged object, including unknown nested keys.
    combined = {**before, **patch}
    for key in ("approval_routing", "delivery_day_window"):
        if key in patch and isinstance(patch[key], dict):
            combined[key] = {**before[key], **patch[key]}
    from pydantic import ValidationError
    try:
        return AgentPolicy.model_validate(combined).model_dump(mode="json")
    except ValidationError as error:
        from .recovery import validation_problem
        raise validation_problem(error) from error


class Governance:
    def __init__(self, gateway):
        self.gateway, self.db = gateway, gateway.db
        with self.db.connect(write=True) as conn:
            conn.execute("INSERT OR IGNORE INTO workspace(key,value) VALUES('governance_adopted_at',?)", (audit.utcnow().isoformat(),))
            self.adopted_at = conn.execute("SELECT value FROM workspace WHERE key='governance_adopted_at'").fetchone()[0]
            conn.execute("UPDATE tenant_agents SET owner_user_id=(SELECT id FROM users WHERE users.tenant_id=tenant_agents.tenant_id AND role='tenant_admin' AND disabled_at IS NULL AND removed_at IS NULL ORDER BY created_at,id LIMIT 1) WHERE owner_user_id IS NULL")
            conn.execute("UPDATE tenant_agents SET policy_author_user_id=owner_user_id WHERE policy_author_user_id IS NULL")

    def own_new_agent(self, conn, agent_id, tid, p=None):
        uid = p.user_id if p and not p.operator and p.role in {"member", "approver", "tenant_admin"} else None
        owner = conn.execute("SELECT id FROM users WHERE tenant_id=? AND role='tenant_admin' AND disabled_at IS NULL AND removed_at IS NULL ORDER BY created_at,id LIMIT 1", (tid,)).fetchone()
        uid = uid or (owner[0] if owner else None)
        if not uid:
            raise DomainError("TENANT_ADMIN_REQUIRED", "Invite the tenant's first administrator before creating agents", 403)
        conn.execute("UPDATE tenant_agents SET owner_user_id=?,policy_author_user_id=? WHERE agent_id=? AND owner_user_id IS NULL", (uid, uid, agent_id))

    @staticmethod
    def member(p):
        if not p or not (p.operator or p.kind == "tenant_user" and p.role in {"member", "approver", "tenant_admin"}):
            raise DomainError("TENANT_ADMIN_REQUIRED", "A member or tenant administrator is required", 403)

    def target(self, conn, agent_id, p):
        self.member(p)
        row = conn.execute("SELECT * FROM main.tenant_agents WHERE agent_id=?", (agent_id,)).fetchone()
        if not row or not p.operator and (row["tenant_id"] != p.tenant_id or p.role != "tenant_admin" and row["owner_user_id"] != p.user_id):
            raise DomainError("AGENT_NOT_FOUND", "Agent was not found", 404)
        return row

    def check_active(self, agent_id, conn=None):
        if conn is None:
            with self.db.connect() as conn:
                return self.check_active(agent_id, conn)
        row = conn.execute("SELECT a.*,t.paused_by AS tenant_paused_by,t.paused_at AS tenant_paused_at FROM main.tenant_agents a JOIN main.tenants t ON t.id=a.tenant_id WHERE a.agent_id=?", (agent_id,)).fetchone()
        if row and (row["status"] == "paused" or row["tenant_paused_at"]):
            raise DomainError("AGENT_PAUSED", "Agent is paused; contact the owner or tenant administrator to resume it", 423,
                {"agent_id": agent_id, "paused_by": row["tenant_paused_by"] or row["paused_by"],
                 "paused_at": row["tenant_paused_at"] or row["paused_at"]})
        if row and (row["disabled_at"] or row["status"] == "disabled"):
            raise DomainError("UNAUTHENTICATED", "Agent is disabled; contact its administrator", 401)

    def note_refusal(self, agent_id, error):
        if getattr(error, "audited", False):
            return
        with self.db.connect(write=True) as conn:
            audit.append(conn, "agent:" + agent_id, "AGENT_CALL_FAILED", error.message,
                         data={"error": error.code, **error.data, "live_calls": {"price": 0, "availability": 0}})
        error.audited = True

    def _baseline(self, conn, layer, target, policy, agent=None):
        policy = self.gateway.policy.get(tenant_overlay=False)
        data = AgentPolicy().model_dump(mode="json")
        author = "valio-admin"
        if layer == "platform":
            data["per_order_ceiling_eur"] = policy.approval.max_auto_total_eur
            # The spec 40 daily default is an issuance default, not a retroactive
            # platform limit for already signed buyer mandates (spec 12).
            author = policy.updated_by or author
        elif layer == "tenant":
            data["allowed_customers"] = [r[0] for r in conn.execute("SELECT customer_id FROM main.tenant_customers WHERE tenant_id=? ORDER BY customer_id", (target,))]
            first = conn.execute("SELECT id FROM main.users WHERE tenant_id=? AND role='tenant_admin' ORDER BY created_at,id LIMIT 1", (target,)).fetchone()
            author = first[0] if first else "valio-admin"
            overlay = conn.execute("SELECT data FROM main.tenant_policy_overlays WHERE id=?", (target,)).fetchone()
            if overlay:
                approval = json.loads(overlay[0]).get("approval", {})
                for key, field in (("max_auto_total_eur", "per_order_ceiling_eur"), ("max_auto_daily_total_eur", "daily_budget_eur")):
                    data[field] = approval.get(key)
        else:
            row = policy.agents.get(target)
            if row:
                data.update(allowed_customers=row.customer_ids, per_order_ceiling_eur=row.max_auto_total_eur,
                    autonomy_level=row.autonomy or policy.autonomy.default)
                # Calendar governance budgets are opt-in. The existing rolling
                # autonomous-only mandate limit remains a separate authority;
                # adopting governance must not count past human-approved spend
                # against that legacy limit retroactively.
            else:
                grant = conn.execute("SELECT level FROM agent_autonomy WHERE agent_id=?", (target,)).fetchone()
                if grant:
                    data["autonomy_level"] = grant[0]
            author = policy.updated_by or (agent["policy_author_user_id"] if agent else "valio-admin")
        vid = "base_" + sha256(canonical_json([layer, target, author, data])).hexdigest()
        return {"id": vid, "layer": layer, "target": target, "data": data, "author": author,
                "created_at": policy.updated_at.isoformat() if policy.updated_at else self.adopted_at,
                "reason": "Existing authority at governance adoption", "diff": {}}

    def layer(self, conn, layer, target, policy, agent=None):
        base = self._baseline(conn, layer, target, policy, agent)
        row = conn.execute("SELECT v.* FROM main.governance_heads h JOIN main.governance_versions v ON v.id=h.version_id WHERE h.layer=? AND h.target=?", (layer, target)).fetchone()
        if not row:
            return base
        version = dict(row)
        version["data"], version["diff"] = json.loads(row["data"]), json.loads(row["diff"])
        if layer == "agent":
            # A version freezes its historical snapshot. At runtime, fields never
            # edited by governance continue to inherit the existing mandate; an
            # autonomy-only version must not accidentally replace customer scope.
            overrides = set()
            for change in conn.execute("SELECT diff FROM main.governance_versions WHERE layer='agent' AND target=? AND rowid<=(SELECT rowid FROM main.governance_versions WHERE id=?)", (target, version["id"])):
                overrides.update(json.loads(change[0]))
            baseline_fields, saved = flatten(base["data"]), flatten(version["data"])
            version["data"] = expand({field: saved[field] if field in overrides else value for field, value in baseline_fields.items()})
            version["field_sources"] = {field: {"source_layer": "agent", "version_id": base["id"], "author": base["author"]}
                                        for field in baseline_fields if field not in overrides}
        return version

    def resolve(self, agent_id, *, conn=None, policy=None, proposed=None):
        if conn is None:
            with self.db.connect() as conn:
                return self.resolve(agent_id, conn=conn, policy=policy, proposed=proposed)
        # Baseline versions always describe persisted authority, never a projected
        # or VC-narrowed request-local resource passed back by the evaluator.
        policy = self.gateway.policy.get(tenant_overlay=False)
        agent = conn.execute("SELECT * FROM main.tenant_agents WHERE agent_id=?", (agent_id,)).fetchone()
        # Unregistered identities still fail the existing mandate boundary. Never
        # create a mandate while merely resolving governance.
        tid = agent["tenant_id"] if agent else self.db_tenant(conn, agent_id)
        layers = [self.layer(conn, "platform", "platform", policy), self.layer(conn, "tenant", tid, policy),
                  self.layer(conn, "agent", agent_id, policy, agent)]
        if proposed is not None:
            layers[-1] = {**layers[-1], "data": proposed}
        fields = {}
        for layer in layers:
            for field, value in flatten(layer["data"]).items():
                source = {"source_layer": layer["layer"], "version_id": layer["id"], "author": layer["author"]}
                source = layer.get("field_sources", {}).get(field, source)
                contribution = {**source, "value": value}
                if field not in fields:
                    fields[field] = {"value": value, **source, "sources": [contribution]}
                else:
                    old = fields[field]
                    meet = restrictive(field, old["value"], value)
                    old["sources"].append(contribution)
                    if meet != old["value"]:
                        old.update(value=meet, **source)
        vid = "eff_" + sha256(canonical_json({"agent_id": agent_id, "layers": [v["id"] for v in layers], "fields": fields})).hexdigest()
        return EffectivePolicy(agent_id=agent_id, tenant_id=tid, version_id=vid,
                               fields={k: PolicyField(**v) for k, v in fields.items()})

    @staticmethod
    def db_tenant(conn, agent_id):
        from .tenancy import TARGET_TENANT, HOTEL
        from .foundation import PRINCIPAL
        p = PRINCIPAL.get()
        return TARGET_TENANT.get() or (p.tenant_id if p and p.tenant_id else HOTEL)

    @staticmethod
    def values(effective):
        return expand({field: meta.value for field, meta in effective.fields.items()})

    def categories(self, effective, permitted=None):
        values = self.values(effective)
        allowed, blocked = values["allowed_categories"], set(values["blocked_categories"])
        categories = set(permitted) if permitted is not None else None
        if allowed is not None:
            categories = set(allowed) if categories is None else categories & set(allowed)
        if blocked:
            if categories is None:
                with self.db.connect() as conn:
                    categories = {r[0] for r in conn.execute("SELECT DISTINCT category FROM products UNION SELECT DISTINCT json_extract(data,'$.category') FROM supplier_products")}
            categories -= blocked
        return sorted(c for c in categories if c is not None) if categories is not None else None

    def snapshot(self, conn, effective):
        conn.execute("INSERT OR IGNORE INTO governance_effective(id,data,tenant_id) VALUES(?,?,?)",
                     (effective.version_id, effective.model_dump_json(), effective.tenant_id))
        # Also retain immutable baseline versions referenced by effective fields.
        policy = self.gateway.policy.get()
        agent = conn.execute("SELECT * FROM main.tenant_agents WHERE agent_id=?", (effective.agent_id,)).fetchone()
        for layer, target in (("platform", "platform"), ("tenant", effective.tenant_id), ("agent", effective.agent_id)):
            self._retain(conn, self.layer(conn, layer, target, policy, agent), "platform" if layer == "platform" else effective.tenant_id)
            if layer == "agent":
                self._retain(conn, self._baseline(conn, layer, target, policy, agent), effective.tenant_id)

    @staticmethod
    def _retain(conn, v, tid):
        conn.execute("INSERT OR IGNORE INTO governance_versions(id,layer,target,data,author,created_at,reason,diff,tenant_id) VALUES(?,?,?,?,?,?,?,?,?)",
                     (v["id"], v["layer"], v["target"], json.dumps(v["data"]), v["author"], v["created_at"], v["reason"], json.dumps(v["diff"]), tid))

    def project(self, agent_id, customer_id, policy, *, conn=None):
        """The only bridge from layered policy into existing procurement policy."""
        effective = self.resolve(agent_id, conn=conn, policy=policy)
        values = self.values(effective)
        resource = policy.model_copy(deep=True)
        resource._governance = effective
        limits = effective.fields["per_order_ceiling_eur"].sources
        parent_ceiling = restrictive("per_order_ceiling_eur", limits[0]["value"], limits[1]["value"])
        if parent_ceiling is not None:
            resource.approval.max_auto_total_eur = parent_ceiling
        resource._tenant_daily_ceiling = values["daily_budget_eur"]
        row = resource.agents.get(agent_id)
        if row:
            row.customer_ids = values["allowed_customers"] if values["allowed_customers"] is not None else row.customer_ids
            row.max_auto_total_eur = limits[2]["value"]
            if values["daily_budget_eur"] is not None:
                row.max_auto_daily_total_eur = min(row.max_auto_daily_total_eur, values["daily_budget_eur"])
            row.autonomy = values["autonomy_level"]
        if customer_id:
            from .models import CustomerConstraints
            customer = resource.customers.setdefault(customer_id, CustomerConstraints())
            customer.required_claims = list(dict.fromkeys([*customer.required_claims, *values["required_claims"]]))
            customer.exclude_allergens = list(dict.fromkeys([*customer.exclude_allergens, *values["excluded_allergens"]]))
        return resource

    def check_operation(self, conn, agent_id, operation, customer_id=None, deliver_by=None, products=(), effective=None):
        self.check_active(agent_id, conn)
        effective = effective or self.resolve(agent_id, conn=conn)
        values = self.values(effective)
        code, details = None, None
        if values["allowed_operations"] is not None and operation not in values["allowed_operations"]:
            code, details = "OPERATION_NOT_ALLOWED", operation
        if customer_id and values["allowed_customers"] is not None and customer_id not in values["allowed_customers"]:
            code, details = "CUSTOMER_NOT_IN_MANDATE", customer_id
        hours = values["ordering_hours"]
        if hours is not None and audit.utcnow().hour not in hours:
            self.alert(conn, agent_id, "activity.outside_hours", {"operation": operation, "policy_version_id": effective.version_id},
                       dedup=f"hours:{agent_id}:{audit.utcnow().isoformat()[:13]}")
            code, details = "OUTSIDE_ORDERING_HOURS", hours
        if deliver_by:
            days = (deliver_by - audit.utcnow().date()).days
            window = values["delivery_day_window"]
            if window["min_days"] is not None and days < window["min_days"] or window["max_days"] is not None and days > window["max_days"]:
                code, details = "OUTSIDE_DELIVERY_WINDOW", window
        catalog = self.gateway.suppliers.get("valio").catalog
        for product in products:
            product = catalog.get(product) if isinstance(product, str) else product
            if product.category in values["blocked_categories"] or values["allowed_categories"] is not None and product.category not in values["allowed_categories"]:
                code, details = "CATEGORY_NOT_ALLOWED", product.category
        if code:
            raise DomainError(code, "The effective agent policy refuses this operation", 403,
                              {"limit": details, "policy_version_id": effective.version_id})
        return effective

    def spend(self, conn, agent_id, *, at=None, exclude_order=None):
        at = at or audit.utcnow()
        day, month = Decimal(0), Decimal(0)
        for row in conn.execute("SELECT id,data FROM orders WHERE status IN ('CONFIRMED','PENDING_EXECUTION') AND json_extract(data,'$.agent_id')=?", (agent_id,)):
            obj = json.loads(row["data"])
            created = datetime.fromisoformat(obj["created_at"])
            if row["id"] == exclude_order or created > at:
                continue
            if created.date() == at.date():
                day += Decimal(str(obj["total"]))
            if (created.year, created.month) == (at.year, at.month):
                month += Decimal(str(obj["total"]))
        return {"day_eur": float(round(day, 2)), "month_eur": float(round(month, 2))}

    def evaluate(self, conn, effective, total, lines, *, at=None, spent=None, operation="order", deliver_by=None):
        """Read-only extra rule hits, shared by placement, preview and simulation."""
        from .models import PolicyResult, RuleHit
        values = self.values(effective)
        hits, reject = [], False
        spent = spent if spent is not None else self.spend(conn, effective.agent_id, at=at)
        for field, used in (("per_order_ceiling_eur", 0), ("daily_budget_eur", spent["day_eur"]), ("monthly_budget_eur", spent["month_eur"])):
            limit = values[field]
            if limit is not None and Decimal(str(used)) + Decimal(str(total)) > Decimal(str(limit)):
                hits.append(RuleHit(rule=field, detail=f"{used:.2f} + {total:.2f} exceeds {limit:.2f} EUR"))
        if values["autonomy_level"] == "approval_required":
            hits.append(RuleHit(rule="autonomy_level", detail="Agent policy requires human approval."))
        # Restricted routing has meaning even for otherwise autonomous orders.
        routing = values["approval_routing"]
        if routing["approver_user_ids"] is not None or routing["tenant_admin_above_eur"] is not None and total > routing["tenant_admin_above_eur"]:
            hits.append(RuleHit(rule="approval_routing", detail="A designated approver must review this order."))
        for line in lines:
            product = None
            category = getattr(line, "category", None)
            if category is None:
                try:
                    product = self.gateway.suppliers.get(line.supplier_id).catalog.get(line.product_id)
                    category = product.category
                except DomainError:
                    category = None
            if category in values["blocked_categories"] or values["allowed_categories"] is not None and category not in values["allowed_categories"]:
                hits.append(RuleHit(rule="CATEGORY_NOT_ALLOWED", detail=f"{line.product_id}: category is outside effective policy"))
                reject = True
            if values["required_claims"] or values["excluded_allergens"]:
                from .catalog import hard_violations
                if product is None:
                    try:
                        product = self.gateway.suppliers.get(line.supplier_id).catalog.get(line.product_id)
                    except DomainError:
                        pass
                violations = hard_violations(product, values["required_claims"], values["excluded_allergens"]) if product else ["Product evidence unavailable"]
                for violation in violations:
                    hits.append(RuleHit(rule="hard_constraint_violation", detail=violation))
                    reject = True
            if not values["substitutes_allowed"] and any(w.code == "OUTSIDE_PREFERENCE" for w in line.warnings):
                hits.append(RuleHit(rule="SUBSTITUTES_NOT_ALLOWED", detail=f"{line.product_id}: substitutes are disabled"))
                reject = True
        stamp = at or audit.utcnow()
        for field, invalid in (
            ("allowed_operations", values["allowed_operations"] is not None and operation not in values["allowed_operations"]),
            ("ordering_hours", values["ordering_hours"] is not None and stamp.hour not in values["ordering_hours"]),
        ):
            if invalid:
                hits.append(RuleHit(rule=field, detail="Outside the effective policy"))
                reject = True
        if deliver_by:
            window, days = values["delivery_day_window"], (deliver_by - stamp.date()).days
            if window["min_days"] is not None and days < window["min_days"] or window["max_days"] is not None and days > window["max_days"]:
                hits.append(RuleHit(rule="delivery_day_window", detail="Delivery date outside the effective window"))
                reject = True
        return PolicyResult(outcome="REJECTED" if reject else "PENDING_APPROVAL" if hits else "CONFIRMED", hits=hits)

    def _validate_bounds(self, proposed, boundaries, before=None):
        proposed_fields = flatten(proposed)
        changed = {field for field, direction in classify_change(before, proposed).items() if direction != "unchanged"} if before else set(flatten(proposed))
        for boundary in boundaries:
            limits = flatten(boundary["data"])
            for field, direction in classify_change(boundary["data"], proposed).items():
                # Null clears this layer's restriction and inherits its parents;
                # it cannot override a finite parent after resolution.
                if field in changed and proposed_fields[field] is not None and direction in {"loosening", "mixed"}:
                    raise DomainError("OUTSIDE_GUARDRAIL", "Proposed policy exceeds a governing limit", 422,
                        {"field": field, "limit": limits[field], "source_layer": boundary["layer"],
                         "version_id": boundary["id"], "author": boundary["author"]})

    def _write_version(self, conn, layer, target, tid, before, after, p, reason, *, action="POLICY_CHANGED", agent_id=None, emit_audit=True):
        self._retain(conn, before, tid)
        directions, previous = classify_change(before["data"], after), flatten(before["data"])
        diff = {key: {"before": previous[key], "after": value, "direction": directions[key]}
                for key, value in flatten(after).items() if directions[key] != "unchanged"}
        v = {"id": "pol_" + uuid4().hex, "layer": layer, "target": target, "data": after,
             "author": p.user_id or p.id, "created_at": audit.utcnow().isoformat(), "reason": reason, "diff": diff}
        self._retain(conn, v, tid)
        conn.execute("INSERT INTO governance_heads(layer,target,version_id,tenant_id) VALUES(?,?,?,?) ON CONFLICT(layer,target) DO UPDATE SET version_id=excluded.version_id",
                     (layer, target, v["id"], tid))
        if not emit_audit:
            return v
        targets = [agent_id] if agent_id else [r[0] for r in conn.execute("SELECT agent_id FROM main.tenant_agents WHERE (?='platform' OR tenant_id=?)", (layer, tid))]
        for aid in targets or [None]:
            audit.append(conn, v["author"], action, reason, data={"tenant_id": tid if layer != "platform" else self.db_tenant_for_agent(conn, aid), "agent_id": aid,
                         "version_id": v["id"], "layer": layer, "diff": diff})
        if layer == "agent" and "autonomy_level" in diff:
            policy = self.gateway.policy.get(tenant_overlay=False)
            record = self.gateway.autonomy._record(conn, target, policy)
            customers = after["allowed_customers"] or []
            audit.append(conn, v["author"], "AUTONOMY_CHANGED", reason, data={"tenant_id": tid, "agent_id": target,
                "version_id": v["id"], "before": before["data"]["autonomy_level"], "level": after["autonomy_level"],
                "reason": reason, "customer_ids": customers, "customer_id": customers[0] if len(customers) == 1 else None,
                "track_record": record.model_dump(mode="json")})
        return v

    @staticmethod
    def db_tenant_for_agent(conn, agent_id):
        row = conn.execute("SELECT tenant_id FROM main.tenant_agents WHERE agent_id=?", (agent_id,)).fetchone()
        return row[0] if row else "platform"

    def change(self, agent_id, patch, reason, p, *, rollback_version=None):
        with self.db.connect(write=True) as conn:
            agent, before, proposed = self._agent_proposal(conn, agent_id, patch, p, rollback_version)
            directions = classify_change(before["data"], proposed)
            # Admins may apply others' proposals directly; owners, including an
            # admin who owns this agent, may never self-approve a loosening.
            if any(d in {"loosening", "mixed"} for d in directions.values()):
                self._retain(conn, before, agent["tenant_id"])
                simulation = self._simulate(conn, agent, proposed)
                request = {"id": "pcr_" + uuid4().hex, "agent_id": agent_id, "requester": p.user_id or p.id,
                           "base_version": before["id"], "status": "pending", "policy": proposed, "reason": reason,
                           "created_at": audit.utcnow().isoformat(), "classification": directions,
                           "simulation": simulation, "rollback_version": rollback_version}
                conn.execute("INSERT INTO governance_requests(id,agent_id,requester,base_version,status,data,tenant_id) VALUES(?,?,?,?,?,?,?)",
                             (request["id"], agent_id, request["requester"], before["id"], "pending", json.dumps(request), agent["tenant_id"]))
                audit.append(conn, p.user_id or p.id, "POLICY_CHANGE_REQUESTED", reason,
                             data={"tenant_id": agent["tenant_id"], "agent_id": agent_id, "version_id": before["id"], "request_id": request["id"]})
                self.alert(conn, agent_id, "policy.change_pending", {"request_id": request["id"]}, dedup=request["id"])
                return {"status": "pending", "change_request": request}
            version = self._write_version(conn, "agent", agent_id, agent["tenant_id"], before, proposed, p, reason,
                                          action="POLICY_ROLLED_BACK" if rollback_version else "POLICY_CHANGED", agent_id=agent_id)
            return {"status": "applied", "version": version}

    def _agent_proposal(self, conn, agent_id, patch, p, rollback_version=None):
        agent = self.target(conn, agent_id, p)
        policy = self.gateway.policy.get(tenant_overlay=False)
        before = self.layer(conn, "agent", agent_id, policy, agent)
        if rollback_version:
            row = conn.execute("SELECT * FROM governance_versions WHERE id=? AND layer='agent' AND target=?", (rollback_version, agent_id)).fetchone()
            if not row:
                raise DomainError("POLICY_VERSION_NOT_FOUND", "Policy version was not found", 404)
            proposed = json.loads(row["data"])
        else:
            proposed = merge_policy(before["data"], patch)
        proposed = self._normalize_policy(conn, proposed, agent["tenant_id"])
        self._validate_bounds(proposed, [self.layer(conn, "platform", "platform", policy), self.layer(conn, "tenant", agent["tenant_id"], policy)], before["data"])
        return agent, before, proposed

    def _normalize_policy(self, conn, proposed, tid=None):
        proposed = dict(proposed)
        if tid is None and proposed["per_order_ceiling_eur"] is None:
            # The platform's inherited per-order rule is the existing global
            # policy default; lower layers inherit the resolved parent instead.
            proposed["per_order_ceiling_eur"] = self.gateway.policy.get(tenant_overlay=False).approval.max_auto_total_eur
        for field, vocabulary in (("required_claims", "required_claims"), ("excluded_allergens", "exclude_allergens")):
            proposed[field] = self.gateway.catalog.normalize_codes(proposed[field], vocabulary)
        approvers = proposed["approval_routing"]["approver_user_ids"]
        if approvers is not None:
            for uid in approvers:
                user = conn.execute("SELECT * FROM main.users WHERE id=? AND role IN ('approver','tenant_admin') AND disabled_at IS NULL AND removed_at IS NULL", (uid,)).fetchone()
                if not user or tid and user["tenant_id"] != tid:
                    raise DomainError("VALIDATION_ERROR", "Approval routing must name active approvers of this tenant", 422)
        return proposed

    def validate_change(self, agent_id, patch, p, rollback_version=None):
        with self.db.connect() as conn:
            self._agent_proposal(conn, agent_id, patch, p, rollback_version)

    def validate_guardrails(self, patch, p, *, layer="tenant", rollback_version=None):
        self.gateway.accounts.manage(p, platform=layer == "platform")
        target = "platform" if layer == "platform" else p.tenant_id
        with self.db.connect() as conn:
            policy = self.gateway.policy.get(tenant_overlay=False)
            before = self.layer(conn, layer, target, policy)
            if rollback_version:
                row = conn.execute("SELECT data FROM main.governance_versions WHERE id=? AND layer=? AND target=?", (rollback_version, layer, target)).fetchone()
                if not row:
                    raise DomainError("POLICY_VERSION_NOT_FOUND", "Policy version was not found", 404)
                proposed = json.loads(row[0])
            else:
                proposed = merge_policy(before["data"], patch)
            proposed = self._normalize_policy(conn, proposed, target if layer == "tenant" else None)
            if layer == "tenant":
                self._validate_bounds(proposed, [self.layer(conn, "platform", "platform", policy)], before["data"])
                known = set(self.gateway.accounts.customers(target))
                if proposed["allowed_customers"] is None or not set(proposed["allowed_customers"]) <= known:
                    raise DomainError("OUTSIDE_GUARDRAIL", "Customers must belong to this tenant", 422,
                        {"field": "allowed_customers", "limit": sorted(known), "source_layer": "tenant", "version_id": before["id"], "author": before["author"]})

    def change_guardrails(self, patch, reason, p, *, layer="tenant", rollback_version=None):
        self.gateway.accounts.manage(p, platform=layer == "platform")
        target = "platform" if layer == "platform" else p.tenant_id
        with self.db.connect(write=True) as conn:
            self.validate_guardrails(patch, p, layer=layer, rollback_version=rollback_version)
            policy = self.gateway.policy.get()
            before = self.layer(conn, layer, target, policy)
            if rollback_version:
                row = conn.execute("SELECT * FROM main.governance_versions WHERE id=? AND layer=? AND target=?", (rollback_version, layer, target)).fetchone()
                if not row:
                    raise DomainError("POLICY_VERSION_NOT_FOUND", "Policy version was not found", 404)
                proposed = json.loads(row["data"])
            else:
                proposed = merge_policy(before["data"], patch)
            proposed = self._normalize_policy(conn, proposed, target if layer == "tenant" else None)
            if layer == "tenant":
                self._validate_bounds(proposed, [self.layer(conn, "platform", "platform", policy)], before["data"])
                known = set(self.gateway.accounts.customers(target))
                if proposed["allowed_customers"] is None or not set(proposed["allowed_customers"]) <= known:
                    raise DomainError("OUTSIDE_GUARDRAIL", "Customers must belong to this tenant", 422,
                                      {"field": "allowed_customers", "limit": sorted(known), "source_layer": "tenant", "version_id": before["id"], "author": before["author"]})
            return self._write_version(conn, layer, target, target, before, proposed, p, reason,
                                       action="POLICY_ROLLED_BACK" if rollback_version else "GUARDRAILS_CHANGED")

    def requests(self, p, agent_id=None):
        self.member(p)
        with self.db.connect() as conn:
            if agent_id:
                self.target(conn, agent_id, p)
            agents = {r["agent_id"] for r in self.register(p)}
            return [json.loads(r["data"]) for r in conn.execute("SELECT * FROM governance_requests ORDER BY rowid DESC")
                    if r["agent_id"] in agents and (not agent_id or r["agent_id"] == agent_id)]

    def decide(self, request_id, decision, reason, p):
        self.gateway.accounts.manage(p)
        with self.db.connect(write=True) as conn:
            row = conn.execute("SELECT * FROM governance_requests WHERE id=?", (request_id,)).fetchone()
            if not row:
                raise DomainError("POLICY_REQUEST_NOT_FOUND", "Change request was not found", 404)
            agent = self.target(conn, row["agent_id"], p)
            if row["requester"] == (p.user_id or p.id):
                raise DomainError("SELF_APPROVAL_FORBIDDEN", "A requester cannot decide its own loosening request", 403)
            if row["status"] != "pending":
                raise DomainError("INVALID_TRANSITION", "Change request is already decided", 409)
            request = json.loads(row["data"])
            policy = self.gateway.policy.get()
            before = self.layer(conn, "agent", row["agent_id"], policy, agent)
            if decision == "approve":
                if before["id"] != row["base_version"]:
                    raise DomainError("POLICY_CHANGED", "Policy changed since this proposal; simulate and submit again", 409)
                self._normalize_policy(conn, request["policy"], agent["tenant_id"])
                self._validate_bounds(request["policy"], [self.layer(conn, "platform", "platform", policy), self.layer(conn, "tenant", agent["tenant_id"], policy)], before["data"])
                version = self._write_version(conn, "agent", row["agent_id"], agent["tenant_id"], before, request["policy"], p, request["reason"],
                    action="POLICY_ROLLED_BACK" if request.get("rollback_version") else "POLICY_CHANGED", agent_id=row["agent_id"])
                from .guardrail_profiles import record_policy_loosening
                record_policy_loosening(conn, row["agent_id"], p.user_id or p.id, agent["tenant_id"])
                request["version_id"] = version["id"]
            request.update(status="approved" if decision == "approve" else "rejected", decided_by=p.user_id or p.id,
                           decided_at=audit.utcnow().isoformat(), decision_reason=reason)
            conn.execute("UPDATE governance_requests SET status=?,data=? WHERE id=?", (request["status"], json.dumps(request), request_id))
            audit.append(conn, p.user_id or p.id, "POLICY_REQUEST_DECIDED", reason,
                         data={"tenant_id": agent["tenant_id"], "agent_id": row["agent_id"], "version_id": request.get("version_id", before["id"]), "request_id": request_id, "decision": decision})
            return request

    def versions(self, agent_id, p, *, layer="agent", version_id=None):
        with self.db.connect() as conn:
            if layer == "agent":
                agent = self.target(conn, agent_id, p)
                target = agent_id
            else:
                self.gateway.accounts.manage(p, platform=layer == "platform")
                target = "platform" if layer == "platform" else p.tenant_id
                agent = None
            current = self.layer(conn, layer, target, self.gateway.policy.get(), agent)
            versions = [dict(r) for r in conn.execute("SELECT * FROM main.governance_versions WHERE layer=? AND target=? ORDER BY created_at DESC,rowid DESC", (layer, target))]
            for v in versions:
                v["data"], v["diff"] = json.loads(v["data"]), json.loads(v["diff"])
            # The head is authoritative even when a sandbox/test clock moves
            # backwards; a timestamp sort must never select a retired policy.
            active = next((v for v in versions if v["id"] == current["id"]), current)
            versions = [active, *(v for v in versions if v["id"] != current["id"])]
            if version_id:
                found = next((v for v in versions if v["id"] == version_id), None)
                if not found:
                    raise DomainError("POLICY_VERSION_NOT_FOUND", "Policy version was not found", 404)
                return found
            return versions

    def effective(self, agent_id, p):
        with self.db.connect() as conn:
            self.target(conn, agent_id, p)
            return self.resolve(agent_id, conn=conn)

    def simulate(self, agent_id, patch, p):
        with self.db.connect() as conn:
            agent = self.target(conn, agent_id, p)
            before = self.layer(conn, "agent", agent_id, self.gateway.policy.get(), agent)
            return self._simulate(conn, agent, self._normalize_policy(conn, merge_policy(before["data"], patch), agent["tenant_id"]))

    def _simulate(self, conn, agent, proposed):
        from .models import Quote, Order
        now = audit.utcnow()
        effective = self.resolve(agent["agent_id"], conn=conn, proposed=proposed)
        counts = {"passed": 0, "needed_approval": 0, "refused": 0}
        examples, records = [], []
        for table, model in (("quotes", Quote), ("orders", Order)):
            for row in conn.execute(f"SELECT data FROM {table} WHERE json_extract(data,'$.agent_id')=? ORDER BY json_extract(data,'$.created_at'),rowid", (agent["agent_id"],)):
                obj = model.model_validate_json(row[0])
                if now - timedelta(days=30) <= obj.created_at <= now:
                    records.append((obj.created_at, table, obj))
        daily, monthly = Counter(), Counter()
        policy = self.project(agent["agent_id"], None, self.gateway.policy.get(), conn=conn)
        # Use the same evaluator for the stored evidence, and the proposed layer
        # for extra rules. No clock mutation, stock refresh or network is allowed.
        policy._governance = effective
        vals = self.values(effective)
        limits = effective.fields["per_order_ceiling_eur"].sources
        parent_ceiling = restrictive("per_order_ceiling_eur", limits[0]["value"], limits[1]["value"])
        if parent_ceiling is not None:
            policy.approval.max_auto_total_eur = parent_ceiling
        mandate = policy.agents.get(agent["agent_id"])
        if mandate:
            mandate.autonomy = vals["autonomy_level"]
            mandate.max_auto_total_eur = limits[2]["value"]
            mandate.max_auto_daily_total_eur = vals["daily_budget_eur"] if vals["daily_budget_eur"] is not None else mandate.max_auto_daily_total_eur
            mandate.customer_ids = vals["allowed_customers"] or []
        for stamp, table, obj in sorted(records, key=lambda item: (item[0], item[1])):
            day, month = stamp.date().isoformat(), stamp.strftime("%Y-%m")
            spent = {"day_eur": daily[day], "month_eur": monthly[month]}
            violations, delivery = self._simulation_constraints(conn, obj, policy, vals)
            result = self.gateway.policy.evaluate(obj.total, obj.lines, violations,
                agent_id=agent["agent_id"], customer_id=obj.customer_id, policy=policy, warnings=obj.warnings, autonomous_total=Decimal(str(daily[day])),
                governance_at=stamp, governance_spent=spent, governance_operation="quote" if table == "quotes" else "order", deliver_by=delivery)
            outcome = {"REJECTED": "refused", "PENDING_APPROVAL": "needed_approval", "CONFIRMED": "passed"}[result.outcome]
            counts[outcome] += 1
            if len(examples) < 20:
                examples.append({"kind": table[:-1], "id": obj.id, "outcome": outcome,
                                 "rules": sorted({h.rule for h in result.hits})})
            if table == "orders" and obj.status in {"CONFIRMED", "PENDING_EXECUTION"}:
                daily[day] += obj.total
                monthly[month] += obj.total
        return {**counts, "quotes": sum(t == "quotes" for _, t, _ in records), "orders": sum(t == "orders" for _, t, _ in records),
                "window_start": (now - timedelta(days=30)).isoformat(), "window_end": now.isoformat(), "examples": examples}

    def _simulation_constraints(self, conn, obj, policy, values):
        """Reapply immutable request constraints and proposed standing constraints locally."""
        from .catalog import hard_violations
        quote = obj.model_dump(mode="json")
        if "quote_id" in quote:
            row = conn.execute("SELECT data FROM quotes WHERE id=?", (quote["quote_id"],)).fetchone()
            quote = json.loads(row[0]) if row else {}
        delivery = datetime.fromisoformat(quote["deliver_by"]).date() if quote.get("deliver_by") else None
        row = conn.execute("SELECT data FROM lookups WHERE id=?", (quote.get("need_ref"),)).fetchone()
        if not row:
            return quote.get("hard_constraint_violations", []), delivery
        lookup = json.loads(row[0])
        basket = conn.execute("SELECT data FROM basket_lookups WHERE id=?", (quote["need_ref"],)).fetchone()
        line_needs = json.loads(basket[0]).get("line_needs", {}) if basket else (lookup.get("basket") or {}).get("line_needs", {})
        customer = policy.customers.get(obj.customer_id)
        violations = [v for v in quote.get("hard_constraint_violations", []) if "functional product type" in v]
        for line in obj.lines:
            need = line_needs.get(line.product_id, lookup["need"])
            claims = list(dict.fromkeys([*need.get("required_claims", []), *(customer.required_claims if customer else []), *values["required_claims"]]))
            allergens = list(dict.fromkeys([*need.get("exclude_allergens", []), *(customer.exclude_allergens if customer else []), *values["excluded_allergens"]]))
            try:
                product = self.gateway.suppliers.get(line.supplier_id).catalog.get(line.product_id)
            except DomainError:
                violations.append(line.product_id + ": product evidence unavailable")
            else:
                violations.extend(hard_violations(product, claims, allergens))
        return violations, delivery

    def register(self, p, owner_user_id=None, status=None):
        self.member(p)
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM main.tenant_agents WHERE (? OR tenant_id=?) ORDER BY agent_id", (int(p.operator), p.tenant_id)).fetchall()
            result = []
            for row in rows:
                if not p.operator and p.role != "tenant_admin" and row["owner_user_id"] != p.user_id:
                    continue
                state = "disabled" if row["disabled_at"] else row["status"]
                tenant = conn.execute("SELECT paused_at FROM main.tenants WHERE id=?", (row["tenant_id"],)).fetchone()
                if tenant and tenant[0] and state != "disabled":
                    state = "paused"
                if owner_user_id and row["owner_user_id"] != owner_user_id or status and state != status:
                    continue
                effective = self.resolve(row["agent_id"], conn=conn)
                connections = [dict(c) for c in conn.execute("SELECT connection_id,client,type,audience,revoked_at,last_used_at FROM connections WHERE agent_id=?", (row["agent_id"],))]
                orders = [json.loads(r[0]) for r in conn.execute("SELECT data FROM orders WHERE json_extract(data,'$.agent_id')=?", (row["agent_id"],))]
                entries = self._activity(conn, row["agent_id"])
                result.append({**dict(row), "status": state, "connections": connections,
                    "clients": sorted({c["client"] for c in connections}), "autonomy_level": effective.fields["autonomy_level"].value,
                    "budget_use": self.spend(conn, row["agent_id"]), "effective_policy_version_id": effective.version_id,
                    "orders": len(orders), "approvals_asked": sum(any(e["action"] == "PLACE_ORDER" and e["order_id"] == o["id"] and e["data"].get("outcome") == "PENDING_APPROVAL" for e in entries) for o in orders),
                    "approvals_granted": sum(bool(o.get("approver")) and o["status"] in {"CONFIRMED", "PENDING_EXECUTION", "CANCELLED"} for o in orders),
                    "refusals_by_code": dict(Counter(e["data"].get("error", "HUMAN_REJECTED" if e["action"] == "REJECT" else "POLICY_REJECTED") for e in entries if e["action"].endswith("_FAILED") or e["action"] == "REJECT" or e["data"].get("outcome") == "REJECTED")),
                    "last_activity": entries[0]["ts"] if entries else None})
            return result

    @staticmethod
    def _activity(conn, agent_id):
        rows = conn.execute("SELECT a.* FROM audit a WHERE a.actor=? OR json_extract(a.data,'$.agent_id')=? OR a.order_id IN (SELECT id FROM orders WHERE json_extract(data,'$.agent_id')=?) OR a.quote_id IN (SELECT id FROM quotes WHERE json_extract(data,'$.agent_id')=?) ORDER BY a.id DESC", ("agent:" + agent_id, agent_id, agent_id, agent_id))
        entries = []
        for r in rows:
            entry = dict(r)
            entry["data"] = json.loads(r["data"])
            entry["policy_version_id"] = entry["data"].get("policy_version_id") or entry["data"].get("version_id")
            entries.append(entry)
        return entries

    def activity(self, agent_id, p):
        with self.db.connect() as conn:
            self.target(conn, agent_id, p)
            return self._activity(conn, agent_id)

    def pause(self, agent_id, paused, reason, p):
        with self.db.connect(write=True) as conn:
            agent = self.target(conn, agent_id, p)
            if not paused:
                owner = conn.execute("SELECT disabled_at,removed_at FROM main.users WHERE id=?", (agent["owner_user_id"],)).fetchone()
                if not owner or owner[0] or owner[1]:
                    raise DomainError("OWNER_UNAVAILABLE", "Transfer the agent to an active member before resuming", 409)
            return self._pause(conn, agent, paused, reason, p.user_id or p.id)

    def _pause(self, conn, agent, paused, reason, actor):
        now = audit.utcnow().isoformat()
        status = "disabled" if agent["disabled_at"] else "paused" if paused else "active"
        conn.execute("UPDATE tenant_agents SET status=?,paused_by=?,paused_at=?,pause_reason=? WHERE agent_id=?",
                     (status, actor if paused else None,
                      now if paused else None, reason if paused else None, agent["agent_id"]))
        effective = self.resolve(agent["agent_id"], conn=conn)
        self.snapshot(conn, effective)
        audit.append(conn, actor, "GOVERNANCE_AGENT_PAUSED" if paused else "GOVERNANCE_AGENT_RESUMED", reason,
                     data={"tenant_id": agent["tenant_id"], "agent_id": agent["agent_id"], "policy_version_id": effective.version_id})
        return {"agent_id": agent["agent_id"], "status": status, "paused_by": actor if paused else None, "paused_at": now if paused else None}

    def pause_tenant(self, tid, paused, reason, p):
        self.gateway.accounts.manage(p, platform=p.operator)
        if not p.operator and tid != p.tenant_id:
            raise DomainError("TENANT_NOT_FOUND", "Tenant was not found", 404)
        with self.db.connect(write=True) as conn:
            if not conn.execute("SELECT 1 FROM main.tenants WHERE id=?", (tid,)).fetchone():
                raise DomainError("TENANT_NOT_FOUND", "Tenant was not found", 404)
            # Tenant stop is independent of individual pauses: resuming the tenant
            # never clears a pause made by an owner or a departing-user control.
            conn.execute("UPDATE tenants SET paused_by=?,paused_at=?,pause_reason=? WHERE id=?",
                         (p.user_id or p.id if paused else None, audit.utcnow().isoformat() if paused else None, reason if paused else None, tid))
            result = []
            for agent in conn.execute("SELECT * FROM main.tenant_agents WHERE tenant_id=?", (tid,)).fetchall():
                effective = self.resolve(agent["agent_id"], conn=conn)
                self.snapshot(conn, effective)
                audit.append(conn, p.user_id or p.id, "GOVERNANCE_TENANT_PAUSED" if paused else "GOVERNANCE_TENANT_RESUMED", reason,
                    data={"tenant_id": tid, "agent_id": agent["agent_id"], "policy_version_id": effective.version_id})
                result.append(agent["agent_id"])
            return {"tenant_id": tid, "paused": paused, "agent_ids": result}

    def user_leaving(self, conn, uid, p):
        result = []
        for agent in conn.execute("SELECT * FROM main.tenant_agents WHERE owner_user_id=?", (uid,)).fetchall():
            result.append(self._pause(conn, agent, True, "Owner disabled or removed; transfer required", p.user_id or p.id))
        return result

    def transfer(self, agent_id, owner_user_id, reason, p):
        self.gateway.accounts.manage(p)
        with self.db.connect(write=True) as conn:
            agent = self.target(conn, agent_id, p)
            owner = conn.execute("SELECT * FROM main.users WHERE id=? AND tenant_id=? AND disabled_at IS NULL AND removed_at IS NULL", (owner_user_id, agent["tenant_id"])).fetchone()
            if not owner or owner["role"] not in {"member", "approver", "tenant_admin"}:
                raise DomainError("USER_NOT_FOUND", "New owner must be an active member of this tenant", 404)
            conn.execute("UPDATE tenant_agents SET owner_user_id=? WHERE agent_id=?", (owner_user_id, agent_id))
            effective = self.resolve(agent_id, conn=conn)
            self.snapshot(conn, effective)
            audit.append(conn, p.user_id or p.id, "AGENT_TRANSFERRED", reason,
                data={"tenant_id": agent["tenant_id"], "agent_id": agent_id, "previous_owner_user_id": agent["owner_user_id"],
                      "owner_user_id": owner_user_id, "policy_version_id": effective.version_id})
            return {"agent_id": agent_id, "owner_user_id": owner_user_id, "status": agent["status"]}

    def alert(self, conn, agent_id, kind, data, *, dedup=None):
        if kind not in ALERT_TYPES:
            raise ValueError("Unknown governance alert")
        agent = conn.execute("SELECT tenant_id FROM main.tenant_agents WHERE agent_id=?", (agent_id,)).fetchone()
        if not agent:
            return
        tid, now = agent[0], audit.utcnow().isoformat()
        identifier = "alert_" + uuid4().hex
        dedup = dedup or identifier
        if conn.execute("SELECT 1 FROM main.governance_alerts WHERE dedup=?", (dedup,)).fetchone():
            return
        data = {**data, "id": identifier, "agent_id": agent_id, "tenant_id": tid, "type": kind, "created_at": now}
        conn.execute("INSERT INTO governance_alerts(id,agent_id,type,created_at,dedup,data,tenant_id) VALUES(?,?,?,?,?,?,?)",
                     (identifier, agent_id, kind, now, dedup, json.dumps(data), tid))
        audit.append(conn, "system:governance", "GOVERNANCE_ALERT", "Tenant governance alert: " + kind,
                     data={**data, "alert_id": identifier})

    def observe(self, conn, agent_id, action, data):
        """Called inside the audited command transaction; alerts share its outbox."""
        effective = self.resolve(agent_id, conn=conn)
        if data.get("error") == "OUTSIDE_ORDERING_HOURS":
            self.alert(conn, agent_id, "activity.outside_hours", {"policy_version_id": effective.version_id},
                       dedup=f"hours:{agent_id}:{audit.utcnow().isoformat()[:13]}")
        if action in {"PLACE_ORDER", "APPROVE", "SUPPLIER_ACKNOWLEDGED"}:
            spent = self.spend(conn, agent_id)
            for period, key, boundary in (("day", "daily_budget_eur", audit.utcnow().date().isoformat()),
                                          ("month", "monthly_budget_eur", audit.utcnow().strftime("%Y-%m"))):
                limit = effective.fields[key].value
                if limit is None:
                    continue
                used = spent[period + "_eur"]
                for percent in (80, 100):
                    if Decimal(str(used)) >= Decimal(str(limit)) * percent / 100:
                        self.alert(conn, agent_id, f"budget.{percent}", {"period": period, "used_eur": used, "limit_eur": limit,
                            "policy_version_id": effective.version_id}, dedup=f"budget:{agent_id}:{period}:{boundary}:{percent}:{limit}")
        if action.endswith("_FAILED") or data.get("outcome") == "REJECTED":
            cutoff = (audit.utcnow() - timedelta(minutes=10)).isoformat()
            count = conn.execute("SELECT COUNT(*) FROM audit WHERE ts>=? AND actor=? AND (action LIKE '%_FAILED' OR json_extract(data,'$.outcome')='REJECTED')", (cutoff, "agent:" + agent_id)).fetchone()[0]
            if count >= 5:
                self.alert(conn, agent_id, "refusal.burst", {"count": count, "window_minutes": 10},
                           dedup=f"refusals:{agent_id}:{int(audit.utcnow().timestamp()) // 600}")

    def connection_seen(self, agent_id, connection_id, address):
        with self.db.connect(write=True) as conn:
            row = conn.execute("SELECT last_address FROM connections WHERE connection_id=?", (connection_id,)).fetchone()
            if row and address and row[0] != address:
                self.alert(conn, agent_id, "connection.new_address", {"connection_id": connection_id, "address": address},
                           dedup=f"address:{connection_id}:{address}")
                conn.execute("UPDATE connections SET last_address=? WHERE connection_id=?", (address, connection_id))

    def alerts(self, p):
        agents = {a["agent_id"] for a in self.register(p)}
        with self.db.connect() as conn:
            return [json.loads(r["data"]) for r in conn.execute("SELECT * FROM governance_alerts ORDER BY created_at DESC,rowid DESC") if r["agent_id"] in agents]

    def report(self, p, start, end, signed=False):
        if start > end:
            raise DomainError("VALIDATION_ERROR", "from must be no later than to", 422)
        agents = self.register(p)
        result = []
        with self.db.connect() as conn:
            for agent in agents:
                history = self._activity(conn, agent["agent_id"])
                activity = [e for e in history if start <= datetime.fromisoformat(e["ts"]) <= end]
                orders = [json.loads(r[0]) for r in conn.execute("SELECT data FROM orders WHERE json_extract(data,'$.agent_id')=?", (agent["agent_id"],))]
                orders = [o for o in orders if start <= datetime.fromisoformat(o["created_at"]) <= end]
                ids = {o.get("policy_version_id") for o in orders} | {e["policy_version_id"] for e in activity}
                prior = next((e["policy_version_id"] for e in history if e["policy_version_id"] and datetime.fromisoformat(e["ts"]) <= start), None)
                if prior:
                    ids.add(prior)
                current = self.resolve(agent["agent_id"], conn=conn)
                if start <= audit.utcnow() <= end:
                    ids.add(current.version_id)
                ids.discard(None)
                versions = [json.loads(r[0]) for vid in ids if (r := conn.execute("SELECT data FROM governance_effective WHERE id=?", (vid,)).fetchone())]
                if current.version_id in ids and not any(v["version_id"] == current.version_id for v in versions):
                    versions.append(current.model_dump(mode="json"))
                changes = [e for e in activity if e["action"].startswith(("POLICY_", "GUARDRAIL", "AGENT_TRANSFER"))]
                result.append({"agent_id": agent["agent_id"], "owner_user_id": agent["owner_user_id"], "policy_version_ids": sorted(ids),
                    "policy_versions": versions, "orders": orders,
                    "spend_eur": round(sum(o["total"] for o in orders if o["status"] in {"CONFIRMED", "PENDING_EXECUTION"}), 2),
                    "approvals": [{"order_id": o["id"], "approver": o.get("approver"), "approver_user_id": o.get("approver_user_id"),
                                    "approver_role": o.get("approver_role")} for o in orders if o.get("approver")],
                    "refusals": [e for e in activity if e["action"].endswith("_FAILED") or e["action"] == "REJECT" or e["data"].get("outcome") == "REJECTED"],
                    "pauses": [e for e in activity if "PAUSED" in e["action"] or "RESUMED" in e["action"]], "changes": changes})
        payload = {"tenant_id": p.tenant_id, "from": start.isoformat(), "to": end.isoformat(), "agents": result}
        return {"report": payload, "signature": self.gateway.signing.sign(payload), "jwks_url": "/.well-known/jwks.json"} if signed else payload

    def authorize_approval(self, conn, order, p):
        if p is None or p.operator:
            return
        values = self.values(self.resolve(order.agent_id, conn=conn))
        routing = values["approval_routing"]
        threshold = routing["tenant_admin_above_eur"]
        if threshold is not None and order.total > threshold and p.role != "tenant_admin":
            raise DomainError("APPROVAL_ROUTING_REQUIRED", "A tenant administrator must approve this amount", 403)
        if routing["approver_user_ids"] is not None and p.user_id not in routing["approver_user_ids"]:
            raise DomainError("APPROVAL_ROUTING_REQUIRED", "This policy requires a designated approver", 403)
