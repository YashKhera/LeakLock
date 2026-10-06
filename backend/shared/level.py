"""Tank level maths shared by the simulator, firmware logic and cloud code.

Pure Python, no AWS dependencies. All calibration values are passed in as
arguments; nothing is read from config files or environment variables here.
"""

import math
from typing import Iterable, Optional


def level_pct(distance_cm: float, d_empty_cm: float, d_full_cm: float) -> float:
    """Convert an ultrasonic distance reading to a fill percentage.

    level_pct = (d_empty - d) / (d_empty - d_full) * 100, clamped to 0..100.

    A NaN distance propagates as NaN so the caller (pump controller) treats
    it as an invalid reading and fails safe. Infinite distances clamp to
    the nearer bound.
    """
    for name, value in (
        ("distance_cm", distance_cm),
        ("d_empty_cm", d_empty_cm),
        ("d_full_cm", d_full_cm),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} must be a number, got {type(value).__name__}")
    if math.isnan(d_empty_cm) or math.isnan(d_full_cm):
        raise ValueError("calibration bounds must not be NaN")
    if not math.isfinite(d_empty_cm) or not math.isfinite(d_full_cm):
        raise ValueError("calibration bounds must be finite")
    if d_empty_cm <= d_full_cm:
        raise ValueError(
            f"d_empty_cm ({d_empty_cm}) must be greater than "
            f"d_full_cm ({d_full_cm}): the sensor is closer when the tank is full"
        )
    if distance_cm is None or math.isnan(distance_cm):
        return float("nan")

    raw = (d_empty_cm - distance_cm) / (d_empty_cm - d_full_cm) * 100.0
    if raw < 0.0:
        return 0.0
    if raw > 100.0:
        return 100.0
    return raw


def volume_l(level_pct: float, capacity_l: float) -> float:
    """Convert a fill percentage to litres for a tank of ``capacity_l``."""
    if isinstance(level_pct, bool) or not isinstance(level_pct, (int, float)):
        raise TypeError(f"level_pct must be a number, got {type(level_pct).__name__}")
    if isinstance(capacity_l, bool) or not isinstance(capacity_l, (int, float)):
        raise TypeError(f"capacity_l must be a number, got {type(capacity_l).__name__}")
    if math.isnan(capacity_l) or not math.isfinite(capacity_l):
        raise ValueError("capacity_l must be a finite number")
    if capacity_l < 0:
        raise ValueError(f"capacity_l must be >= 0, got {capacity_l}")
    if math.isnan(level_pct):
        return float("nan")

    clamped = level_pct
    if clamped < 0.0:
        clamped = 0.0
    if clamped > 100.0:
        clamped = 100.0
    return clamped / 100.0 * capacity_l


def is_valid_distance(
    distance_cm: Optional[float],
    d_empty_cm: float,
    d_full_cm: float,
    margin_cm: float,
) -> bool:
    """Check a raw distance reading against the calibrated range.

    Returns False for None, NaN, infinite, negative, or non-numeric values,
    and for values outside [min(d_full, d_empty) - margin,
    max(d_full, d_empty) + margin]. Bad calibration/margin is a programmer
    error and raises instead of returning False.
    """
    if isinstance(margin_cm, bool) or not isinstance(margin_cm, (int, float)):
        raise TypeError(f"margin_cm must be a number, got {type(margin_cm).__name__}")
    if math.isnan(margin_cm) or not math.isfinite(margin_cm):
        raise ValueError("margin_cm must be a finite number")
    if margin_cm < 0:
        raise ValueError(f"margin_cm must be >= 0, got {margin_cm}")
    for name, value in (("d_empty_cm", d_empty_cm), ("d_full_cm", d_full_cm)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} must be a number, got {type(value).__name__}")
        if math.isnan(value) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number")

    if distance_cm is None:
        return False
    if isinstance(distance_cm, bool) or not isinstance(distance_cm, (int, float)):
        return False
    if math.isnan(distance_cm) or not math.isfinite(distance_cm):
        return False
    if distance_cm < 0:
        return False

    lo = min(d_full_cm, d_empty_cm) - margin_cm
    hi = max(d_full_cm, d_empty_cm) + margin_cm
    return lo <= distance_cm <= hi


def median(values: Iterable[object]) -> Optional[float]:
    """Median of valid readings, ignoring None/NaN/inf/non-numeric entries.

    Returns None when there is no valid reading (so the pump controller
    treats it as a sensor fault and fails safe).
    """
    valid: list[float] = []
    for v in values:
        if v is None or isinstance(v, bool):
            continue
        try:
            f = float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if math.isnan(f) or not math.isfinite(f):
            continue
        valid.append(f)
    if not valid:
        return None
    valid.sort()
    n = len(valid)
    mid = n // 2
    if n % 2 == 1:
        return valid[mid]
    return (valid[mid - 1] + valid[mid]) / 2.0
