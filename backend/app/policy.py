from .telemetry import traced
import copy
import os

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import yaml

from . import audit
from .db import DomainError
from .models import ConstraintSource, EffectiveConstraints, Order, PolicyDocument, PolicyResource, PolicyResult, RuleHit


class PolicyLoader(yaml.SafeLoader):
    # Preserve the key 'on' while retaining safe YAML null/list/number handling.
    yaml_implicit_resolvers = {
        key: [(tag, regex) for tag, regex in resolvers if tag != "tag:yaml.org,2002:bool"]
        for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }


_PARSED = {}


def _parse(text):
    """YAML parsing dominated governance and policy reads (several parses per request); parse each text once."""
    parsed = _PARSED.get(text)
    if parsed is None:
        parsed = yaml.load(text, Loader=PolicyLoader)
        if len(_PARSED) > 64:
            _PARSED.clear()
        _PARSED[text] = parsed
    return copy.deepcopy(parsed)  # callers fill in defaults; the cached tree stays untouched


class Policy:
    def __init__(self, path, runtime_path=None, db=None):
        self.path = Path(path)
        self.runtime_path = Path(runtime_path) if runtime_path is not None else self.path.parent / "var/policy.yaml"
        self.db = db
        self.get()  # Validate configuration at startup.

    @staticmethod
    def _load(path, defaults=None, *, content=None):
        data = _parse(path.read_text() if content is None else content)
        if defaults is not None:
            data.setdefault("shortlist", {})
            data["shortlist"].setdefault("live_max", defaults.shortlist.live_max)
            data.setdefault("agents", {key: value.model_dump() for key, value in defaults.agents.items()})
            data.setdefault("customers", {})
            data.setdefault("autonomy", defaults.autonomy.model_dump())
            data.setdefault("anomalies", defaults.anomalies.model_dump())
            # ignored_warnings was introduced with the evidence defaults. Its absence identifies
            # a legacy document; explicitly saved current policies must keep their operator choices.
            if "ignored_warnings" not in data["approval"]:
                for code in ("SIZE_UNCONFIRMED", "UNIT_MISMATCH", "STOCK_MODEL_UNKNOWN", "CUTOFF_UNKNOWN"):
                    if code not in data["approval"]["warnings_need_approval"]:
                        data["approval"]["warnings_need_approval"].append(code)
                if "NO_STOCK" not in data["reject"]["on"]:
                    data["reject"]["on"].append("NO_STOCK")
            data["approval"].setdefault("ignored_warnings", defaults.approval.ignored_warnings)
        return PolicyResource.model_validate(data)

    def get(self, *, tenant_overlay=True):
        from .lookup import WARNING_TEXT

        defaults = self._load(self.path)
        content = self.db.file_content(self.runtime_path) if self.db is not None else None
        resource = self._load(self.runtime_path, defaults, content=content) if content is not None or self.runtime_path.exists() else defaults
        resource.known_warning_codes = sorted(WARNING_TEXT)
        if self.db:
            # Customer standing constraints bind even platform-side approvals.
            # These values are a projection, never a rewrite of global policy.
            import json
            from .models import CustomerConstraints
            with self.db.connect() as conn:
                profiles = conn.execute("SELECT tenant_id,data FROM tenant_guardrail_profiles").fetchall()
                customers = conn.execute("SELECT customer_id,tenant_id FROM tenant_customers").fetchall()
            for row in profiles:
                snapshot = json.loads(row["data"])
                resource.tenant_anomaly_settings[row["tenant_id"]] = snapshot["guardrails"]["anomaly_sensitivity"]["value"]
                for customer in customers:
                    if not tenant_overlay or customer["tenant_id"] != row["tenant_id"]:
                        continue
                    constraints = resource.customers.setdefault(customer["customer_id"], CustomerConstraints())
                    for field in ("required_claims", "exclude_allergens"):
                        setattr(constraints, field, sorted(set(getattr(constraints, field)) | set(snapshot["profile"][field])))
        if tenant_overlay and self.db:
            from .tenancy import principal, TARGET_TENANT
            p = principal()
            tid = TARGET_TENANT.get() or (p.tenant_id if p and not p.operator else None)
            if tid:
                import json
                with self.db.connect() as conn:
                    overlay = conn.execute("SELECT data FROM tenant_policy_overlays WHERE id=?", (tid,)).fetchone()
                if overlay:
                    data = json.loads(overlay[0])
                    daily_ceiling = data.get("approval", {}).get("max_auto_daily_total_eur")
                    resource._tenant_daily_ceiling = daily_ceiling
                    if daily_ceiling is not None:
                        with self.db.connect() as conn:
                            customers = {r[0] for r in conn.execute("SELECT customer_id FROM tenant_customers WHERE tenant_id=?", (tid,))}
                        for mandate in resource.agents.values():
                            if customers.intersection(mandate.customer_ids):
                                mandate.max_auto_daily_total_eur = min(mandate.max_auto_daily_total_eur, daily_ceiling)
                    for key, value in data.get("approval", {}).items():
                        current = getattr(resource.approval, key)
                        setattr(resource.approval, key, sorted(set(current) | set(value)) if isinstance(current, list) else
                                max(current, value) if key.startswith("min_") else min(current, value))
                    resource.reject.on = sorted(set(resource.reject.on) | set(data.get("reject", {}).get("on", [])))
                    resource.reject.on_hard_constraint_violation |= data.get("reject", {}).get("on_hard_constraint_violation", False)
        return resource

    @property
    def weights(self):
        return self.get().ranking_weights.model_dump()

    @property
    def catalog_pool(self):
        return self.get().shortlist.catalog_pool

    @staticmethod
    def _editable(resource):
        return PolicyDocument.model_validate(resource.model_dump(include=set(PolicyDocument.model_fields))).model_dump(mode="json")

    @staticmethod
    def _diff(before, after, prefix=""):
        changes = {}
        for key in sorted(before.keys() | after.keys()):
            old, value = before.get(key), after.get(key)
            field = f"{prefix}.{key}" if prefix else key
            if isinstance(old, dict) and isinstance(value, dict):
                changes.update(Policy._diff(old, value, field))
            elif value != old:
                changes[field] = {"before": old, "after": value}
        return changes

    def _replace(self, content):
        self.db.stage_file(self.runtime_path, content)

    def update(self, request, *, reset=False, actor=None):
        return self._update(request, reset=reset, actor=actor)

    def change_autonomy(self, prepare, principal):
        result = []

        def request_factory():
            request, data, response = prepare()
            result.append(response)
            return request, data

        self._update(request_factory, actor="human:operator" if principal.operator else f"human:{principal.id}",
                     audit_action="AUTONOMY_CHANGED")
        return result[0]

    def _update(self, request, *, reset=False, actor=None, audit_action="POLICY_UPDATED", audit_data=None):
        # File staging shares the outer command transaction, including its result insert.
        with self.db.connect(write=True) as conn:
            if callable(request):
                request, audit_data = request()
                if request is None:
                    # VC-only agents retain their signed authority; changing a level must
                    # never mint a policy mandate that bypasses VC expiry or revocation.
                    audit.append(conn, actor, audit_action, "Changed verifiable-mandate agent autonomy.", data=audit_data)
                    return self.get()
            before = self._editable(self.get(tenant_overlay=False))
            governed_before = None
            if hasattr(self, "governance"):
                agent_id = (audit_data or {}).get("agent_id") if audit_action == "AUTONOMY_CHANGED" else None
                layer, target = ("agent", agent_id) if agent_id else ("platform", "platform")
                agent = conn.execute("SELECT * FROM tenant_agents WHERE agent_id=?", (agent_id,)).fetchone() if agent_id else None
                governed_before = self.governance.layer(conn, layer, target, self.get(tenant_overlay=False), agent)
            after = self._editable(self._load(self.path) if reset else request.policy)
            if not reset and "live_max" not in request.policy.shortlist.model_fields_set:
                after["shortlist"]["live_max"] = before["shortlist"]["live_max"]
            for section in ("agents", "customers", "organisations", "identities", "rate_limits", "autonomy", "proposal_quotas", "anomalies"):
                if not reset and section not in request.policy.model_fields_set:
                    after[section] = before[section]
            from .catalog import Catalog
            catalog = Catalog(self.db)
            for customer_id, customer in after["customers"].items():
                for field in ("required_claims", "exclude_allergens"):
                    customer[field] = catalog.normalize_codes(customer[field], field, location=f"customers.{customer_id}.{field}")
            resource = PolicyResource(**after, updated_at=audit.utcnow(), updated_by=request.updated_by)
            self._replace(yaml.safe_dump(resource.model_dump(mode="json", exclude={"known_warning_codes", "tenant_anomaly_settings"}), sort_keys=False))
            governed_version = None
            if governed_before:
                from .auth import Principal
                from .foundation import PRINCIPAL
                principal = PRINCIPAL.get() or Principal(request.updated_by, operator=True, kind="platform_admin", user_id=request.updated_by, role="platform_admin")
                proposed = dict(governed_before["data"])
                if agent_id:
                    proposed["autonomy_level"] = audit_data["level"]
                    tid = agent["tenant_id"] if agent else self.governance.db_tenant(conn, agent_id)
                else:
                    proposed["per_order_ceiling_eur"] = resource.approval.max_auto_total_eur
                    tid = "platform"
                governed_version = self.governance._write_version(conn, layer, target, tid, governed_before, proposed, principal,
                    getattr(request, "comment", None) or (audit_data or {}).get("reason") or "Legacy policy update",
                    action="POLICY_CHANGED" if agent_id else "GUARDRAILS_CHANGED", agent_id=agent_id, emit_audit=False)
                # The legacy operator policy editor remains authoritative for its
                # explicitly changed agent fields. Do not let a historical
                # governance snapshot restore a revoked customer or autonomy.
                if not agent_id:
                    for aid, mandate in after["agents"].items():
                        old = before["agents"].get(aid, {})
                        if not conn.execute("SELECT 1 FROM governance_heads WHERE layer='agent' AND target=?", (aid,)).fetchone():
                            continue
                        agent_row = conn.execute("SELECT * FROM tenant_agents WHERE agent_id=?", (aid,)).fetchone()
                        current = self.governance.layer(conn, "agent", aid, resource, agent_row)
                        updated = dict(current["data"])
                        for legacy, field in (("customer_ids", "allowed_customers"), ("max_auto_total_eur", "per_order_ceiling_eur"), ("autonomy", "autonomy_level")):
                            if old.get(legacy) != mandate.get(legacy):
                                updated[field] = mandate.get(legacy) if legacy != "autonomy" else mandate.get(legacy) or resource.autonomy.default
                        if updated != current["data"]:
                            self.governance._write_version(conn, "agent", aid, agent_row["tenant_id"] if agent_row else self.governance.db_tenant(conn, aid),
                                current, updated, principal, getattr(request, "comment", None) or "Operator changed legacy agent authority",
                                agent_id=aid, emit_audit=False)
            audit.append(conn, actor or f"human:{request.updated_by}", audit_action,
                         "Restored default policy." if reset else "Updated procurement policy.",
                         data={"diff": self._diff(before, after), "comment": getattr(request, "comment", None),
                               "reset": reset, "stated_name": request.updated_by, **(audit_data or {}),
                               **({"version_id": governed_version["id"]} if governed_version else {})})

        return self.get()

    def effective_need(self, need, policy=None):
        """Keep the stored request intact; recompute the current standing union at every step."""
        from .catalog import Catalog
        catalog = self.suppliers.get(need.supplier_id).catalog if hasattr(self, "suppliers") else Catalog(self.db)
        policy = policy or self.get()
        customer = policy.customers.get(need.customer_id)
        from .brief_scenarios import NUTRIENTS
        effective, updates = EffectiveConstraints(nutrient_limits={key: getattr(customer, key) for key in NUTRIENTS if customer and getattr(customer, key) is not None}), {}
        for field in ("required_claims", "exclude_allergens"):
            entries = []
            for source, values in (("request", getattr(need, field)),
                                   ("customer_policy", getattr(customer, field) if customer else [])):
                for code in catalog.normalize_codes(values, field):
                    entries.append(ConstraintSource(code=code, source=source))
            setattr(effective, field, entries)
            updates[field] = list(dict.fromkeys(entry.code for entry in entries))
        preferences = need.preferences.model_copy(update={"labels": catalog.normalize_codes(need.preferences.labels, "preferences.labels")})
        updates["preferences"] = preferences
        return need.model_copy(update=updates), effective

    def check_customer(self, agent_id, customer_id, policy=None):
        from .auth import check_customer_scope
        check_customer_scope(agent_id, customer_id)
        mandate = (policy or self.get()).agents.get(agent_id)
        if mandate is None:
            raise DomainError("AGENT_NOT_REGISTERED", f"Agent {agent_id} has no mandate; procurement requires a mandate", 403)
        from .foundation import PRINCIPAL
        principal = PRINCIPAL.get()
        if principal and not principal.operator and principal.tenant_id and customer_id not in principal.customer_ids:
            raise DomainError("CUSTOMER_NOT_IN_MANDATE", "Customer is outside the credential's allowed customers", 403)
        if mandate is not None and customer_id not in mandate.customer_ids:
            raise DomainError("CUSTOMER_NOT_IN_MANDATE", f"Agent {agent_id} has no mandate for customer {customer_id}", 403)

    def check_supplier(self, agent_id, supplier_id, policy=None):
        row = (policy or self.get()).agents.get(agent_id)
        if row and row.supplier_ids is not None and supplier_id not in row.supplier_ids:
            raise DomainError("SUPPLIER_NOT_IN_MANDATE", "The agent's authority does not include this supplier", 403)

    def autonomous_total(self, conn, agent_id):
        """Read rolling spend once from the caller's SQLite snapshot/placement lock."""
        now = audit.utcnow()
        start = now - timedelta(hours=24)
        autonomous_total = Decimal(0)
        epoch = conn.execute("SELECT value FROM workspace WHERE key='budget_start'").fetchone()
        for row in conn.execute("SELECT data FROM orders WHERE status IN ('CONFIRMED','PENDING_EXECUTION') AND rowid>?", (int(epoch[0]) if epoch else 0,)):
            order = Order.model_validate_json(row["data"])
            if (order.agent_id == agent_id and order.approver is None and order.currency == "EUR"
                    and start <= order.created_at <= now):
                autonomous_total += Decimal(str(order.total))
        return autonomous_total

    def evaluate_order(self, conn, total, lines, hard_violations=(), *, agent_id, customer_id=None,
                       policy=None, warnings=()):
        """Shared read-only placement/preview evaluation, under the caller's SQLite snapshot/lock."""
        policy = policy or self.get()
        if hasattr(self, "governance") and policy._governance is None:
            policy = self.governance.project(agent_id, customer_id, policy, conn=conn)
        return self.evaluate(total, lines, hard_violations, agent_id=agent_id,
                             customer_id=customer_id, autonomous_total=self.autonomous_total(conn, agent_id),
                             policy=policy, warnings=warnings)

    @traced("policy.evaluate")
    def evaluate(self, total, lines, hard_violations=(), *, agent_id=None, customer_id=None,
                 autonomous_total=Decimal(0), policy=None, warnings=(), governance_spent=None,
                 governance_at=None, governance_operation="order", deliver_by=None):
        policy = policy or self.get()
        for line in lines:
            self.check_supplier(agent_id, line.supplier_id, policy)
            if line.currency != "EUR":
                raise DomainError("UNSUPPORTED_CURRENCY", "Policy supports EUR only; no FX rate source is configured", 422)
        max_total = policy.approval.max_auto_total_eur
        source = "global policy"
        mandate = policy.agents.get(agent_id)
        if mandate is not None and mandate.max_auto_total_eur is not None and mandate.max_auto_total_eur <= max_total:
            max_total = mandate.max_auto_total_eur
            source = f"mandate for {agent_id}"
        min_fit = policy.approval.min_fit_probability
        approval_codes, reject_codes = set(policy.approval.warnings_need_approval), set(policy.reject.on)
        ignored_codes = set(policy.approval.ignored_warnings)
        hits, rejected, approval = [], False, False
        if self.db and (trust := getattr(self.db, "connection_trust", None)) and trust.approval_required(agent_id):
            hits.append(RuleHit(rule="connection_continuity", detail="This connection needs owner confirmation or verified client identity before earned autonomy applies."))
            approval = True
        customer = policy.customers.get(customer_id)
        from .substitution import apply_profile
        for line in lines:
            apply_profile(line, policy, customer_id)
            if line.substitution is not None:
                hits.append(RuleHit(rule="SUBSTITUTION_REQUIRED", detail=f"{line.product_id}: substitutes require human approval"))
                approval = True
        if customer and customer.clinical_review == "required":
            hits.append(RuleHit(rule="CLINICAL_REVIEW", detail="Clinical review by a buyer approver is required."))
            approval = True
        if mandate is not None and (mandate.autonomy or policy.autonomy.default) == "approval_required":
            hits.append(RuleHit(rule="autonomy_level", detail="This agent's orders need sign-off until autonomy is granted."))
            approval = True
        if mandate is not None and customer_id is not None and customer_id not in mandate.customer_ids:
            hits.append(RuleHit(rule="CUSTOMER_NOT_IN_MANDATE",
                                detail=f"Agent {agent_id} has no mandate for customer {customer_id}"))
            rejected = True
        if agent_id is not None and mandate is None:
            hits.append(RuleHit(rule="unregistered_agent", detail=f"Agent {agent_id} has no mandate; ordering requires a mandate."))
            rejected = True
        if mandate is not None and autonomous_total + Decimal(str(total)) > Decimal(str(mandate.max_auto_daily_total_eur)):
            hits.append(RuleHit(rule="max_auto_daily_total_eur", detail=(
                f"Agent {agent_id}: {autonomous_total:.2f} + {total:.2f} exceeds "
                f"{mandate.max_auto_daily_total_eur:.2f} EUR in the rolling 24-hour autonomous window.")))
            approval = True
        for violation in hard_violations:
            hits.append(RuleHit(rule="hard_constraint_violation", detail=violation))
            binding = policy.reject.on_hard_constraint_violation or violation.endswith("(customer_policy)")
            rejected |= binding
            approval |= not binding
        if total > max_total:
            hits.append(RuleHit(rule="max_auto_total_eur", detail=f"Net total {total:.2f} exceeds {max_total:.2f} EUR ({source})"))
            approval = True
        warning_groups = [("plan", warnings)]
        for line in lines:
            if line.fit is not None and line.fit < min_fit:
                hits.append(RuleHit(rule="min_fit_probability", detail=f"{line.product_id}: {line.fit:.3f} < {min_fit:.3f}"))
                approval = True
            warning_groups.append((line.product_id, line.warnings))
        for product_id, line_warnings in warning_groups:
            # Stock refresh changes warning insertion order; equivalent evidence must yield identical hits.
            for warning in sorted(line_warnings, key=lambda w: (w.code, w.text)):
                if warning.code in reject_codes or warning.code in approval_codes or warning.code not in ignored_codes:
                    hits.append(RuleHit(rule=warning.code, detail=f"{product_id}: {warning.text}"))
                    rejected |= warning.code in reject_codes
                    approval |= warning.code not in reject_codes
        result = PolicyResult(outcome="REJECTED" if rejected else "PENDING_APPROVAL" if approval else "CONFIRMED", hits=hits)
        if hasattr(self, "governance") and policy._governance is not None:
            with self.db.connect() as conn:
                extra = self.governance.evaluate(conn, policy._governance, total, lines, spent=governance_spent,
                    at=governance_at, operation=governance_operation, deliver_by=deliver_by)
            outcomes = {result.outcome, extra.outcome}
            result.outcome = "REJECTED" if "REJECTED" in outcomes else "PENDING_APPROVAL" if "PENDING_APPROVAL" in outcomes else "CONFIRMED"
            known = {h.rule for h in result.hits}
            # Preserve established mandate rule identifiers for the same limit.
            aliases = {"per_order_ceiling_eur": "max_auto_total_eur", "daily_budget_eur": "max_auto_daily_total_eur"}
            result.hits.extend(h for h in extra.hits if h.rule not in known and aliases.get(h.rule) not in known)
        return result
