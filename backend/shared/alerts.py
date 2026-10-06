"""Alert rules, cooldowns, messages and dispatch for LeakLock.

Pure Python, no AWS. Rule functions evaluate telemetry and return an alert
dict (or None when nothing is wrong); :func:`process_alert` records the
alert via an :class:`AlertStore` and sends it via a :class:`Notifier`
subject to per-type cooldowns. DynamoDB/SNS implementations come later;
tests use :class:`InMemoryAlertStore` and :class:`FakeNotifier`.

Alert dict shape::

    {"alert_type": ..., "severity": ..., "tank_id": ..., "tank_name": ...,
     "ts": <epoch seconds or None>, "details": {...numbers per type...}}

Tank dicts carry ``tank_id`` (or ``id``) and ``name`` (or ``tank_name``).
"""

import logging
import math
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, NamedTuple, Optional, Protocol

logger = logging.getLogger(__name__)

OVERFLOW_RISK = "OVERFLOW_RISK"
LEAK_SUSPECTED = "LEAK_SUSPECTED"
DEVICE_OFFLINE = "DEVICE_OFFLINE"
SENSOR_FAULT = "SENSOR_FAULT"

ALERT_TYPES = (OVERFLOW_RISK, LEAK_SUSPECTED, DEVICE_OFFLINE, SENSOR_FAULT)

SEVERITIES = {
    OVERFLOW_RISK: "critical",
    LEAK_SUSPECTED: "warning",
    DEVICE_OFFLINE: "warning",
    SENSOR_FAULT: "warning",
}

# Default resend cooldowns, in seconds.
DEFAULT_COOLDOWNS = {
    OVERFLOW_RISK: 30 * 60,
    LEAK_SUSPECTED: 12 * 3600,
    DEVICE_OFFLINE: 60 * 60,
    SENSOR_FAULT: 60 * 60,
}


class EmailMessage(NamedTuple):
    subject: str
    body: str


class AlertStore(Protocol):
    """Persistence for alert records and per-type last-sent timestamps."""

    def put_alert(self, alert: Mapping[str, Any]) -> None:
        """Record an alert (every evaluation, even if sending is skipped)."""
        ...

    def get_last_sent(
        self, alert_type: str, tank_id: Optional[str]
    ) -> Optional[float]:
        """Epoch seconds of the last sent alert, or None if never sent."""
        ...

    def set_last_sent(
        self, alert_type: str, tank_id: Optional[str], ts: float
    ) -> None:
        """Remember that an alert was sent now."""
        ...


class Notifier(Protocol):
    """Email (later: SNS) delivery."""

    def send(self, subject: str, body: str) -> None:
        """Deliver one message; may raise on failure."""
        ...


class InMemoryAlertStore:
    """Test/throwaway AlertStore: keeps everything in lists and dicts."""

    def __init__(self) -> None:
        self.alerts: list[dict] = []
        self._last_sent: dict[tuple[str, Optional[str]], float] = {}

    def put_alert(self, alert: Mapping[str, Any]) -> None:
        self.alerts.append(dict(alert))

    def get_last_sent(
        self, alert_type: str, tank_id: Optional[str]
    ) -> Optional[float]:
        return self._last_sent.get((alert_type, tank_id))

    def set_last_sent(
        self, alert_type: str, tank_id: Optional[str], ts: float
    ) -> None:
        self._last_sent[(alert_type, tank_id)] = float(ts)


class FakeNotifier:
    """Test Notifier: records sent mail, optionally fails."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[str, str]] = []

    def send(self, subject: str, body: str) -> None:
        if self.fail:
            raise RuntimeError("simulated notifier failure")
        self.sent.append((subject, body))


def _tank_id(tank: Mapping[str, Any]) -> Optional[str]:
    return tank.get("tank_id", tank.get("id"))


def _tank_name(tank: Mapping[str, Any]) -> str:
    name = tank.get("name", tank.get("tank_name"))
    if name:
        return str(name)
    tank_id = _tank_id(tank)
    return str(tank_id) if tank_id else "Unnamed tank"


def _make_alert(
    alert_type: str,
    tank: Mapping[str, Any],
    ts: Optional[float],
    details: dict,
) -> dict:
    return {
        "alert_type": alert_type,
        "severity": SEVERITIES[alert_type],
        "tank_id": _tank_id(tank),
        "tank_name": _tank_name(tank),
        "ts": ts,
        "details": details,
    }


def _reading_level(reading: Any) -> Optional[float]:
    if not isinstance(reading, dict):
        return None
    level = reading.get("levelPct", reading.get("level_pct"))
    if isinstance(level, bool) or not isinstance(level, (int, float)):
        return None
    if math.isnan(level) or not math.isfinite(level):
        return None
    return float(level)


def _reading_has_fault(reading: Any) -> bool:
    if not isinstance(reading, dict):
        return True
    if bool(reading.get("fault", False)):
        return True
    level = _reading_level(reading)
    return level is None or not 0.0 <= level <= 100.0


def evaluate_overflow_risk(
    reading: Mapping[str, Any],
    tank: Mapping[str, Any],
    level_threshold_pct: float = 98.0,
) -> Optional[dict]:
    """OVERFLOW_RISK when the tank is at/above the threshold and pumping."""
    if not isinstance(reading, dict):
        return None
    level = _reading_level(reading)
    if level is None:
        return None
    pump_on = bool(reading.get("pumpOn", reading.get("pump_on", False)))
    if level >= level_threshold_pct and pump_on:
        ts = reading.get("ts", reading.get("timestamp"))
        return _make_alert(
            OVERFLOW_RISK,
            tank,
            float(ts) if isinstance(ts, (int, float)) else None,
            {"level_pct": level, "pump_on": True},
        )
    return None


def evaluate_sensor_fault(
    recent_readings: Iterable[Mapping[str, Any]], consecutive: int = 3
) -> Optional[dict]:
    """SENSOR_FAULT when the last ``consecutive`` readings all have faults.

    A reading counts as faulty with an explicit ``fault`` flag or a
    missing/NaN/out-of-range level. Fewer than ``consecutive`` readings is
    not enough evidence: returns None.
    """
    readings = list(recent_readings)
    if consecutive < 1:
        raise ValueError(f"consecutive must be >= 1, got {consecutive}")
    if len(readings) < consecutive:
        return None
    window = readings[-consecutive:]
    if not all(_reading_has_fault(r) for r in window):
        return None
    last_ts = window[-1].get("ts", window[-1].get("timestamp")) if isinstance(
        window[-1], dict
    ) else None
    return _make_alert(
        SENSOR_FAULT,
        {"tank_id": None, "name": None},
        float(last_ts) if isinstance(last_ts, (int, float)) else None,
        {"consecutive_faults": consecutive},
    )


def evaluate_offline(
    last_ts: Optional[float],
    now_ts: float,
    interval_sec: float,
    missed_intervals: int = 3,
) -> Optional[dict]:
    """DEVICE_OFFLINE when silence stretches past the missed-interval budget.

    Triggers when ``now_ts - last_ts >= missed_intervals * interval_sec``.
    A ``last_ts`` of None (never heard from) also triggers.
    """
    if interval_sec <= 0:
        raise ValueError(f"interval_sec must be > 0, got {interval_sec}")
    if missed_intervals < 1:
        raise ValueError(f"missed_intervals must be >= 1, got {missed_intervals}")
    if last_ts is None:
        return _make_alert(
            DEVICE_OFFLINE,
            {"tank_id": None, "name": None},
            now_ts,
            {"last_ts": None, "gap_sec": None, "missed_intervals": missed_intervals},
        )
    gap = now_ts - last_ts
    if gap >= missed_intervals * interval_sec:
        return _make_alert(
            DEVICE_OFFLINE,
            {"tank_id": None, "name": None},
            now_ts,
            {"last_ts": last_ts, "gap_sec": gap, "missed_intervals": missed_intervals},
        )
    return None


def leak_alert_from_evaluation(
    tank: Mapping[str, Any], evaluation: Optional[Mapping[str, Any]]
) -> Optional[dict]:
    """Build LEAK_SUSPECTED from ``leak_detection.evaluate_leak`` output."""
    if not evaluation or not evaluation.get("leak"):
        return None

    def _num(key: str) -> float:
        value = evaluation.get(key, 0.0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0.0
        if math.isnan(value) or not math.isfinite(value):
            return 0.0
        return float(value)

    return _make_alert(
        LEAK_SUSPECTED,
        tank,
        None,
        {
            "rate_pct_per_hr": _num("rate"),
            "r_squared": _num("r_squared"),
            "hours_observed": _num("hours_observed"),
            "threshold": _num("threshold"),
            "confidence": str(evaluation.get("confidence", "low")),
            "estimated_loss_l": _num("estimated_loss_l"),
            "estimated_rupees": _num("estimated_rupees"),
        },
    )


def _cooldown_for(
    alert_type: str, cooldowns: Optional[Mapping[str, float]]
) -> float:
    merged = dict(DEFAULT_COOLDOWNS)
    if cooldowns:
        merged.update(cooldowns)
    if alert_type not in merged:
        raise ValueError(f"unknown alert_type: {alert_type!r}")
    return float(merged[alert_type])


def should_send(
    alert_type: str,
    last_sent_ts: Optional[float],
    now_ts: float,
    cooldowns: Optional[Mapping[str, float]] = None,
) -> bool:
    """True if enough time passed since the last send (or never sent)."""
    cooldown = _cooldown_for(alert_type, cooldowns)
    if last_sent_ts is None:
        return True
    return (now_ts - last_sent_ts) >= cooldown


def _fmt_ts(ts: Optional[float]) -> str:
    if ts is None or isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return "unknown time"
    if math.isnan(ts) or not math.isfinite(ts):
        return "unknown time"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


_ACTIONS = {
    OVERFLOW_RISK: "Switch the pump off now and check the tank's auto cut-off switch.",
    LEAK_SUSPECTED: (
        "Inspect the tank outlet valve and overflow pipe for dripping, "
        "then re-check tomorrow's night report."
    ),
    DEVICE_OFFLINE: (
        "Check the device power supply and Wi-Fi connection, "
        "then confirm readings resume on the dashboard."
    ),
    SENSOR_FAULT: (
        "Wipe the ultrasonic sensor face clean and check its wiring "
        "for looseness or moisture."
    ),
}


def build_message(alert: Mapping[str, Any], tank: Mapping[str, Any]) -> EmailMessage:
    """Subject + plain-language body naming the tank, numbers and one action."""
    alert_type = alert.get("alert_type", "UNKNOWN")
    name = _tank_name(tank)
    details = alert.get("details") or {}
    action = _ACTIONS.get(
        str(alert_type),
        "Check the tank on the dashboard and investigate.",
    )
    if alert_type == OVERFLOW_RISK:
        level = details.get("level_pct")
        subject = f"CRITICAL: {name} may overflow - pump still ON at {level:.1f}%"
        body = (
            f"Tank {name} is at {level:.1f}% full and the pump is still running. "
            f"Overflow is imminent.\nSuggested action: {action}"
        )
    elif alert_type == LEAK_SUSPECTED:
        subject = f"WARNING: suspected leak in {name}"
        body = (
            f"Tank {name} lost level at {details.get('rate_pct_per_hr', 0.0):.2f}% per hour "
            f"over {details.get('hours_observed', 0.0):.1f} hours last night "
            f"(confidence: {details.get('confidence', 'low')}). "
            f"Estimated loss: {details.get('estimated_loss_l', 0.0):.1f} litres "
            f"(about Rs {details.get('estimated_rupees', 0.0):.2f}).\n"
            f"Suggested action: {action}"
        )
    elif alert_type == DEVICE_OFFLINE:
        gap = details.get("gap_sec")
        gap_text = f"{gap / 60.0:.0f} minutes" if isinstance(gap, (int, float)) else "a long time"
        subject = f"WARNING: {name} device offline"
        body = (
            f"Tank {name} has not sent readings for {gap_text} "
            f"(last seen {_fmt_ts(details.get('last_ts'))}).\n"
            f"Suggested action: {action}"
        )
    elif alert_type == SENSOR_FAULT:
        subject = f"WARNING: {name} sensor fault"
        body = (
            f"Tank {name} reported {details.get('consecutive_faults', 0)} faulty "
            f"sensor readings in a row, so its level cannot be trusted.\n"
            f"Suggested action: {action}"
        )
    else:
        subject = f"Alert for {name}: {alert_type}"
        body = f"Tank {name} raised {alert_type} with details {details}.\nSuggested action: {action}"
    return EmailMessage(subject=subject, body=body)


def process_alert(
    alert: Mapping[str, Any],
    store: AlertStore,
    notifier: Notifier,
    now_ts: float,
    cooldowns: Optional[Mapping[str, float]] = None,
) -> dict:
    """Record the alert, then send it only if the cooldown allows.

    The alert is always recorded, even when sending is suppressed. A
    failing notifier never raises: the failure is logged, the record is
    marked, and the last-sent timestamp is left untouched so a later run
    can retry. Returns ``{"sent": bool, "error": str | None}``.
    """
    alert_type = str(alert.get("alert_type"))
    tank_id = alert.get("tank_id")
    record = dict(alert)
    record["processed_ts"] = now_ts
    record["notified"] = False
    record["notify_error"] = None

    sent = False
    error: Optional[str] = None
    if should_send(alert_type, store.get_last_sent(alert_type, tank_id), now_ts, cooldowns):
        tank = {"tank_id": tank_id, "name": alert.get("tank_name")}
        message = build_message(alert, tank)
        try:
            notifier.send(message.subject, message.body)
            sent = True
            record["notified"] = True
            store.set_last_sent(alert_type, tank_id, now_ts)
        except Exception as exc:  # never let delivery break the pipeline
            error = f"{type(exc).__name__}: {exc}"
            record["notify_error"] = error
            logger.exception("Notifier failed for %s alert on tank %s", alert_type, tank_id)

    store.put_alert(record)
    return {"sent": sent, "error": error}
