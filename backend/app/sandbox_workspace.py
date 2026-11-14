"""Versioned deterministic supplier evidence, using the real procurement state machine."""
import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from app import audit
from app.db import DomainError
from app.models import Product
from app.runtime import scope
from app.suppliers import Adapter, SupplierCatalog, SimulatedExecution
from app.telemetry import traced, metric

SCENARIOS = Path(__file__).resolve().parents[1] / "sandbox/scenarios"


class FixtureDecisions:
    @traced("fit.classify")
    def ask(self, need, candidates, counter=None):
        if counter:
            counter.screened_count += len(candidates)
        return {p.id: {"probability": .95, "question": "Synthetic fixture suitability"} for p in candidates}

    def close(self):
        pass


class FixtureSupplierAdapter(Adapter):
    synthetic = True

    def __init__(self, workspace, supplier_id="valio"):
        self.workspace, self.id = workspace, supplier_id
        self.catalog = SupplierCatalog(workspace.gateway.db, supplier_id)
        self.execution = SimulatedExecution()
        self.ingest_catalog()

    def capabilities(self):
        return {"currencies": ["EUR"], "pricing": "fixture sale-unit", "stock": ["finite"],
                "environment": "sandbox", "scenario_version": self.workspace.scenario["version"],
                "execution": {"mode": "simulated", "atomicity": "atomic", "partial_acceptance": False}}

    def ingest_catalog(self):
        products = []
        for i, size in (("1", 1), ("2", 5), ("3", 1)):
            missing = self.workspace.scenario.get("missing_evidence", False) and i == "1"
            products.append(Product(id=i, supplier_id=self.id, supplier_sku=i, synthetic=True,
                gtin="06408430000" + i.zfill(2), name_en="Synthetic milk lactose free", sales_unit="ST", base_unit="ST",
                units={"ST": 1}, base_volume_l=size, claims=["FREE_FROM_LACTOSE"], category="10400",
                has_allergen_declaration=not missing,
                allergen_status={} if missing else {"AM": ["contains"], "AE": ["free_from"]},
                allergens_contains=[] if missing else ["AM"], free_from=[] if missing else ["AE"]))
        return self.catalog.ingest(products)

    def _fetch(self, endpoint, customer, deliver_by, ids, counter, *, fresh=False):
        if not ids:
            return {}
        setattr(counter, endpoint, getattr(counter, endpoint) + 1)
        scenario, now = self.workspace.scenario, self.workspace.now()
        if scenario.get("timeout"):
            raise DomainError("SUPPLIER_UNAVAILABLE", "Synthetic fixture supplier timeout; retry the same command", 502)
        result = {}
        for sku in ids:
            product = self.catalog.get(sku)
            common = {"_fetched_at": now.isoformat(), "_synthetic": True,
                      "_source": f"fixture:{scenario['name']}:v{scenario['version']}:{endpoint}"}
            if endpoint == "price":
                net = {"1": 1.0, "2": 6.0, "3": 1.4}[sku]
                result[sku] = {**common, "catalogPrice": {"value": net, "currency": "EUR", "valueWithTax": net * 1.135,
                    "validFrom": now.isoformat(), "validTo": (now + timedelta(hours=1)).isoformat()},
                    "comparisonPrice": {"value": net / product.base_volume_l}, "comparisonUnit": "L"}
            else:
                amount = scenario["stock"].get(sku, 0)
                if self.workspace.changed:
                    amount = scenario.get("stock_after_place", {}).get(sku, amount)
                result[sku] = {**common, "logisticModel": "Stock", "closingTime": (now + timedelta(hours=1)).isoformat(),
                    "scheduleLines": [{"availableAmount": amount, "availableDate": (deliver_by + timedelta(days=scenario.get("date_shift_days", 0))).isoformat(),
                                       "salesUnit": "ST", "salesBlock": False}]}
        return result


class Workspace:
    def __init__(self, gateway):
        self.gateway = gateway
        self.lock = threading.RLock()
        with gateway.db.connect() as conn:
            row = conn.execute("SELECT value FROM workspace WHERE key='scenario'").fetchone()
            state = conn.execute("SELECT value FROM workspace WHERE key='state'").fetchone()
        self.scenario = self.load(row[0] if row else "full_stock")
        self.state = json.loads(state[0]) if state else {"now": self.scenario["clock"], "changed": False}

    @staticmethod
    def load(name):
        if name not in {p.stem for p in SCENARIOS.glob("*.yaml")}:
            raise DomainError("UNKNOWN_SCENARIO", "Unknown sandbox scenario", 422)
        scenario = yaml.safe_load((SCENARIOS / (name + ".yaml")).read_text())
        if not scenario.get("clock"):
            raise DomainError("UNKNOWN_SCENARIO", "Scenario belongs to the local demo harness", 422)
        return scenario

    def now(self):
        return datetime.fromisoformat(self.state["now"])

    @property
    def changed(self):
        return self.state["changed"]

    def _save_state(self):
        with self.gateway.db.connect(write=True) as conn:
            conn.execute("INSERT OR REPLACE INTO workspace VALUES('state',?)", (json.dumps(self.state),))

    def advance(self, seconds):
        self.state["now"] = (self.now() + timedelta(seconds=seconds)).isoformat()
        self._save_state()

    def after(self, command, result):
        if command == "place_order" and result.status == "PENDING_APPROVAL" and self.scenario.get("stock_after_place"):
            self.state["changed"] = True
            self._save_state()
        if command == "create_quote" and self.scenario.get("advance_after_quote_seconds"):
            self.advance(self.scenario["advance_after_quote_seconds"])

    def list(self):
        scenarios = [(p.stem, yaml.safe_load(p.read_text())) for p in sorted(SCENARIOS.glob("*.yaml"))]
        return [{"scenario": name, "version": scenario["version"], "environment": "sandbox",
                 "synthetic": True, "clock": scenario["clock"]} for name, scenario in scenarios if scenario.get("clock")]

    def reset(self, name):
        scenario = self.load(name)  # Validate before invalidating anything.
        gateway = self.gateway
        with gateway.activate(), gateway.db.connect(write=True) as conn:
            for row in conn.execute("SELECT data FROM quotes WHERE status='OPEN'").fetchall():
                from app.models import Quote
                quote = Quote.model_validate_json(row[0])
                quote.status, quote.expiration_reason = "EXPIRED", "sandbox_reset"
                metric("expiries")
                gateway.orders._save_quote(conn, quote)
                audit.append(conn, "sandbox-admin", "QUOTE_EXPIRED", "sandbox_reset", quote_id=quote.id, data={"reason": "sandbox_reset", "environment": "sandbox"})
            for row in conn.execute("SELECT data FROM orders WHERE status IN ('PENDING_APPROVAL','PENDING_EXECUTION')").fetchall():
                from app.models import Order
                order = Order.model_validate_json(row[0])
                order.status, order.expiration_reason = "EXPIRED", "sandbox_reset"
                metric("expiries")
                gateway.orders._sign_receipt(order, gateway.orders._quote(conn, order.quote_id), "system")
                gateway.orders._save_order(conn, order)
                audit.append(conn, "sandbox-admin", "ORDER_EXPIRED", "sandbox_reset", order_id=order.id, quote_id=order.quote_id,
                             data={"reason": "sandbox_reset", "environment": "sandbox"})
            conn.execute("DELETE FROM reservations")
            conn.execute("DELETE FROM commands")
            # Keep audit/history, disable all previous deliveries and budgets for this rehearsal.
            conn.execute("UPDATE outbox SET status='skipped' WHERE status='pending'")
            conn.execute("UPDATE webhook_subscriptions SET data=json_set(data,'$.enabled',json('false'))")
            self.scenario, self.state = scenario, {"now": scenario["clock"], "changed": False}
            conn.execute("INSERT OR REPLACE INTO workspace VALUES('scenario',?)", (name,))
            conn.execute("INSERT OR REPLACE INTO workspace VALUES('budget_start',?)", (str(conn.execute("SELECT COALESCE(MAX(rowid),0) FROM orders").fetchone()[0]),))
            self._save_state()
            for adapter in gateway.suppliers.adapters.values():
                adapter.ingest_catalog()
            audit.append(conn, "sandbox-admin", "SANDBOX_RESET", "Reset synthetic workspace to " + name,
                         data={"scenario": name, "version": scenario["version"], "environment": "sandbox"})
        gateway.valio.cache.clear()
        gateway.limiter.buckets.clear()
        return {"environment": "sandbox", "scenario": name, "version": scenario["version"], "clock": self.state["now"]}
