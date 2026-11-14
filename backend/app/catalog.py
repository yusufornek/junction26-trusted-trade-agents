import argparse
import csv
import gzip
import json
import math
import re
import time
from statistics import median
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
        raise ValueError(f"Missing catalog data file: {path}")
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for required in ("productId", column):
            if required not in (reader.fieldnames or []):
                raise ValueError(f"Catalog data file {path}: missing column {required}")
        return {str(row["productId"]): number(row.get(column)) for row in reader}


def name_sizes(name, unit):
    pattern = r"(?<![\d.,])(?:(\d+)\s*[x×]\s*)?(\d+(?:[.,]\d+)?)\s*(kg|dl|cl|ml|l|g)\b"
    factors = {"l": 1, "dl": .1, "cl": .01, "ml": .001} if unit == "L" else {"kg": 1, "g": .001}
    return [float(match[2].replace(",", ".")) * factors[match[3].lower()]
            for match in re.finditer(pattern, name, re.IGNORECASE) if match[3].lower() in factors]


def suspect_content(value, names, unit):
    if value is None:
        return False
    named = [size for name in names for size in name_sizes(name, unit) if size > 0]
    # Names can contain both portion and package weights. A compatible package candidate
    # supports the structured content even if an earlier portion candidate disagrees.
    return (value <= 0 or value > 100 or (unit == "KG" and value < .001)
            or bool(named and all(max(value / size, size / value) >= 10 for size in named)))


def extract(raw, reliability, carbon):
    synkka = raw.get("synkkaData") or {}
    grouped = {c["name"]: c.get("values") or [] for c in synkka.get("classifications", [])}

    def ids(name, unit=None):
        return sorted({str(v["id"]) for v in grouped.get(name, [])
                       if v.get("id") and (unit is None or v.get("unit") == unit)})

    base = next((c for c in synkka.get("unitConversions", [])
                 if c.get("synkkaPackagingType") == "BASE_UNIT_OR_EACH"), {})
    case = next((c for c in synkka.get("unitConversions", [])
                 if c.get("synkkaPackagingType") == "CASE"), {}) if raw.get("salesUnit") in {"KI", "CASE"} else {}

    def content(record, field, factors):
        value = record.get(field) or {}
        n = number(value.get("value"))
        return n * factors[value["unit"]] if n is not None and value.get("unit") in factors else None

    volumes, weights = {"LTR": 1, "MLT": .001, "CLT": .01}, {"KGM": 1, "GRM": .001}
    volume_l, weight_kg = content(base, "volume", volumes), content(base, "netWeight", weights)
    case_l, case_kg = content(case, "volume", volumes), content(case, "netWeight", weights)
    names = [localized(synkka.get("names"), language) for language in ("en", "fi")]
    suspicion = {"L": suspect_content(volume_l, names, "L"),
                 "KG": suspect_content(weight_kg, names, "KG")}
    multiplier = next((number(u.get("sizeInBaseUnits")) for u in raw.get("units") or []
                       if u.get("unitId") == raw.get("salesUnit")), 1) or 1
    for value, unit in ((case_l, "L"), (case_kg, "KG")):
        per_base = value / multiplier if value is not None else None
        suspicion[unit] |= suspect_content(per_base, names, unit)
    statuses, declaration = {}, False
    for group in ("allergen", "nonAllergen"):
        for entry in grouped.get(group, []):
            code, status = str(entry.get("id", "")).strip().upper(), str(entry.get("unit", "")).lower()
            if code == "X99":
                declaration = True
                continue
            if code and status in ({"contains", "may_contain", "undeclared"} if group == "allergen" else {"free_from"}):
                statuses.setdefault(code, set()).add(status)
                declaration |= status != "free_from"
    return Product(
        id=str(raw["id"]), name_en=localized(synkka.get("names"), "en") or localized(synkka.get("names"), "fi"),
        gtin=str(raw["salesUnitGtin"]) if raw.get("salesUnitGtin") else None,
        unit_gtins={u["unitId"]: str(u["gtin"]) for u in (raw.get("units") or []) if u.get("gtin")},
        name_fi=localized(synkka.get("names"), "fi"), category=str(raw["category"]) if raw.get("category") else None,
        sales_unit=raw.get("salesUnit") or "ST", base_unit=raw.get("baseUnit") or "ST",
        allowed_lot_size=number(raw.get("allowedLotSize")) or 1,
        vendor_name=raw.get("vendorName"), brand=synkka.get("brand"), country=raw.get("countryOfOrigin"),
        temperature_condition=raw.get("temperatureCondition"),
        units={u["unitId"]: n for u in (raw.get("units") or [])
               if (n := number(u.get("sizeInBaseUnits"))) is not None},
        claims=[c.strip().upper() for c in ids("nutritionalClaim")],
        allergens_contains=sorted(c for c, s in statuses.items() if "contains" in s),
        allergen_status={c: sorted(s) for c, s in statuses.items()}, has_allergen_declaration=declaration,
        free_from=sorted(c for c, s in statuses.items() if "free_from" in s), labels=ids("packagingMarkedLabel"),
        base_volume_l=volume_l, size_suspect=any(suspicion.values()),
        volume_suspect=suspicion["L"], weight_suspect=suspicion["KG"], sales_volume_l=case_l, sales_net_weight_kg=case_kg,
        variable_unit=bool(synkka.get("variableUnit") or raw.get("variableUnit")), deposits=raw.get("deposits") or [],
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
        count, deleted = 0, 0
        coverage = {field: {"filled": 0, "empty": 0} for field in Product.model_fields}
        vocabulary = {"required_claims": set(), "exclude_allergens": set(), "preferences.labels": set()}
        ranking = {"reliability": [], "co2": []}
        for raw in raw_products:
            if raw.get("deleted"):
                deleted += 1
                continue
            product = extract(raw, reliability, carbon)
            values = product.model_dump()
            for field, value in values.items():
                filled = value is not None and value != "" and value != [] and value != {} and value is not False
                coverage[field]["filled" if filled else "empty"] += 1
            vocabulary["required_claims"].update(product.claims)
            vocabulary["exclude_allergens"].update(product.allergen_status)
            vocabulary["preferences.labels"].update(product.labels)
            for key, value in (("reliability", product.delivery_reliability), ("co2", product.co2_kg_per_kg)):
                if value is not None:
                    ranking[key].append(value)
            columns = list(values)
            row = [json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for v in values.values()]
            conn.execute(f"INSERT INTO products({','.join(columns)},data) VALUES({','.join('?' for _ in range(len(row)+1))})",
                         [*row, product.model_dump_json()])
            synkka = raw.get("synkkaData") or {}
            conn.execute("INSERT INTO products_fts VALUES(?,?,?,?,?)", (
                product.id, product.name_en, product.name_fi,
                localized(synkka.get("keyIngredients"), "en"), localized(synkka.get("marketingTexts"), "en")))
            count += 1
        metadata = {"coverage": coverage, "vocabulary": {k: sorted(v) for k, v in vocabulary.items()},
                    "ranking": {k: {"median": median(v), "min": min(v), "max": max(v)}
                                for k, v in ranking.items() if v}}
        for key, value in metadata.items():
            conn.execute("INSERT OR REPLACE INTO catalog_metadata VALUES(?,?)", (key, json.dumps(value)))
    return {"rows": count, "deleted_skipped": deleted, "seconds": round(time.perf_counter() - start, 3),
            "coverage": coverage}


CLAIM_CONTRADICTIONS = {
    "FREE_FROM_LACTOSE": {"ML"}, "FREE_FROM_GLUTEN": {"AW", "UW", "GB", "NR", "GS", "GK"},
    "FREE_FROM_EGGS": {"AE"}, "FREE_FROM_SOY": {"AY"}, "FREE_FROM_WHEAT": {"UW"},
    "VEGAN": {"AM", "ML", "AE", "AF", "AC", "AN"},
}


def allergen_evidence(product, code):
    statuses = set(product.allergen_status.get(code, []))
    if "contains" in statuses:
        return False, "contains"
    if "may_contain" in statuses:
        return False, "may contain"
    if "undeclared" in statuses:
        return False, "undeclared"
    if "free_from" in statuses:
        return True, f"{code} declared free-from (catalog nonAllergen)"
    if product.has_allergen_declaration:
        return True, f"{code} not listed in the catalog allergen declaration"
    return False, "no allergen data"


def constraint_failures(product, required_claims, exclude_allergens):
    failures = []
    for code in required_claims:
        reason = None
        if code not in product.claims:
            reason = "missing required claim"
        elif any(set(product.allergen_status.get(a, [])) & {"contains", "may_contain"}
                 for a in CLAIM_CONTRADICTIONS.get(code, set())):
            reason = "claim contradicted by allergen data"
        if reason:
            failures.append(("required_claims", code, reason))
    for code in exclude_allergens:
        passes, reason = allergen_evidence(product, code)
        if not passes:
            failures.append(("exclude_allergens", code, reason))
    return failures


def hard_violations(product, required_claims, exclude_allergens, sources=None):
    return [f"{product.id}: {reason} {code} ({source})"
            for field, code, reason in constraint_failures(product, required_claims, exclude_allergens)
            for source in ([v.source for v in getattr(sources, field) if v.code == code] if sources else ["request"])]


class Catalog:
    def __init__(self, db):
        self.db = db

    def metadata(self, key):
        with self.db.connect() as conn:
            row = conn.execute("SELECT data FROM catalog_metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else {}

    def normalize_codes(self, values, field, *, location=None):
        vocabulary = self.metadata("vocabulary").get(field, [])
        canonical = {v.casefold(): v for v in vocabulary}
        result = []
        for value in values:
            code = canonical.get(value.strip().casefold())
            if code is None:
                raise DomainError("UNKNOWN_CODE", f"{location or field}: unknown code {value!r}; valid values: {', '.join(vocabulary)}", 422)
            if code not in result:
                result.append(code)
        return result

    def get(self, product_id):
        with self.db.connect() as conn:
            row = conn.execute("SELECT data FROM products WHERE id=?", (product_id,)).fetchone()
        if not row:
            raise DomainError("PRODUCT_NOT_FOUND", "Product is not in the local catalog", 404)
        return Product.model_validate_json(row["data"])

    def count(self):
        with self.db.connect() as conn:
            return conn.execute("SELECT count(*) FROM products").fetchone()[0]

    def search(self, query, required_claims=None, exclude_allergens=None, limit=10, *, max_limit=10,
               removed_by=None, mode="and", counts=None, sources=None, categories=None):
        """Keep the (products, excluded) API; optional counts includes every hard-filter match.

        OR retrieval orders by distinct terms matched, then FTS rank and product ID.
        Quoted hyphenated terms are phrases; callers can also supply their parts.
        """
        required_claims = self.normalize_codes(required_claims or [], "required_claims")
        exclude_allergens = self.normalize_codes(exclude_allergens or [], "exclude_allergens")
        if not 1 <= limit <= max_limit:
            raise DomainError("INVALID_LIMIT", f"limit must be between 1 and {max_limit}", 422)
        if mode not in {"and", "or"}:
            raise DomainError("INVALID_SEARCH_MODE", "Search mode must be and or or", 422)
        if counts is not None:
            counts["matched_count"] = 0
        tokens = re.findall(r"[^\W_]+(?:-[^\W_]+)*" if mode == "or" else r"[^\W_]+", query, re.UNICODE)
        if mode == "or":
            tokens = list(dict.fromkeys(t.lower() for t in tokens))
        if not tokens:
            return [], 0
        phrases = ['"' + token + '"' for token in tokens]
        expression = (" OR " if mode == "or" else " AND ").join(phrases)
        offers, excluded, matched = [], 0, 0
        with self.db.connect() as conn:
            prefix, join, order, params = "", "", "", []
            if mode == "or":
                # json_each avoids one compound SELECT per word (SQLite caps those at 500).
                # Each distinct phrase still contributes exactly one hit per matching product.
                prefix = ("WITH term_hits AS (SELECT products_fts.rowid FROM json_each(?) AS term "
                          "CROSS JOIN products_fts WHERE products_fts MATCH term.value), "
                          "hits AS (SELECT rowid, count(*) AS words FROM term_hits GROUP BY rowid) ")
                join = "JOIN hits ON hits.rowid=f.rowid "
                order = "hits.words DESC, "
                params.append(json.dumps(phrases))
            rows = conn.execute(prefix +
                "SELECT p.data FROM products_fts f JOIN products p ON p.id=f.id " + join +
                "WHERE products_fts MATCH ? ORDER BY " + order + "bm25(products_fts), p.id", (*params, expression))
            for row in rows:
                p = Product.model_validate_json(row["data"])
                if categories is not None and p.category not in categories:
                    continue
                failures = constraint_failures(p, required_claims, exclude_allergens)
                if failures:
                    excluded += 1
                    if removed_by is not None:
                        for field, code, reason in failures:
                            origins = [v.source for v in getattr(sources, field) if v.code == code] if sources else ["request"]
                            for origin in origins:
                                key = f"{field}.{code} ({origin}; {reason})"
                                removed_by[key] = removed_by.get(key, 0) + 1
                else:
                    matched += 1
                    if len(offers) < limit:
                        offers.append(p)
        if counts is not None:
            counts["matched_count"] = matched
        return offers, excluded


def main():
    parser = argparse.ArgumentParser(description="Build the local product catalog; no network calls.")
    parser.add_argument("command", choices=["build"])
    parser.add_argument("--db", type=Path, default=BACKEND / "var/app.db")
    parser.add_argument("--source", type=Path, default=ROOT / "data/product_data.json.gz")
    parser.add_argument("--reliability", type=Path, default=ROOT / "data/delivery_reliability.csv")
    parser.add_argument("--carbon", type=Path, default=ROOT / "data/carbon_footprint.csv")
    args = parser.parse_args()
    try:
        print(json.dumps(build(Database(args.db), args.source, args.reliability, args.carbon)))
    except (ValueError, OSError) as error:
        parser.exit(1, f"Catalog build failed: {error}\n")


if __name__ == "__main__":
    main()
