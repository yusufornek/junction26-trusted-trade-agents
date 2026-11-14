from .telemetry import traced
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


class Policy:
    def __init__(self, path, runtime_path=None, db=None):
        self.path = Path(path)
        self.runtime_path = Path(runtime_path) if runtime_path is not None else self.path.parent / "var/policy.yaml"
        self.db = db
        self.get()  # Validate configuration at startup.

    @staticmethod
    def _load(path, defaults=None, *, content=None):
        data = yaml.load(path.read_text() if content is None else content, Loader=PolicyLoader)
        if defaults is not None:
            data.setdefault("shortlist", {})
            data["shortlist"].setdefault("live_max", defaults.shortlist.live_max)
            data.setdefault("agents", {key: value.model_dump() for key, value in defaults.agents.items()})
            data.setdefault("customers", {})
            data.setdefault("autonomy", defaults.autonomy.model_dump())
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

    def get(self):
        from .lookup import WARNING_TEXT

        defaults = self._load(self.path)
        content = self.db.file_content(self.runtime_path) if self.db is not None else None
        resource = self._load(self.runtime_path, defaults, content=content) if content is not None or self.runtime_path.exists() else defaults
        resource.known_warning_codes = sorted(WARNING_TEXT)
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
            before = self._editable(self.get())
            after = self._editable(self._load(self.path) if reset else request.policy)
            if not reset and "live_max" not in request.policy.shortlist.model_fields_set:
                after["shortlist"]["live_max"] = before["shortlist"]["live_max"]
            for section in ("agents", "customers", "organisations", "identities", "rate_limits", "autonomy", "proposal_quotas"):
                if not reset and section not in request.policy.model_fields_set:
                    after[section] = before[section]
            from .catalog import Catalog
            catalog = Catalog(self.db)
            for customer_id, customer in after["customers"].items():
                for field in ("required_claims", "exclude_allergens"):
                    customer[field] = catalog.normalize_codes(customer[field], field, location=f"customers.{customer_id}.{field}")
            resource = PolicyResource(**after, updated_at=audit.utcnow(), updated_by=request.updated_by)
            audit.append(conn, actor or f"human:{request.updated_by}", audit_action,
                         "Restored default policy." if reset else "Updated procurement policy.",
                         data={"diff": self._diff(before, after), "comment": getattr(request, "comment", None),
                               "reset": reset, "stated_name": request.updated_by, **(audit_data or {})})
            self._replace(yaml.safe_dump(resource.model_dump(mode="json", exclude={"known_warning_codes"}), sort_keys=False))

        return self.get()

    def effective_need(self, need, policy=None):
        """Keep the stored request intact; recompute the current standing union at every step."""
        from .catalog import Catalog
        catalog = self.suppliers.get(need.supplier_id).catalog if hasattr(self, "suppliers") else Catalog(self.db)
        policy = policy or self.get()
        customer = policy.customers.get(need.customer_id)
        effective, updates = EffectiveConstraints(), {}
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
        return self.evaluate(total, lines, hard_violations, agent_id=agent_id,
                             customer_id=customer_id, autonomous_total=self.autonomous_total(conn, agent_id),
                             policy=policy, warnings=warnings)

    @traced("policy.evaluate")
    def evaluate(self, total, lines, hard_violations=(), *, agent_id=None, customer_id=None,
                 autonomous_total=Decimal(0), policy=None, warnings=()):
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
        customer = policy.customers.get(customer_id)
        from .substitution import apply_profile
        for line in lines:
            apply_profile(line, policy, customer_id)
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
        return PolicyResult(outcome="REJECTED" if rejected else "PENDING_APPROVAL" if approval else "CONFIRMED", hits=hits)
