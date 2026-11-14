"""Pure spec 45 checks. No storage, network, model loading or policy mutations."""
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from statistics import median
from typing import Protocol, Sequence


class Forecaster(Protocol):
    def predict(self, series: Sequence[float], horizon: int) -> dict[float, float]: ...


@dataclass(frozen=True)
class Point:
    at: datetime
    value: float


def expected_band(series, at, k=3, *, weekday=True, forecaster=None):
    """Use a weekday bucket only with four observations; otherwise use the full series.

    MAD is deliberately unscaled. Constant history has a zero-width band. Invalid
    or unavailable forecast output always leaves the deterministic band intact.
    """
    series = sorted((p for p in series if p.at < at and isfinite(p.value)), key=lambda p: p.at)
    bucket = [p for p in series if p.at.weekday() == at.weekday()]
    selected = bucket if weekday and len(bucket) >= 4 else series
    result = {"status": "insufficient_history", "expected_band": None, "band_source": "statistical",
              "history_window": {"start": selected[0].at.isoformat() if selected else None,
                                 "end": selected[-1].at.isoformat() if selected else None,
                                 "points": len(selected), "weekday": at.weekday() if selected and selected is bucket else None}}
    if len(selected) < 4:
        return result
    values = [p.value for p in selected]
    center = median(values)
    mad = median(abs(x - center) for x in values)
    result.update(status="normal", expected_band={"lower": max(0, center - k * mad),
                                                 "upper": center + k * mad, "median": center})
    if forecaster is not None:
        try:
            quantiles = forecaster.predict(values, 1)
            low, high = float(quantiles[.05]), float(quantiles[.95])
            if not (isfinite(low) and isfinite(high) and 0 <= low <= high):
                raise ValueError("Invalid forecast quantiles")
            result.update(expected_band={"lower": low, "upper": high, "median": center}, band_source="forecast")
        except Exception:
            pass
    return result


def quantity_check(series, observed, at, k=3, *, forecaster=None):
    result = expected_band(series, at, k, forecaster=forecaster)
    if result["expected_band"]:
        band = result["expected_band"]
        result["status"] = "anomaly" if observed < band["lower"] or observed > band["upper"] else "normal"
    return result


def price_check(series, observed, at, increase_pct=20):
    result = expected_band(series, at, weekday=False)
    if result["expected_band"]:
        last = max((p for p in series if p.at < at), key=lambda p: p.at).value
        limit = last * (1 + increase_pct / 100)
        result["expected_band"] = {"lower": 0, "upper": limit, "last_paid": last}
        result["status"] = "anomaly" if observed >= limit else "normal"
    return result


def velocity_check(order_times, observed, at, k=3, minimum_upper=5):
    # Completed occupied hours are the baseline. The configurable floor prevents
    # a sparse monthly customer from being paused by a second legitimate order.
    hours = {}
    current_hour = at.replace(minute=0, second=0, microsecond=0)
    for when in order_times:
        hour = when.replace(minute=0, second=0, microsecond=0)
        if hour < current_hour:
            hours[hour] = hours.get(hour, 0) + 1
    result = expected_band([Point(hour, count) for hour, count in hours.items()], at, k)
    if result["expected_band"]:
        result["expected_band"]["upper"] = max(minimum_upper, result["expected_band"]["upper"])
        result["status"] = "anomaly" if observed > result["expected_band"]["upper"] else "normal"
    result["pause"] = bool(result["expected_band"] and observed >= 2 * result["expected_band"]["upper"])
    return result


def category_check(categories, observed):
    return "insufficient_history" if len(categories) < 4 else "normal" if observed in categories else "anomaly"
