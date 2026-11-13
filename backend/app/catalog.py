import argparse
import csv
import gzip
import json
import math
import re
import time
from pathlib import Path

from .config import BACKEND, ROOT
from .db import Database, DomainError
from .models import Product


def localized(items, language):
    return " ".join(str(v.get("value", "")) for v in (items or []) if v.get("language") == language)


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def csv_values(path, column):
    if path is None or not Path(path).is_file():
        return {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        return {str(row["productId"]): number(row.get(column)) for row in csv.DictReader(f)}


def extract(raw, reliability, carbon):
    synkka = raw.get("synkkaData") or {}
    grouped = {c["name"]: c.get("values") or [] for c in synkka.get("classifications", [])}

    def ids(name, unit=None):
        return sorted({str(v["id"]) for v in grouped.get(name, [])
                       if v.get("id") and (unit is None or v.get("unit") == unit)})

    base = next((c for c in synkka.get("unitConversions", [])
                 if c.get("synkkaPackagingType") == "BASE_UNIT_OR_EACH"), {})
    vol, weight = base.get("volume") or {}, base.get("netWeight") or {}
    weight_value = number(weight.get("value"))
    weight_kg = (weight_value * {"KGM": 1, "GRM": 0.001}.get(weight.get("unit"), 0)
                 if weight_value is not None else None)
    return Product(
        id=str(raw["id"]), name_en=localized(synkka.get("names"), "en") or localized(synkka.get("names"), "fi"),
        name_fi=localized(synkka.get("names"), "fi"), category=str(raw["category"]) if raw.get("category") else None,
        sales_unit=raw.get("salesUnit") or "ST", base_unit=raw.get("baseUnit") or "ST",
        allowed_lot_size=number(raw.get("allowedLotSize")) or 1,
        vendor_name=raw.get("vendorName"), brand=synkka.get("brand"), country=raw.get("countryOfOrigin"),
        temperature_condition=raw.get("temperatureCondition"),
        units={u["unitId"]: n for u in (raw.get("units") or [])
               if (n := number(u.get("sizeInBaseUnits"))) is not None},
        claims=ids("nutritionalClaim"), allergens_contains=ids("allergen", "CONTAINS"),
        free_from=ids("nonAllergen"), labels=ids("packagingMarkedLabel"),
        base_volume_l=number(vol.get("value")) if vol.get("unit") == "LTR" else None,
        base_net_weight_kg=weight_kg or None, delivery_reliability=reliability.get(str(raw["id"])),
        co2_kg_per_kg=carbon.get(str(raw["id"])),
    )


def build(db, source=ROOT / "data/product_data.json.gz", reliability_path=ROOT / "data/delivery_reliability.csv",
          carbon_path=ROOT / "data/carbon_footprint.csv"):
    start = time.perf_counter()
    reliability = csv_values(reliability_path, "deliveryReliability")
    carbon = csv_values(carbon_path, "AVG kg CO2-eq/kg")
    opener = gzip.open if str(source).endswith(".gz") else open
    with opener(source, "rt", encoding="utf-8") as f:
        raw_products = json.load(f)
    with db.connect(write=True) as conn:
        conn.execute("DELETE FROM products")
        conn.execute("DELETE FROM products_fts")
        count = 0
        for raw in raw_products:
            product = extract(raw, reliability, carbon)
            values = product.model_dump()
            columns = list(values)
            row = [json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for v in values.values()]
            conn.execute(f"INSERT INTO products({','.join(columns)},data) VALUES({','.join('?' for _ in range(len(row)+1))})",
                         [*row, product.model_dump_json()])
            synkka = raw.get("synkkaData") or {}
            conn.execute("INSERT INTO products_fts VALUES(?,?,?,?,?)", (
                product.id, product.name_en, product.name_fi,
                localized(synkka.get("keyIngredients"), "en"), localized(synkka.get("marketingTexts"), "en")))
            count += 1
    return {"rows": count, "seconds": round(time.perf_counter() - start, 3)}


def hard_violations(product, required_claims, exclude_allergens):
    return ([f"{product.id}: missing required claim {c}" for c in required_claims if c not in product.claims]
            + [f"{product.id}: contains excluded allergen {a}" for a in exclude_allergens if a in product.allergens_contains])


class Catalog:
    def __init__(self, db):
        self.db = db

    def get(self, product_id):
        with self.db.connect() as conn:
            row = conn.execute("SELECT data FROM products WHERE id=?", (product_id,)).fetchone()
        if not row:
            raise DomainError("PRODUCT_NOT_FOUND", "Product is not in the local catalog", 404)
        return Product.model_validate_json(row["data"])

    def count(self):
        with self.db.connect() as conn:
            return conn.execute("SELECT count(*) FROM products").fetchone()[0]

    def search(self, query, required_claims=None, exclude_allergens=None, limit=10):
        if not 1 <= limit <= 10:
            raise DomainError("INVALID_LIMIT", "limit must be between 1 and 10", 422)
        tokens = re.findall(r"[^\W_]+", query, re.UNICODE)
        if not tokens:
            return [], 0
        expression = " AND ".join('"' + token + '"' for token in tokens)
        offers, excluded = [], 0
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT p.data FROM products_fts f JOIN products p ON p.id=f.id "
                "WHERE products_fts MATCH ? ORDER BY bm25(products_fts), p.id", (expression,))
            for row in rows:
                p = Product.model_validate_json(row["data"])
                if hard_violations(p, required_claims or [], exclude_allergens or []):
                    excluded += 1
                elif len(offers) < limit:
                    offers.append(p)
        return offers, excluded


def main():
    parser = argparse.ArgumentParser(description="Build the local product catalog; no network calls.")
    parser.add_argument("command", choices=["build"])
    parser.add_argument("--db", type=Path, default=BACKEND / "var/app.db")
    args = parser.parse_args()
    print(json.dumps(build(Database(args.db))))


if __name__ == "__main__":
    main()
