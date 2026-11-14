import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile

import yaml

from . import audit
from .db import DomainError
from .models import Order, PolicyDocument, PolicyResource, PolicyResult, RuleHit


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
    def _load(path, defaults=None):
        data = yaml.load(path.read_text(), Loader=PolicyLoader)
        if defaults is not None:
            data.setdefault("shortlist", {})
            data["shortlist"].setdefault("live_max", defaults.shortlist.live_max)
            data.setdefault("agents", {key: value.model_dump() for key, value in defaults.agents.items()})
        return PolicyResource.model_validate(data)

    def get(self):
        from .lookup import WARNING_TEXT

        defaults = self._load(self.path)
        resource = self._load(self.runtime_path, defaults) if self.runtime_path.exists() else defaults
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
        self.runtime_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile(mode="w", dir=self.runtime_path.parent, delete=False) as file:
                temporary = Path(file.name)
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.runtime_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def update(self, request, *, reset=False):
        # Shared SQLite writer lock serializes policy updates across REST/MCP processes.
        replaced, previous = False, None
        try:
            with self.db.connect(write=True) as conn:
                previous = self.runtime_path.read_text() if self.runtime_path.exists() else None
                before = self._editable(self.get())
                after = self._editable(self._load(self.path) if reset else request.policy)
                if not reset and "live_max" not in request.policy.shortlist.model_fields_set:
                    after["shortlist"]["live_max"] = before["shortlist"]["live_max"]
                if not reset and "agents" not in request.policy.model_fields_set:
                    after["agents"] = before["agents"]
                resource = PolicyResource(**after, updated_at=audit.utcnow(), updated_by=request.updated_by)
                audit.append(conn, f"human:{request.updated_by}", "POLICY_UPDATED",
                             "Restored default policy." if reset else "Updated procurement policy.",
                             data={"diff": self._diff(before, after), "comment": getattr(request, "comment", None),
                                   "reset": reset})
                self._replace(yaml.safe_dump(resource.model_dump(mode="json", exclude={"known_warning_codes"}), sort_keys=False))
                replaced = True
        except BaseException:
            # Restore the file if the audit transaction cannot commit.
            if replaced:
                if previous is None:
                    self.runtime_path.unlink(missing_ok=True)
                else:
                    self._replace(previous)
            raise
        return self.get()

    def check_customer(self, agent_id, customer_id, policy=None):
        mandate = (policy or self.get()).agents.get(agent_id)
        if mandate is not None and customer_id not in mandate.customer_ids:
            raise DomainError("CUSTOMER_NOT_IN_MANDATE", f"Agent {agent_id} has no mandate for customer {customer_id}", 403)

    def autonomous_total(self, conn, agent_id):
        """Read rolling spend once from the caller's SQLite snapshot/placement lock."""
        now = audit.utcnow()
        start = now - timedelta(hours=24)
        autonomous_total = Decimal(0)
        for row in conn.execute("SELECT data FROM orders WHERE status='CONFIRMED'"):
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

    def evaluate(self, total, lines, hard_violations=(), *, agent_id=None, customer_id=None,
                 autonomous_total=Decimal(0), policy=None, warnings=()):
        policy = policy or self.get()
        max_total = policy.approval.max_auto_total_eur
        source = "global policy"
        mandate = policy.agents.get(agent_id)
        if mandate is not None and mandate.max_auto_total_eur is not None and mandate.max_auto_total_eur <= max_total:
            max_total = mandate.max_auto_total_eur
            source = f"mandate for {agent_id}"
        min_fit = policy.approval.min_fit_probability
        approval_codes, reject_codes = set(policy.approval.warnings_need_approval), set(policy.reject.on)
        hits, rejected, approval = [], False, False
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
            rejected |= policy.reject.on_hard_constraint_violation
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
                if warning.code in reject_codes or warning.code in approval_codes:
                    hits.append(RuleHit(rule=warning.code, detail=f"{product_id}: {warning.text}"))
                    rejected |= warning.code in reject_codes
                    approval |= warning.code in approval_codes
        return PolicyResult(outcome="REJECTED" if rejected else "PENDING_APPROVAL" if approval else "CONFIRMED", hits=hits)
