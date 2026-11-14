"""The single data-file loader for spec 45 tenant guardrail presets.

Values use the spec 44 tenant-layer concepts; they are not agent mandates.
The governance lane consumes this snapshot when materializing layer 2.
"""
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path

import yaml


@lru_cache(maxsize=1)
def preset_catalog():
    with (Path(__file__).resolve().parents[1] / "guardrail_presets.yaml").open() as file:
        return yaml.safe_load(file)


def signup_options():
    data = preset_catalog()
    return {
        "business_types": [{"value": k, "label": v["label"]} for k, v in data["presets"].items()],
        "monthly_purchase_volume_bands": [{"value": k, "label": v["label"]} for k, v in data["volume_bands"].items()],
        "meals_per_day_bands": [{"value": k, "label": v} for k, v in data["meals_per_day_bands"].items()],
    }


def load_preset(name, monthly_purchase_volume_band, sites=1):
    """Return independent, sourced field values; budgets cover the whole tenant.

    Open-ended volume is conservatively seeded at 200k EUR. Daily budget is
    monthly / 30; a multi-site group/chain also gets equal per-site allocations.
    """
    data = preset_catalog()
    preset = deepcopy(data["presets"][name])
    monthly = Decimal(str(data["volume_bands"][monthly_purchase_volume_band]["monthly_eur"])) * Decimal(str(preset.pop("budget_multiplier")))
    per_site = preset.pop("per_site_budgets")
    preset.pop("label")
    money = lambda value: float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    preset.update(daily_budget_eur=money(monthly / 30), monthly_budget_eur=money(monthly))
    if per_site and sites > 1:
        # Round down allocations so their sum never exceeds the tenant budget.
        preset["per_site_budgets"] = {
            "sites": sites,
            "daily_budget_eur": int(monthly / 30 * 100 / sites) / 100,
            "monthly_budget_eur": int(monthly * 100 / sites) / 100,
        }
    return {key: {"value": value, "source": "preset:" + name} for key, value in preset.items()}
