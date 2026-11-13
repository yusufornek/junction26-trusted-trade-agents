import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import yaml

from . import audit
from .models import PolicyDocument, PolicyResource, PolicyResult, RuleHit


class Policy:
    def __init__(self, path, runtime_path=None, db=None):
        self.path = Path(path)
        self.runtime_path = Path(runtime_path) if runtime_path is not None else self.path.parent / "var/policy.yaml"
        self.db = db
        self.get()  # Validate configuration at startup.

    @staticmethod
    def _load(path):
        # BaseLoader keeps YAML's 'on' key a string (safe_load interprets it as boolean).
        return PolicyResource.model_validate(yaml.load(path.read_text(), Loader=yaml.BaseLoader))

    def get(self):
        from .lookup import WARNING_TEXT

        resource = self._load(self.runtime_path if self.runtime_path.exists() else self.path)
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
        for key, value in after.items():
            field = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                changes.update(Policy._diff(before[key], value, field))
            elif value != before[key]:
                changes[field] = {"before": before[key], "after": value}
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

    def evaluate(self, total, lines, hard_violations=()):
        policy = self.get()
        max_total = policy.approval.max_auto_total_eur
        min_fit = policy.approval.min_fit_probability
        approval_codes, reject_codes = set(policy.approval.warnings_need_approval), set(policy.reject.on)
        hits, rejected, approval = [], False, False
        for violation in hard_violations:
            hits.append(RuleHit(rule="hard_constraint_violation", detail=violation))
            rejected |= policy.reject.on_hard_constraint_violation
        if total > max_total:
            hits.append(RuleHit(rule="max_auto_total_eur", detail=f"Net total {total:.2f} exceeds {max_total:.2f} EUR"))
            approval = True
        for line in lines:
            if line.fit is not None and line.fit < min_fit:
                hits.append(RuleHit(rule="min_fit_probability", detail=f"{line.product_id}: {line.fit:.3f} < {min_fit:.3f}"))
                approval = True
            for warning in line.warnings:
                if warning.code in reject_codes or warning.code in approval_codes:
                    hits.append(RuleHit(rule=warning.code, detail=f"{line.product_id}: {warning.text}"))
                    rejected |= warning.code in reject_codes
                    approval |= warning.code in approval_codes
        return PolicyResult(outcome="REJECTED" if rejected else "PENDING_APPROVAL" if approval else "CONFIRMED", hits=hits)
