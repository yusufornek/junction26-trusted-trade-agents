"""Freeze pre-02k choices that agreed with explicit organiser catalog content.

Run from backend/. Uses Git and the shipped catalog only; never opens a database.
"""
import gzip
import hashlib
import json
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

BASELINE = "f799bca1f2232df2cf99636e1171048d537c707f"  # 74588c2^ (before 02k)


def main():
    modules = {}
    for module in ("catalog", "lookup"):
        source = subprocess.check_output(
            ["git", "show", f"{BASELINE}:backend/app/{module}.py"], cwd=BACKEND, text=True)
        scope = {"__name__": f"app._baseline_{module}", "__package__": "app"}
        exec(compile(source, f"{BASELINE}/{module}.py", "exec"), scope)
        modules[module] = scope
    # The old lookup must use its own parser, not today's imported implementation.
    for name in modules["catalog"]:
        if name in modules["lookup"]:
            modules["lookup"][name] = modules["catalog"][name]
    catalog_path = BACKEND.parent / "data/product_data.json.gz"
    with gzip.open(catalog_path, "rt") as file:
        rows = json.load(file)
    sizes = {}
    for row in rows:
        if row.get("deleted"):
            continue
        product = modules["catalog"]["extract"](row, {}, {})
        multiplier = product.units.get(product.sales_unit, 1 if product.sales_unit == product.base_unit else None)
        for unit, base, sales in (("L", product.base_volume_l, product.sales_volume_l),
                                 ("KG", product.base_net_weight_kg, product.sales_net_weight_kg)):
            anchor = sales or (base * multiplier if base and multiplier else None)
            if product.sales_unit == unit == "KG":
                anchor = 1.0
            elif product.base_unit == unit and not anchor:
                anchor = multiplier
            size, source, *_ = modules["lookup"]["reconcile_size"](product, {}, unit)
            if anchor and size == round(anchor, 6):
                sizes.setdefault(product.id, {})[unit] = size
    fixture = {"baseline_commit": BASELINE,
               "catalog_sha256": hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
               "product_count": len(rows), "anchor_count": sum(map(len, sizes.values())), "sizes": sizes}
    target = BACKEND / "tests/fixtures/catalog_anchor_sizes.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(fixture, sort_keys=True, indent=2) + "\n")
    print(f"Stored {fixture['anchor_count']} anchored dimensions across {len(sizes)} products in {target}")


if __name__ == "__main__":
    main()
