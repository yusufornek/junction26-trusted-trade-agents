import yaml

from .models import PolicyResult, RuleHit


class Policy:
    def __init__(self, path):
        # BaseLoader keeps YAML's 'on' key a string (safe_load interprets it as boolean).
        raw = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.max_total = float(raw["approval"]["max_auto_total_eur"])
        self.min_fit = float(raw["approval"]["min_fit_probability"])
        self.approval_codes = set(raw["approval"]["warnings_need_approval"])
        self.reject_codes = set(raw["reject"]["on"])
        self.reject_hard = raw["reject"]["on_hard_constraint_violation"].lower() == "true"
        self.weights = {k: float(v) for k, v in raw["ranking_weights"].items()}

    def evaluate(self, total, lines, hard_violations=()):
        hits, rejected, approval = [], False, False
        for violation in hard_violations:
            hits.append(RuleHit(rule="hard_constraint_violation", detail=violation))
            rejected |= self.reject_hard
        if total > self.max_total:
            hits.append(RuleHit(rule="max_auto_total_eur", detail=f"Net total {total:.2f} exceeds {self.max_total:.2f} EUR"))
            approval = True
        for line in lines:
            if line.fit is not None and line.fit < self.min_fit:
                hits.append(RuleHit(rule="min_fit_probability", detail=f"{line.product_id}: {line.fit:.3f} < {self.min_fit:.3f}"))
                approval = True
            for warning in line.warnings:
                if warning.code in self.reject_codes or warning.code in self.approval_codes:
                    hits.append(RuleHit(rule=warning.code, detail=f"{line.product_id}: {warning.text}"))
                    rejected |= warning.code in self.reject_codes
                    approval |= warning.code in self.approval_codes
        return PolicyResult(outcome="REJECTED" if rejected else "PENDING_APPROVAL" if approval else "CONFIRMED", hits=hits)
