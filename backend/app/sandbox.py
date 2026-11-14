"""Offline scenario harness using isolated temporary state and the real procurement services.

This is a local fixture harness, not the workspace/reset HTTP service from spec 26.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import secrets
from tempfile import TemporaryDirectory

import yaml

from . import audit
from .auth import Credentials
from .catalog import build
from .config import BACKEND, Settings
from .policy import PolicyLoader


class FixtureSupplierAdapter:
    atomicity = "all-or-none local simulation; fixture supplier acceptance is simulated"

    def __init__(self, scenario):
        self.rows = {p["id"]: deepcopy(p) for p in scenario["products"]}
        self.cache = {}
        self.calls = []

    def fetch(self, endpoint, customer, deliver_by, ids, counter, *, fresh=False):
        ids = list(dict.fromkeys(ids))
        if not ids:
            return {}
        self.calls.append((endpoint, ids))
        setattr(counter, endpoint, getattr(counter, endpoint) + 1)
        result = {}
        now = audit.utcnow()
        for pid in ids:
            p = self.rows[pid]
            if endpoint == "price":
                result[pid] = {"catalogPrice": {"value": p["price"], "currency": "EUR",
                    "validFrom": (now - timedelta(days=1)).isoformat(), "validTo": (now + timedelta(days=1)).isoformat()},
                    "comparisonPrice": {"value": p["price"] / p["size"]}, "comparisonUnit": p["unit"],
                    "error": False, "_fetched_at": now.isoformat(), "_source": "fixture:sandbox"}
            else:
                result[pid] = {"closingTime": (now + timedelta(hours=2)).isoformat(), "logisticModel": "Stock",
                    "scheduleLines": [{"availableAmount": p["stock"], "availableDate": deliver_by.isoformat(),
                                       "salesUnit": "ST"}], "_fetched_at": now.isoformat(), "_source": "fixture:sandbox"}
        return result

    def flush_rate_limit_audits(self, db):
        pass

    def close(self):
        pass


class FixtureDecisions:
    def __init__(self, supplier):
        self.supplier = supplier

    def ask(self, need, candidates, counter=None):
        if counter is not None:
            counter.decision_calls += bool(candidates)
            counter.screened_count += len(candidates)
        return {p.id: {"probability": self.supplier.rows[p.id].get("fit", .99),
                       "question": f"Fixture fit: {need.intent}; intended use: {need.function.intended_use if need.function else need.intent}"}
                for p in candidates}

    def close(self):
        pass


def raw_catalog_product(p):
    classifications = [{"name": "nutritionalClaim", "values": [{"id": c} for c in p.get("claims", [])]}]
    if p.get("declaration", True):
        classifications.append({"name": "allergen", "values": [{"id": "X99", "unit": "CONTAINS"}]})
    for code, status in p.get("allergens", {}).items():
        classifications.append({"name": "nonAllergen" if status == "FREE_FROM" else "allergen",
                                "values": [{"id": code, "unit": status}]})
    content = {"synkkaPackagingType": "BASE_UNIT_OR_EACH"}
    content["volume" if p["unit"] == "L" else "netWeight"] = {
        "value": p["size"] if p["unit"] == "L" else p["size"] * 1000, "unit": "LTR" if p["unit"] == "L" else "GRM"}
    return {"id": p["id"], "salesUnit": "ST", "baseUnit": "ST", "category": p["category"],
            "allowedLotSize": 1, "units": [{"unitId": "ST", "sizeInBaseUnits": 1}],
            "synkkaData": {"names": [{"language": "en", "value": p["name"]}], "brand": p.get("brand", "Fixture"),
                "classifications": classifications, "unitConversions": [content],
                **({"densityKgPerL": p["density_kg_per_l"]} if "density_kg_per_l" in p else {})}}


@contextmanager
def scenario_gateway(name, *, directory=None):
    """No live environment, catalog or backend/var files are read or changed."""
    from .service import Gateway
    scenario = yaml.safe_load((BACKEND / "sandbox/scenarios" / f"{name}.yaml").read_text())
    with TemporaryDirectory(prefix="aimo-sandbox-", dir=directory) as temporary:
        root = Path(temporary)
        defaults = yaml.load((BACKEND / "policy.yaml").read_text(), Loader=PolicyLoader)
        defaults["customers"] = scenario.get("customers", {})
        defaults["approval"].update(scenario.get("approval", {}))
        defaults["agents"]["sandbox-agent"] = {"customer_ids": list(dict.fromkeys(["hospital", "recipe", *defaults["customers"]])),
            "autonomy": "autonomous_within_limits", "max_auto_daily_total_eur": 100000}
        defaults["organisations"] = {"hospital": {"customer_ids": ["hospital"]}}
        defaults["identities"] = {"hospital-approver": {"kind": "buyer_user", "role": "buyer_approver", "organisation_id": "hospital"}}
        (root / "defaults.yaml").write_text(yaml.safe_dump(defaults))
        (root / "catalog.json").write_text(json.dumps([raw_catalog_product(p) for p in scenario["products"]]))
        (root / "reliability.csv").write_text("productId,deliveryReliability\n")
        (root / "carbon.csv").write_text("productId,AVG kg CO2-eq/kg\n")
        keys = Credentials(secrets.token_urlsafe(32), f"sandbox-agent:{secrets.token_urlsafe(32)},hospital-approver:{secrets.token_urlsafe(32)}", environment="sandbox")
        g = Gateway(Settings(db_path=root / "app.db", policy_path=root / "defaults.yaml", keys=keys))
        g.settings.environment = "sandbox"
        with g.db.connect(write=True) as conn:
            conn.execute("UPDATE workspace SET value='sandbox' WHERE key='environment'")
        g.policy.environment = "sandbox"
        build(g.db, root / "catalog.json", root / "reliability.csv", root / "carbon.csv")
        g.valio.close()
        g.decisions.close()
        g.valio = FixtureSupplierAdapter(scenario)
        g.decisions = FixtureDecisions(g.valio)
        from .suppliers import ValioAdapter
        g.suppliers.adapters["valio"] = ValioAdapter(g.valio, g.catalog)
        g.lookup.valio = g.orders.valio = g.valio
        g.lookup.decisions = g.decisions
        try:
            with g.activate():
                yield g
        finally:
            g.close()
