"""Overnight leak detection on tank level telemetry.

Pure Python, no AWS, no I/O. All thresholds are function arguments with
sensible defaults; nothing is read from config files or the environment.

Pipeline: raw readings -> :func:`resample_median` -> :func:`is_clean_night`
gate -> :func:`fit_loss_rate` -> :func:`nightly_rate` -> compare against
:func:`baseline` of past clean nights in :func:`evaluate_leak`.

Readings are dicts shaped like the simulator/device telemetry:
``{"ts": <epoch seconds>, "levelPct": <0..100>, "pumpOn": <bool>}``.
(``level_pct`` / ``pump_on`` spellings are also accepted.)
"""

import math
from typing import Any, Iterable, Optional

from backend.shared.level import median as _median

MIN_CLEAN_HOURS = 1.5  # nightly_rate needs at least 90 minutes of data
HIGH_CONF_R2 = 0.9
HIGH_CONF_HOURS = 3.0
MED_CONF_R2 = 0.7
MIN_BASELINE_NIGHTS = 3
MAD_MULTIPLIER = 3.0


def _reading_ts(reading: Any) -> Optional[float]:
    if not isinstance(reading, dict):
        return None
    ts = reading.get("ts", reading.get("timestamp"))
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    if math.isnan(ts) or not math.isfinite(ts):
        return None
    return float(ts)


def _reading_level(reading: Any) -> Optional[float]:
    if not isinstance(reading, dict):
        return None
    level = reading.get("levelPct", reading.get("level_pct"))
    if isinstance(level, bool) or not isinstance(level, (int, float)):
        return None
    if math.isnan(level) or not math.isfinite(level):
        return None
    return float(level)


def _reading_pump(reading: Any) -> bool:
    if not isinstance(reading, dict):
        return False
    return bool(reading.get("pumpOn", reading.get("pump_on", False)))


def _check_threshold(name: str, value: float, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number, got {type(value).__name__}")
    if math.isnan(value) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return float(value)


def resample_median(
    readings: Iterable[dict], bucket_seconds: int = 300
) -> list[tuple[float, float]]:
    """Bucket readings into fixed windows; median level per non-empty bucket.

    Returns ``[(bucket_start_ts, median_level_pct)]`` sorted by timestamp.
    Buckets are epoch-aligned (``floor(ts / bucket_seconds)``). Readings
    with missing/invalid timestamps or levels are skipped; buckets left
    with no valid level produce no point.
    """
    _check_threshold("bucket_seconds", bucket_seconds, minimum=1)
    buckets: dict[float, list[float]] = {}
    for reading in readings:
        ts = _reading_ts(reading)
        level = _reading_level(reading)
        if ts is None or level is None:
            continue
        start = math.floor(ts / bucket_seconds) * bucket_seconds
        buckets.setdefault(start, []).append(level)
    points: list[tuple[float, float]] = []
    for start in sorted(buckets):
        med = _median(buckets[start])
        if med is not None:
            points.append((float(start), med))
    return points


def is_clean_night(
    readings: Iterable[dict], noise_tolerance_pct: float = 0.5
) -> bool:
    """True only if the pump stayed off and the level never rose.

    "Never rose" means ``max(level) - first(level) <= noise_tolerance_pct``
    over readings sorted by timestamp, so small sensor noise passes but a
    refill (or any upward drift beyond tolerance) fails. A falling level
    (a leak) still counts as clean: it is what the night is for measuring.
    """
    _check_threshold("noise_tolerance_pct", noise_tolerance_pct)
    rows: list[tuple[float, float]] = []
    pump_seen = False
    for reading in readings:
        if _reading_pump(reading):
            pump_seen = True
        ts = _reading_ts(reading)
        level = _reading_level(reading)
        if ts is None or level is None:
            continue
        rows.append((ts, level))
    if not rows or pump_seen:
        return False
    rows.sort(key=lambda r: r[0])
    levels = [level for _, level in rows]
    return max(levels) - levels[0] <= noise_tolerance_pct


def fit_loss_rate(points: Iterable[tuple[float, float]]) -> tuple[float, float]:
    """Least-squares fit of level vs time.

    Returns ``(rate_pct_per_hr, r_squared)`` where the rate is the negative
    slope in percent per hour (positive when the level is falling). A
    perfectly flat series fits exactly, so it yields ``(0.0, 1.0)``. Fewer
    than two valid points yields ``(0.0, 0.0)``.
    """
    xs: list[float] = []
    ys: list[float] = []
    for point in points:
        try:
            x, y = point
        except (TypeError, ValueError):
            continue
        if (
            isinstance(x, bool)
            or isinstance(y, bool)
            or not isinstance(x, (int, float))
            or not isinstance(y, (int, float))
            or math.isnan(x)
            or math.isnan(y)
            or not math.isfinite(x)
            or not math.isfinite(y)
        ):
            continue
        xs.append(float(x))
        ys.append(float(y))
    n = len(xs)
    if n < 2:
        return (0.0, 0.0)
    sum_x = sum(xs)
    sum_y = sum(ys)
    sum_xx = sum(x * x for x in xs)
    sum_xy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        return (0.0, 0.0)
    slope = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n
    rate = -slope * 3600.0
    mean_y = sum_y / n
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    if ss_tot == 0:
        r_squared = 1.0 if ss_res == 0 else 0.0
    else:
        r_squared = 1.0 - ss_res / ss_tot
    r_squared = min(1.0, max(0.0, r_squared))
    return (rate, r_squared)


def nightly_rate(readings: Iterable[dict]) -> Optional[dict]:
    """Fit the overnight loss rate, or None if the night is unusable.

    Returns ``{"rate", "r_squared", "hours_observed"}``. Returns None when
    the night is not clean (pump ran or level rose) or spans under 90
    minutes of resampled data.
    """
    readings = list(readings)
    if not is_clean_night(readings):
        return None
    points = resample_median(readings)
    if len(points) < 2:
        return None
    hours = (points[-1][0] - points[0][0]) / 3600.0
    if hours < MIN_CLEAN_HOURS:
        return None
    rate, r_squared = fit_loss_rate(points)
    return {"rate": rate, "r_squared": r_squared, "hours_observed": hours}


def _rate_values(past_rates: Optional[Iterable[Any]]) -> list[float]:
    values: list[float] = []
    if past_rates is None:
        return values
    for item in past_rates:
        if isinstance(item, dict):
            item = item.get("rate")
        if item is None or isinstance(item, bool):
            continue
        try:
            value = float(item)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if math.isnan(value) or not math.isfinite(value):
            continue
        values.append(value)
    return values


def baseline(past_rates: Optional[Iterable[Any]]) -> tuple[float, float]:
    """Robust baseline of past clean-night rates: ``(median, mad)``.

    MAD is the median absolute deviation from the median. Accepts raw
    floats or ``nightly_rate`` dicts. Empty input yields ``(0.0, 0.0)``.
    """
    values = _rate_values(past_rates)
    if not values:
        return (0.0, 0.0)
    med = _median(values)
    assert med is not None  # non-empty finite input always has a median
    mad = _median([abs(v - med) for v in values])
    assert mad is not None
    return (float(med), float(mad))


def _tonight_values(tonight: Optional[dict]) -> tuple[float, float, float]:
    if tonight is None:
        return (0.0, 0.0, 0.0)

    def _num(key: str) -> float:
        value = tonight.get(key, 0.0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0.0
        if math.isnan(value) or not math.isfinite(value):
            return 0.0
        return float(value)

    return (_num("rate"), _num("r_squared"), _num("hours_observed"))


def evaluate_leak(
    tonight: Optional[dict],
    past_rates: Optional[Iterable[Any]],
    abs_min_pct_per_hr: float = 0.8,
    capacity_l: float = 1000.0,
    tariff_per_kl: float = 60.0,
) -> dict:
    """Decide whether tonight's fitted rate indicates a leak.

    Flags when ``rate > max(abs_min, median + 3*mad)``; with fewer than 3
    past clean nights the absolute threshold alone applies. Confidence is
    high when R^2 >= 0.9 with >= 3 hours observed, medium when R^2 >= 0.7,
    else low. Estimated loss is ``rate x hours x capacity`` (floored at 0
    for nights where the level rose overall); cost follows from the tariff.
    """
    abs_min = _check_threshold("abs_min_pct_per_hr", abs_min_pct_per_hr)
    capacity = _check_threshold("capacity_l", capacity_l)
    tariff = _check_threshold("tariff_per_kl", tariff_per_kl)

    rate, r_squared, hours = _tonight_values(tonight)
    values = _rate_values(past_rates)
    med, mad = baseline(values)
    if len(values) >= MIN_BASELINE_NIGHTS:
        threshold = max(abs_min, med + MAD_MULTIPLIER * mad)
    else:
        threshold = abs_min

    leak = rate > threshold
    if r_squared >= HIGH_CONF_R2 and hours >= HIGH_CONF_HOURS:
        confidence = "high"
    elif r_squared >= MED_CONF_R2:
        confidence = "medium"
    else:
        confidence = "low"

    loss_l = max(0.0, rate) * max(0.0, hours) * capacity / 100.0
    rupees = loss_l / 1000.0 * tariff
    return {
        "leak": leak,
        "rate": rate,
        "r_squared": r_squared,
        "hours_observed": hours,
        "threshold": threshold,
        "confidence": confidence,
        "estimated_loss_l": loss_l,
        "estimated_rupees": rupees,
    }
