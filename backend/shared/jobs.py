"""Scheduled jobs: leak check, offline check, daily stats.

Pure functions over repos plus a clock, so Lambda handlers stay thin and
tests run them against moto unchanged. A ``Repos`` bundle carries the four
repositories; per-tank failures are logged and skipped, never aborting a
run. Days and quiet windows are evaluated in Asia/Kolkata.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo

from backend.shared import leak_detection
from backend.shared.alerts import (
    AlertStore,
    Notifier,
    evaluate_offline,
    leak_alert_from_evaluation,
    process_alert,
)
from backend.shared.daily_stats import compute_daily

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
SECONDS_PER_DAY = 86400
DEFAULT_CAPACITY_L = 1000.0  # mirrors evaluate_leak defaults
DEFAULT_TARIFF_PER_KL = 60.0


@dataclass
class Repos:
    """Repository bundle threaded through every job."""

    tanks: Any
    readings: Any
    events: Any
    daily: Any


def _num(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _tank_view(tank: Mapping[str, Any], tank_id: str) -> dict:
    # alerts.py speaks snake_case; the tank registry speaks camelCase.
    return {
        "tank_id": tank_id,
        "name": tank.get("name", tank.get("tankName", tank_id)),
        "capacityL": tank.get("capacityL", tank.get("capacity_l")),
        "tariffPerKL": tank.get("tariffPerKL", tank.get("tariff_per_kl")),
    }


def _ist_now(now_ts: float) -> datetime:
    return datetime.fromtimestamp(now_ts, tz=IST)


def _in_quiet_window(
    now_ts: float, start_hour: float, end_hour: float, after_minutes: float
) -> bool:
    now = _ist_now(now_ts)
    minute = now.hour * 60 + now.minute + now.second / 60.0
    start, end = start_hour * 60.0, end_hour * 60.0
    if start <= end:
        in_window = start <= minute < end
    else:  # window wraps past midnight, e.g. 22:00 -> 05:00
        in_window = minute >= start or minute < end
    return in_window or (end <= minute < end + after_minutes)


def _window_start_ts(now_ts: float, start_hour: float) -> float:
    midnight = _ist_now(now_ts).replace(hour=0, minute=0, second=0, microsecond=0)
    start_ts = midnight.timestamp() + start_hour * 3600.0
    while start_ts > now_ts:
        start_ts -= SECONDS_PER_DAY
    return start_ts


def run_leak_check(
    now_ts: float,
    repos: Repos,
    alert_store: AlertStore,
    notifier: Notifier,
    quiet_start_hour: float = 0.0,
    quiet_end_hour: float = 5.0,
    after_minutes: float = 30.0,
    abs_min_pct_per_hr: float = 0.8,
    cooldowns: Optional[Mapping[str, float]] = None,
) -> dict:
    """Fit tonight's loss rate per tank and alert on flagged leaks.

    A tank is evaluated only when ``now`` falls inside its quiet window
    (tank fields ``quietStartHour``/``quietEndHour``, else the arguments)
    or within ``after_minutes`` after it. Tanks without clean data are
    skipped. Returns a summary dict.
    """
    summary: dict = {"checked": 0, "leaks": [], "skipped": {}, "errors": {}}
    today_str = _ist_now(now_ts).strftime("%Y-%m-%d")
    for tank in repos.tanks.list_tanks():
        tank_id = tank.get("tankId", tank.get("tank_id"))
        if not tank_id:
            continue
        try:
            start_hour = _num(
                tank.get("quietStartHour", tank.get("quiet_start_hour")),
                quiet_start_hour,
            )
            end_hour = _num(
                tank.get("quietEndHour", tank.get("quiet_end_hour")),
                quiet_end_hour,
            )
            if not _in_quiet_window(now_ts, start_hour, end_hour, after_minutes):
                summary["skipped"][tank_id] = "outside_window"
                continue
            readings = repos.readings.query_readings(
                tank_id, _window_start_ts(now_ts, start_hour), now_ts
            )
            tonight = leak_detection.nightly_rate(readings)
            if tonight is None:
                summary["skipped"][tank_id] = "no_clean_data"
                continue
            past = repos.daily.get_recent_nightly_rates(tank_id, today_str, n=7)
            capacity = _num(
                tank.get("capacityL", tank.get("capacity_l")), DEFAULT_CAPACITY_L
            )
            tariff = _num(
                tank.get("tariffPerKL", tank.get("tariff_per_kl")),
                DEFAULT_TARIFF_PER_KL,
            )
            evaluation = leak_detection.evaluate_leak(
                tonight, past, abs_min_pct_per_hr, capacity, tariff
            )
            summary["checked"] += 1
            if not evaluation.get("leak"):
                continue
            alert = leak_alert_from_evaluation(_tank_view(tank, tank_id), evaluation)
            if alert is None:
                continue
            result = process_alert(alert, alert_store, notifier, now_ts, cooldowns)
            summary["leaks"].append(
                {"tank_id": tank_id, "sent": result["sent"], "error": result["error"]}
            )
        except Exception as exc:  # one bad tank must not sink the run
            logger.exception("Leak check failed for tank %s", tank_id)
            summary["errors"][tank_id] = f"{type(exc).__name__}: {exc}"
    return summary


def run_offline_check(
    now_ts: float,
    repos: Repos,
    alert_store: AlertStore,
    notifier: Notifier,
    default_interval_sec: float = 300.0,
    missed_intervals: int = 3,
    cooldowns: Optional[Mapping[str, float]] = None,
) -> dict:
    """Flag silent tanks offline and raise DEVICE_OFFLINE through cooldowns."""
    summary: dict = {"checked": 0, "offline": [], "errors": {}}
    for tank in repos.tanks.list_tanks():
        tank_id = tank.get("tankId", tank.get("tank_id"))
        if not tank_id:
            continue
        try:
            summary["checked"] += 1
            interval = _num(
                tank.get("reportIntervalSec", tank.get("report_interval_sec")),
                default_interval_sec,
            )
            last_ts = tank.get("lastTs", tank.get("last_ts"))
            alert = evaluate_offline(last_ts, now_ts, interval, missed_intervals)
            if alert is None:
                continue
            alert["tank_id"] = tank_id
            alert["tank_name"] = tank.get("name", tank.get("tankName", tank_id))
            result = process_alert(alert, alert_store, notifier, now_ts, cooldowns)
            repos.tanks.update_latest(tank_id, {"status": "offline"})
            summary["offline"].append(
                {"tank_id": tank_id, "sent": result["sent"], "error": result["error"]}
            )
        except Exception as exc:
            logger.exception("Offline check failed for tank %s", tank_id)
            summary["errors"][tank_id] = f"{type(exc).__name__}: {exc}"
    return summary


def run_daily_stats(
    date_str: Optional[str],
    repos: Repos,
    abs_min_pct_per_hr: float = 0.8,
    noise_threshold_pct: float = 0.5,
    now_ts: Optional[float] = None,
) -> dict:
    """Compute and store one day of stats per tank (default: yesterday IST).

    The night's rate is re-fitted here so the record carries
    ``nightlyRatePctPerHr`` and ``nightLeakFlagged``; flagged nights are
    then excluded from future baselines by ``get_recent_nightly_rates``.
    Re-running a date overwrites the same ``(tankId, date)`` items.
    """
    if date_str is None:
        date_str = (
            _ist_now(time.time() if now_ts is None else now_ts)
            - timedelta(days=1)
        ).strftime("%Y-%m-%d")
    start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=IST).timestamp()
    end = start + SECONDS_PER_DAY
    summary: dict = {"date": date_str, "tanks": [], "errors": {}}
    for tank in repos.tanks.list_tanks():
        tank_id = tank.get("tankId", tank.get("tank_id"))
        if not tank_id:
            continue
        try:
            readings = repos.readings.query_readings(tank_id, start, end - 1)
            events = repos.events.query_events(tank_id, start, end - 1)
            tonight = leak_detection.nightly_rate(readings)
            evaluations = []
            evaluation = None
            if tonight is not None:
                past = repos.daily.get_recent_nightly_rates(tank_id, date_str, n=7)
                capacity = _num(
                    tank.get("capacityL", tank.get("capacity_l")),
                    DEFAULT_CAPACITY_L,
                )
                tariff = _num(
                    tank.get("tariffPerKL", tank.get("tariff_per_kl")),
                    DEFAULT_TARIFF_PER_KL,
                )
                evaluation = leak_detection.evaluate_leak(
                    tonight, past, abs_min_pct_per_hr, capacity, tariff
                )
                evaluation["date"] = date_str
                evaluations.append(evaluation)
            stats = compute_daily(
                readings, events, evaluations, tank, date_str, noise_threshold_pct
            )
            rate = evaluation["rate"] if evaluation else None
            record = {
                **stats,
                "tankId": tank_id,
                "date": date_str,
                "nightlyRatePctPerHr": rate,
                "nightLeakFlagged": bool(evaluation and evaluation.get("leak")),
            }
            repos.daily.put_daily(record)
            summary["tanks"].append(tank_id)
        except Exception as exc:
            logger.exception("Daily stats failed for tank %s on %s", tank_id, date_str)
            summary["errors"][tank_id] = f"{type(exc).__name__}: {exc}"
    return summary
