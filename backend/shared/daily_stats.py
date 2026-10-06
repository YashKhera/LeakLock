"""Daily water-balance statistics for one tank and one IST calendar day.

Pure Python, no AWS, no I/O. The day runs midnight to midnight in
Asia/Kolkata. All tank constants (capacity, pump flow, overrun assumption,
tariff) come from the ``tank`` dict; nothing is hard-coded.

Tank dict keys (camelCase or snake_case)::

    capacityL / capacity_l       tank capacity in litres
    pumpFlowLpm / pump_flow_lpm  pump flow in litres per minute
    assumedOverrunMin / assumed_overrun_min
                                minutes the pump would have kept running
                                past full without the cut-off
    tariffPerKL / tariff_per_kl water price in rupees per kilolitre

Readings are device/simulator telemetry dicts with ``ts``, ``levelPct``
and ``pumpOn``. Events are dicts with a type, a reason and a ``ts``
(bare ``"PUMP_CUT"`` strings carry no reason and are not counted).
Leak evaluations are ``leak_detection.evaluate_leak`` dicts, optionally
carrying ``date`` (``"YYYY-MM-DD"``) or a ``ts`` to place them in a day.
"""

import math
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

from backend.shared.level import median as _median

IST = ZoneInfo("Asia/Kolkata")
BUCKET_SECONDS = 300  # 5-minute medians


def _tank_num(tank: Mapping[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        if key in tank and tank[key] is not None:
            value = tank[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"tank[{key!r}] must be a number")
            if math.isnan(value) or not math.isfinite(value):
                raise ValueError(f"tank[{key!r}] must be finite")
            return float(value)
    return float(default)


def _reading_parts(reading: Any) -> Optional[tuple[float, Optional[float], bool]]:
    """(ts, level or None, pump_on); None when the timestamp is unusable."""
    if not isinstance(reading, dict):
        return None
    ts = reading.get("ts", reading.get("timestamp"))
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    if math.isnan(ts) or not math.isfinite(ts):
        return None
    level = reading.get("levelPct", reading.get("level_pct"))
    if isinstance(level, bool) or not isinstance(level, (int, float)):
        level = None
    elif math.isnan(level) or not math.isfinite(level):
        level = None
    else:
        level = float(level)
    pump = bool(reading.get("pumpOn", reading.get("pump_on", False)))
    return (float(ts), level, pump)


def _day_bounds(date_str: str) -> tuple[float, float]:
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=IST)
    except ValueError:
        raise ValueError(f"date_str must be YYYY-MM-DD, got {date_str!r}")
    start = day.timestamp()
    end = start + 24 * 3600
    return (start, end)


def _event_ts(event: Any) -> Optional[float]:
    if isinstance(event, dict):
        ts = event.get("ts", event.get("timestamp"))
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            return None
        if math.isnan(ts) or not math.isfinite(ts):
            return None
        return float(ts)
    return None


def _is_overflow_cut(event: Any) -> bool:
    if not isinstance(event, dict):
        return False  # bare strings carry no reason; only FULL counts
    kind = event.get("type", event.get("event", event.get("event_type", "")))
    reason = event.get("reason", event.get("cause", ""))
    return str(kind) == "PUMP_CUT" and str(reason).upper() == "FULL"


def _eval_day_key(evaluation: Any) -> Optional[str]:
    if not isinstance(evaluation, dict):
        return None
    date = evaluation.get("date", evaluation.get("date_str"))
    if date:
        return str(date)
    return None


def _eval_num(evaluation: Any, *keys: str) -> Optional[float]:
    if not isinstance(evaluation, dict):
        return None
    for key in keys:
        if key in evaluation and evaluation[key] is not None:
            value = evaluation[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            if math.isnan(value) or not math.isfinite(value):
                return None
            return float(value)
    return None


def compute_daily(
    readings: Iterable[dict],
    events: Iterable[Any],
    leak_evaluations: Iterable[Any],
    tank: Mapping[str, Any],
    date_str: str,
    noise_threshold_pct: float = 0.5,
) -> dict:
    """Aggregate one IST calendar day of telemetry into a stats dict."""
    if (
        isinstance(noise_threshold_pct, bool)
        or not isinstance(noise_threshold_pct, (int, float))
        or math.isnan(noise_threshold_pct)
        or not math.isfinite(noise_threshold_pct)
        or noise_threshold_pct < 0
    ):
        raise ValueError("noise_threshold_pct must be a finite number >= 0")

    capacity_l = _tank_num(tank, "capacityL", "capacity_l")
    flow_lpm = _tank_num(tank, "pumpFlowLpm", "pump_flow_lpm")
    overrun_min = _tank_num(tank, "assumedOverrunMin", "assumed_overrun_min")
    tariff_per_kl = _tank_num(tank, "tariffPerKL", "tariff_per_kl")
    if flow_lpm < 0 or overrun_min < 0 or tariff_per_kl < 0 or capacity_l < 0:
        raise ValueError("tank flow, overrun, tariff and capacity must be >= 0")

    start, end = _day_bounds(date_str)
    noise_l = noise_threshold_pct / 100.0 * capacity_l

    zeros = {
        "date": date_str,
        "tank_id": tank.get("tank_id", tank.get("id")),
        "consumedL": 0.0,
        "filledL": 0.0,
        "overflowCuts": 0,
        "leakEvents": 0,
        "wastedL": 0.0,
        "savedL": 0.0,
        "rupeesWasted": 0.0,
        "rupeesSaved": 0.0,
        "nightlyRatePctPerHr": None,
        "dataMissing": False,
    }

    # Bucket the day's readings into 5-minute medians with pump state.
    buckets: dict[float, dict] = {}
    for reading in readings:
        parts = _reading_parts(reading)
        if parts is None:
            continue
        ts, level, pump = parts
        if not start <= ts < end:
            continue
        key = math.floor(ts / BUCKET_SECONDS) * BUCKET_SECONDS
        bucket = buckets.setdefault(key, {"levels": [], "pump": False})
        if level is not None:
            bucket["levels"].append(level)
        if pump:
            bucket["pump"] = True
    points: list[tuple[float, float, bool]] = []
    for key in sorted(buckets):
        med = _median(buckets[key]["levels"])
        if med is not None:
            points.append((float(key), med, buckets[key]["pump"]))

    if not points:
        zeros["dataMissing"] = True
        return zeros

    consumed_l = 0.0
    filled_l = 0.0
    # Accumulate maximal runs of same-sign bucket steps and flush a run only
    # when it reaches the noise threshold. This keeps slow seeps (every
    # 5-minute step below the threshold, but steadily falling) visible in
    # consumedL, while oscillating sensor noise cancels itself out instead
    # of accumulating.
    run_total = 0.0
    run_kind: Optional[str] = None  # "drop" or "rise"

    def _flush() -> None:
        nonlocal consumed_l, filled_l, run_total, run_kind
        if run_kind == "drop" and -run_total >= noise_l:
            consumed_l += -run_total
        elif run_kind == "rise" and run_total >= noise_l:
            filled_l += run_total
        run_total = 0.0
        run_kind = None

    for (_, prev_level, prev_pump), (_, cur_level, cur_pump) in zip(
        points, points[1:]
    ):
        delta_l = (cur_level - prev_level) / 100.0 * capacity_l
        if delta_l < 0.0 and not prev_pump and not cur_pump:
            # A drop only counts as consumption when the pump was fully off
            # across both buckets; otherwise the pump was moving water too.
            kind: Optional[str] = "drop"
        elif delta_l > 0.0:
            kind = "rise"
        else:
            _flush()  # exact standstill, or an ambiguous pump-on drop
            continue
        if kind != run_kind:
            _flush()
            run_kind = kind
        run_total += delta_l
    _flush()

    overflow_cuts = 0
    for event in events:
        if not _is_overflow_cut(event):
            continue
        ts = _event_ts(event)
        if ts is not None and not start <= ts < end:
            continue
        overflow_cuts += 1

    leak_events = 0
    wasted_l = 0.0
    nightly_rate: Optional[float] = None
    for evaluation in leak_evaluations:
        if not isinstance(evaluation, dict):
            continue
        day_key = _eval_day_key(evaluation)
        if day_key is not None and day_key != date_str:
            continue
        if day_key is None:
            ts = _event_ts(evaluation)
            if ts is not None and not start <= ts < end:
                continue
        rate = _eval_num(evaluation, "rate")
        if rate is not None:
            nightly_rate = rate  # last dated evaluation with a rate wins
        if evaluation.get("leak"):
            leak_events += 1
            loss = _eval_num(evaluation, "estimated_loss_l", "estimatedLossL")
            wasted_l += max(0.0, loss) if loss is not None else 0.0

    # assumedSavedL: each FULL cut is assumed to have spared the tank
    # flow_lpm litres/min of overrun for overrun_min minutes.
    saved_l = overflow_cuts * flow_lpm * overrun_min

    zeros.update(
        {
            "consumedL": consumed_l,
            "filledL": filled_l,
            "overflowCuts": overflow_cuts,
            "leakEvents": leak_events,
            "wastedL": wasted_l,
            "savedL": saved_l,
            "rupeesWasted": wasted_l / 1000.0 * tariff_per_kl,
            "rupeesSaved": saved_l / 1000.0 * tariff_per_kl,
            "nightlyRatePctPerHr": nightly_rate,
        }
    )
    return zeros
