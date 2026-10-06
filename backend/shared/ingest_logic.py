"""Ingest-path business logic: shared by the Lambda handler and local runs.

:func:`process_telemetry` validates one device message (section 6.2 shape),
resolves it against the tank registry, derives level/volume with
:mod:`backend.shared.level`, stores the reading with a TTL, refreshes the
tank's latest fields, and raises an overflow alert when warranted. It never
raises on bad messages: problems come back in the result dict.
"""

import logging
import math
from typing import Any, Mapping, Optional

from backend.shared import level as level_mod
from backend.shared.alerts import (
    AlertStore,
    Notifier,
    evaluate_overflow_risk,
    process_alert,
)

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 14
DEFAULT_MARGIN_CM = 10.0


def _tank_num(tank: Mapping[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        if key in tank and tank[key] is not None:
            value = tank[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            if math.isnan(value) or not math.isfinite(value):
                return None
            return float(value)
    return None


def _validate_message(message: Any, now_ts: float) -> tuple[Optional[dict], Optional[str]]:
    """Return (fields, None) or (None, error_detail)."""
    if not isinstance(message, dict):
        return None, "message must be a JSON object"
    tank_id = message.get("tankId", message.get("tank_id"))
    if not isinstance(tank_id, str) or not tank_id:
        return None, "tankId is required and must be a non-empty string"

    ts = message.get("ts", message.get("timestamp", now_ts))
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None, "ts must be a number"
    if math.isnan(ts) or not math.isfinite(ts):
        return None, "ts must be finite"
    ts = float(ts)

    pump_on = message.get("pumpOn", message.get("pump_on", False))
    if not isinstance(pump_on, bool):
        return None, "pumpOn must be a boolean"

    distance = message.get("distanceCm", message.get("distance_cm"))
    fault_flag = bool(message.get("fault", False))
    return (
        {
            "tank_id": tank_id,
            "ts": ts,
            "pump_on": pump_on,
            "distance_cm": distance,
            "fault_flag": fault_flag,
        },
        None,
    )


def process_telemetry(
    message: Mapping[str, Any],
    tanks_repo: Any,
    readings_repo: Any,
    alert_store: AlertStore,
    notifier: Notifier,
    now_ts: float,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    overflow_threshold_pct: float = 98.0,
    cooldowns: Optional[Mapping[str, float]] = None,
) -> dict:
    """Validate, store and react to one telemetry message.

    Returns a result dict; ``ok`` is False for rejected/unknown-tank
    messages. Faulty readings (device fault flag, missing/invalid
    distance, missing tank calibration) are stored with null level
    fields and a non-empty ``faults`` list.
    """
    fields, error = _validate_message(message, float(now_ts))
    if fields is None:
        logger.warning("Dropping invalid telemetry: %s", error)
        return {"ok": False, "reason": "invalid_message", "detail": error}

    tank_id = fields["tank_id"]
    ts = fields["ts"]
    tank = tanks_repo.get_tank(tank_id)
    if tank is None:
        logger.warning("Dropping telemetry for unknown tankId %r", tank_id)
        return {"ok": False, "reason": "unknown_tank", "tank_id": tank_id}

    faults: list[str] = []
    if fields["fault_flag"]:
        faults.append("device_fault")

    d_empty = _tank_num(tank, "dEmptyCm", "d_empty_cm")
    d_full = _tank_num(tank, "dFullCm", "d_full_cm")
    capacity_l = _tank_num(tank, "capacityL", "capacity_l")
    margin_cm = _tank_num(tank, "marginCm", "margin_cm")
    if margin_cm is None:
        margin_cm = DEFAULT_MARGIN_CM

    distance = fields["distance_cm"]
    if distance is None:
        faults.append("missing_distance")
    if d_empty is None or d_full is None or capacity_l is None:
        faults.append("missing_calibration")

    level_pct: Optional[float] = None
    volume_l: Optional[float] = None
    if not faults:
        # A clean envelope: the distance decides. Anything else leaves the
        # level fields null so downstream code never trusts a bad reading.
        if not level_mod.is_valid_distance(distance, d_empty, d_full, margin_cm):
            faults.append("invalid_distance")
        else:
            level_pct = level_mod.level_pct(distance, d_empty, d_full)
            volume_l = level_mod.volume_l(level_pct, capacity_l)

    reading = {
        "tankId": tank_id,
        "ts": ts,
        "distanceCm": distance if isinstance(distance, (int, float)) else None,
        "levelPct": level_pct,
        "volumeL": volume_l,
        "pumpOn": fields["pump_on"],
        "faults": faults,
        "expiresAt": int(ts) + retention_days * 86400,
    }
    readings_repo.put_reading(reading)

    last_ts = tank.get("lastTs", tank.get("last_ts"))
    latest_updated = (
        last_ts is None
        or isinstance(last_ts, bool)
        or not isinstance(last_ts, (int, float))
        or ts >= float(last_ts)
    )
    if latest_updated:
        tanks_repo.update_latest(
            tank_id,
            {
                "lastTs": ts,
                "lastDistanceCm": reading["distanceCm"],
                "lastLevelPct": level_pct,
                "lastVolumeL": volume_l,
                "pumpOn": fields["pump_on"],
                "status": "FAULT" if faults else "OK",
            },
        )

    alert_result: Optional[dict] = None
    if level_pct is not None:
        # alerts.py speaks snake_case (tank_id/name); the tank registry
        # speaks camelCase (tankId) — translate at this boundary.
        tank_view = {
            "tank_id": tank_id,
            "name": tank.get("name", tank.get("tankName", tank_id)),
        }
        alert = evaluate_overflow_risk(
            {"ts": ts, "levelPct": level_pct, "pumpOn": fields["pump_on"]},
            tank_view,
            level_threshold_pct=overflow_threshold_pct,
        )
        if alert is not None:
            alert_result = process_alert(
                alert, alert_store, notifier, now_ts, cooldowns
            )

    return {
        "ok": True,
        "stored": True,
        "tank_id": tank_id,
        "ts": ts,
        "faults": faults,
        "latest_updated": latest_updated,
        "alert": alert_result,
    }


def process_event(
    message: Mapping[str, Any],
    tanks_repo: Any,
    events_repo: Any,
    now_ts: float,
) -> dict:
    """Validate and store one device event message (section 6.3 shape).

    Unknown tankIds are logged and dropped, like telemetry. Never raises
    on bad messages: problems come back in the result dict.
    """
    if not isinstance(message, dict):
        return {"ok": False, "reason": "invalid_message", "detail": "not an object"}
    tank_id = message.get("tankId", message.get("tank_id"))
    if not isinstance(tank_id, str) or not tank_id:
        return {"ok": False, "reason": "invalid_message", "detail": "tankId required"}
    if tanks_repo.get_tank(tank_id) is None:
        logger.warning("Dropping event for unknown tankId %r", tank_id)
        return {"ok": False, "reason": "unknown_tank", "tank_id": tank_id}

    event_type = message.get("eventType", message.get("type"))
    if not isinstance(event_type, str) or not event_type:
        return {"ok": False, "reason": "invalid_message", "detail": "type required"}

    ts = message.get("ts", message.get("timestamp", now_ts))
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return {"ok": False, "reason": "invalid_message", "detail": "ts not numeric"}
    if math.isnan(ts) or not math.isfinite(ts):
        return {"ok": False, "reason": "invalid_message", "detail": "ts not finite"}

    event = {
        "tankId": tank_id,
        "ts": float(ts),
        "eventType": event_type,
    }
    for key in ("reason", "cause", "pumpOn", "pump_on", "levelPct", "level_pct"):
        if message.get(key) is not None:
            event[key] = message[key]
    events_repo.put_event(event)
    return {"ok": True, "stored": True, "tank_id": tank_id, "event_type": event_type}
