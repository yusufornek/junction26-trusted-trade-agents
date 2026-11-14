"""Supplier boundary. Legacy evidence is rendered here, never by the supplier service."""
import json
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from statistics import median
from typing import Literal, Protocol, runtime_checkable

import httpx

from . import audit
from .catalog import Catalog, constraint_failures
from .db import DomainError
from .models import Product, Execution
from .runtime import SUPPLIER
from .telemetry import span, metric, traced
from .valio import RequestCounter


@dataclass
class PriceFact:
    supplier_id: str
    supplier_sku: str
    currency: str | None
    tax_basis: str
    evidence: dict


@dataclass
class StockFact:
    supplier_id: str
    supplier_sku: str
    semantics: Literal["finite", "unlimited", "unknown"]
    requested_date: date
    returned_dates: list[str]
    evidence: dict


@runtime_checkable
class SupplierAdapter(Protocol):
    def capabilities(self) -> dict: ...
    def ingest_catalog(self): ...
    def price(self, customer, date, skus, counter=None, *, fresh=False) -> dict[str, PriceFact]: ...
    def availability(self, customer, date, skus, counter=None, *, fresh=False) -> dict[str, StockFact]: ...


@runtime_checkable
class OrderExecution(Protocol):
    mode: str
    atomicity: str
    partial_acceptance: bool
    def submit(self, order) -> Execution: ...
    def reconcile(self, order) -> Execution | None: ...
    def status(self, ref) -> Execution: ...
    def cancel(self, ref) -> Execution: ...


class SimulatedExecution:
    mode, atomicity, partial_acceptance = "simulated", "atomic", False

    def submit(self, order):
        return Execution(mode=self.mode, status="simulated")

    def reconcile(self, order):
        return Execution(mode=self.mode, status="simulated")

    def status(self, ref):
        return Execution(mode=self.mode, status="simulated")

    def cancel(self, ref):
        return Execution(mode=self.mode, status="cancelled")


class Adapter:
    synthetic = False

    def fetch(self, endpoint, customer, deliver_by, ids, counter, *, fresh=False):
        before = getattr(counter, endpoint)
        started = time.perf_counter()
        token = SUPPLIER.set(self.id)
        try:
            with span("supplier." + endpoint) as current:
                facts = getattr(self, endpoint)(customer, deliver_by, ids, counter, fresh=fresh)
                if current is not None:
                    current.set_attribute("cache.hit", bool(ids and getattr(counter, endpoint) == before))
                return {sku: fact.evidence for sku, fact in facts.items()}
        finally:
            calls = getattr(counter, endpoint) - before
            metric("upstream.calls", calls)
            if calls:
                metric("upstream.latency", time.perf_counter() - started)
            if ids and not calls:
                metric("cache.hits", **{"cache.hit": True})
            SUPPLIER.reset(token)

    def price(self, customer, date, skus, counter=None, *, fresh=False):
        rows = self._fetch("price", customer, date, skus, counter or RequestCounter(), fresh=fresh)
        return {sku: PriceFact(self.id, sku, (row.get("catalogPrice") or {}).get("currency") or row.get("_currency"),
                               "net", row) for sku, row in rows.items()}

    def availability(self, customer, date, skus, counter=None, *, fresh=False):
        rows = self._fetch("availability", customer, date, skus, counter or RequestCounter(), fresh=fresh)
        return {sku: StockFact(self.id, sku, row.get("_stock_semantics") or (
            "unknown" if any((s.get("availableAmount") or 0) >= 999999 for s in row.get("scheduleLines", [])) else "finite"),
            date, [s["availableDate"] for s in row.get("scheduleLines", []) if s.get("availableDate")], row) for sku, row in rows.items()}


class ValioAdapter(Adapter):
    id = "valio"

    def __init__(self, client, catalog):
        self.client, self.catalog, self.execution = client, catalog, SimulatedExecution()

    def capabilities(self):
        return {"currencies": ["EUR"], "pricing": "sale-unit", "stock": ["finite", "unknown"],
                "execution": {"mode": "simulated", "atomicity": "atomic", "partial_acceptance": False}}

    def ingest_catalog(self, *args, **kwargs):
        from .catalog import build
        return build(self.catalog.db, *args, **kwargs)

    def _fetch(self, *args, **kwargs):
        return self.client.fetch(*args, **kwargs)


class SupplierCatalog(Catalog):
    """Small normalised catalogs with a composite supplier/SKU key; GTIN is never a key."""
    def __init__(self, db, supplier_id):
        super().__init__(db)
        self.supplier_id = supplier_id

    def ingest(self, products):
        with self.db.connect(write=True) as conn:
            conn.execute("DELETE FROM supplier_products WHERE supplier_id=?", (self.supplier_id,))
            conn.executemany("INSERT INTO supplier_products VALUES(?,?,?)",
                [(self.supplier_id, p.id, p.model_dump_json()) for p in products])
        return {"rows": len(products)}

    def rows(self):
        with self.db.connect() as conn:
            return [Product.model_validate_json(r[0]) for r in conn.execute(
                "SELECT data FROM supplier_products WHERE supplier_id=? ORDER BY supplier_sku", (self.supplier_id,))]

    def get(self, product_id):
        with self.db.connect() as conn:
            row = conn.execute("SELECT data FROM supplier_products WHERE supplier_id=? AND supplier_sku=?",
                               (self.supplier_id, product_id)).fetchone()
        if not row:
            raise DomainError("PRODUCT_NOT_FOUND", "Product is not in this supplier's catalog", 404)
        return Product.model_validate_json(row[0])

    def count(self):
        return len(self.rows())

    def metadata(self, key):
        if key == "ranking":
            rows = self.rows()
            return {key: {"min": min(values), "max": max(values), "median": median(values)}
                    for key, field in (("co2", "co2_kg_per_kg"), ("reliability", "delivery_reliability"))
                    if (values := [getattr(p, field) for p in rows if getattr(p, field) is not None])}
        if key == "vocabulary":
            rows = self.rows()
            return {"required_claims": sorted({c for p in rows for c in p.claims}),
                    "exclude_allergens": sorted({c for p in rows for c in p.allergen_status}),
                    "preferences.labels": sorted({c for p in rows for c in p.labels})}
        return {}

    @traced("catalog.search")
    def search(self, query, required_claims=None, exclude_allergens=None, limit=10, *, max_limit=10,
               removed_by=None, mode="and", counts=None, sources=None, categories=None):
        claims = self.normalize_codes(required_claims or [], "required_claims")
        allergens = self.normalize_codes(exclude_allergens or [], "exclude_allergens")
        if not 1 <= limit <= max_limit:
            raise DomainError("INVALID_LIMIT", f"limit must be between 1 and {max_limit}", 422)
        tokens = re.findall(r"[^\W_]+", query.casefold())
        matches, excluded = [], 0
        for p in self.rows():
            words = set(re.findall(r"[^\W_]+", (p.name_en + " " + p.name_fi).casefold()))
            hits = [t in words for t in tokens]
            if not hits or not (any(hits) if mode == "or" else all(hits)) or (categories is not None and p.category not in categories):
                continue
            failures = constraint_failures(p, claims, allergens)
            if failures:
                excluded += 1
                if removed_by is not None:
                    for field, code, reason in failures:
                        origins = [v.source for v in getattr(sources, field) if v.code == code] if sources else ["request"]
                        for origin in origins:
                            key = f"{field}.{code} ({origin}; {reason})"
                            removed_by[key] = removed_by.get(key, 0) + 1
            else:
                matches.append(p)
        if counts is not None:
            counts["matched_count"] = len(matches)
        return matches[:limit], excluded


class NordfoodExecution:
    mode, atomicity, partial_acceptance = "supplier_backed", "atomic", False

    def __init__(self, adapter):
        self.adapter = adapter

    def _result(self, body):
        from .lookup import timestamp
        if body.get("synthetic") is not True or body.get("state") not in {"accepted", "rejected", "cancelled"} or not body.get("reference"):
            raise DomainError("SUPPLIER_UNAVAILABLE", "Invalid synthetic Nordfood acknowledgement", 502)
        acknowledged = timestamp(body.get("received_at"))
        if acknowledged is None:
            raise DomainError("SUPPLIER_UNAVAILABLE", "Missing Nordfood acknowledgement time", 502)
        return Execution(mode=self.mode, supplier_ref=body["reference"], acknowledged_at=acknowledged, status=body["state"])

    def submit(self, order):
        body = self.adapter.request("POST", "/orders", headers={"Idempotency-Key": order.id}, json={
            "buyer": order.customer_id, "currency_code": order.currency,
            "items": [{"article": l.product_id, "cases": l.units_needed, "case_price": l.unit_price} for l in order.lines]})
        return self._result(body)

    def reconcile(self, order):
        body = self.adapter.request("GET", "/orders/by-key/" + order.id, allow_missing=True)
        return self._result(body) if body else None

    def status(self, ref):
        return self._result(self.adapter.request("GET", "/orders/" + ref))

    def cancel(self, ref):
        return self._result(self.adapter.request("POST", "/orders/" + ref + "/cancel"))


class NordfoodAdapter(Adapter):
    id, synthetic = "nordfood", True

    def __init__(self, db, base="http://127.0.0.1:8002", client=None):
        self.base = base.rstrip("/")
        self.client = client or httpx.Client(timeout=5)
        self.catalog = SupplierCatalog(db, self.id)
        self.execution = NordfoodExecution(self)
        self.ingest_catalog()

    def capabilities(self):
        return {"currencies": ["EUR", "SEK"], "pricing": "case; minimum case quantity", "stock": ["finite"],
                "execution": {"mode": self.execution.mode, "atomicity": "atomic", "partial_acceptance": False}}

    def ingest_catalog(self):
        source = Path(__file__).resolve().parents[1] / "suppliers/nordfood/catalog.json"
        self.articles = json.loads(source.read_text())
        products = [Product(id=p["article"], supplier_id=self.id, supplier_sku=p["article"], synthetic=True,
            gtin=p["barcode"], unit_gtins={"CASE": p["barcode"]}, name_en=p["description"],
            sales_unit="CASE", base_unit="ST", allowed_lot_size=p.get("order_multiple", 1), minimum_order_quantity=p["minimum_cases"],
            units={"CASE": p["bottles_per_case"]} if p.get("bottles_per_case") else {},
            base_volume_l=p.get("litres_per_bottle"), claims=p["claims"],
            has_allergen_declaration=any(status in {"contains", "may_contain", "undeclared"}
                for statuses in p.get("allergen_status", {}).values() for status in statuses),
            allergen_status=p.get("allergen_status", {}),
            allergens_contains=[code for code, statuses in p.get("allergen_status", {}).items() if "contains" in statuses],
            free_from=[code for code, statuses in p.get("allergen_status", {}).items() if "free_from" in statuses], category="10400") for p in self.articles]
        return self.catalog.ingest(products)

    def request(self, method, path, *, allow_missing=False, **kwargs):
        execution = path.startswith("/orders")
        started = time.perf_counter()
        try:
            from .limits import SUPPLIER_GUARD
            with SUPPLIER_GUARD.slot():
                response = self.client.request(method, self.base + path, **kwargs)
            if allow_missing and response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise DomainError("SUPPLIER_UNAVAILABLE", "Synthetic Nordfood service unavailable; retry the same command", 502) from None
        finally:
            if execution:
                metric("upstream.calls")
                metric("upstream.latency", time.perf_counter() - started)

    def _fetch(self, endpoint, customer, deliver_by, ids, counter, *, fresh=False):
        if not ids:
            return {}
        setattr(counter, endpoint, getattr(counter, endpoint) + 1)
        body = self.request("POST", "/case-prices" if endpoint == "price" else "/inventory", json={
            "buyer": customer, "delivery_day": deliver_by.isoformat(), "articles": ids})
        result = {}
        for row in body["items"]:
            sku = row["article"]
            common = {"_fetched_at": body["observed_at"], "_synthetic": True,
                      "_source": "synthetic:nordfood:" + endpoint, "_requested_date": deliver_by.isoformat()}
            if endpoint == "price":
                result[sku] = {**common, "catalogPrice": {"value": row["net_per_case"], "currency": row["currency_code"],
                    "validFrom": row["from"], "validTo": row["until"]}}
                if row.get("comparison_price") is not None:
                    result[sku].update(comparisonPrice={"value": row["comparison_price"]}, comparisonUnit=row.get("comparison_unit"))
            else:
                result[sku] = {**common, "_stock_semantics": "finite", "closingTime": body["cutoff"], "logisticModel": "Stock",
                    "scheduleLines": [{"availableAmount": row["cases_left"], "availableDate": row["delivery_day"],
                                       "salesUnit": "CASE", "salesBlock": False}]}
        return result


class SupplierRegistry:
    def __init__(self, valio, nordfood):
        self.adapters = {a.id: a for a in (valio, nordfood)}

    def get(self, supplier_id):
        if supplier_id not in self.adapters:
            raise DomainError("UNKNOWN_SUPPLIER", "Unknown supplier; valid values: valio, nordfood", 422)
        return self.adapters[supplier_id]

    def list(self):
        return [{"supplier_id": a.id, "synthetic": a.synthetic, "capabilities": a.capabilities()} for a in self.adapters.values()]


def reservation_key(supplier_id, sku):
    return sku if supplier_id == "valio" else supplier_id + ":" + sku
