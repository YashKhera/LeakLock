"""DynamoDB repositories for the ingest path (and later Lambdas).

Thin wrappers over boto3 Table resources. Reads decode DynamoDB
``Decimal`` values back to ``int``/``float`` so callers work with plain
Python. Table names come from :class:`TableNames`, which reads optional
``LEAKLOCK_*_TABLE`` environment variables with ``leaklock-`` defaults.
"""

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Optional
from uuid import uuid4

from boto3.dynamodb.conditions import Key

from backend.shared.alerts import AlertStore

UNKNOWN_TANK = "UNKNOWN"


def _encode(value: Any) -> Any:
    """Convert plain Python floats to Decimal for DynamoDB writes."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


@dataclass
class TableNames:
    """Logical table names; override any via environment."""

    tanks: str = "leaklock-tanks"
    readings: str = "leaklock-readings"
    alerts: str = "leaklock-alerts"
    daily: str = "leaklock-daily"

    @classmethod
    def from_env(cls) -> "TableNames":
        return cls(
            tanks=os.environ.get("LEAKLOCK_TANKS_TABLE", cls.tanks),
            readings=os.environ.get("LEAKLOCK_READINGS_TABLE", cls.readings),
            alerts=os.environ.get("LEAKLOCK_ALERTS_TABLE", cls.alerts),
            daily=os.environ.get("LEAKLOCK_DAILY_TABLE", cls.daily),
        )


class TanksRepo:
    """Tank config + latest-telemetry fields, keyed by ``tankId``."""

    def __init__(self, table: Any) -> None:
        self.table = table

    def get_tank(self, tank_id: str) -> Optional[dict]:
        response = self.table.get_item(Key={"tankId": tank_id})
        item = response.get("Item")
        return _decode(item) if item is not None else None

    def put_tank(self, tank: Mapping[str, Any]) -> None:
        if not tank.get("tankId"):
            raise ValueError("tank needs a tankId")
        self.table.put_item(Item=_encode(dict(tank)))

    def update_latest(self, tank_id: str, fields: Mapping[str, Any]) -> None:
        if not fields:
            return
        names = {f"#f{i}": key for i, key in enumerate(fields)}
        values = {f":v{i}": _encode(value) for i, value in enumerate(fields.values())}
        expression = "SET " + ", ".join(
            f"{name} = {val}" for name, val in zip(names, values)
        )
        self.table.update_item(
            Key={"tankId": tank_id},
            UpdateExpression=expression,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )


class ReadingsRepo:
    """Telemetry rows keyed by ``(tankId, ts)``; duplicates overwrite."""

    def __init__(self, table: Any) -> None:
        self.table = table

    def put_reading(self, reading: Mapping[str, Any]) -> None:
        if not reading.get("tankId"):
            raise ValueError("reading needs a tankId")
        ts = reading.get("ts")
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            raise ValueError("reading needs a numeric ts")
        self.table.put_item(Item=_encode(dict(reading)))

    def query_readings(
        self, tank_id: str, from_ts: float, to_ts: float
    ) -> list[dict]:
        response = self.table.query(
            KeyConditionExpression=Key("tankId").eq(tank_id)
            & Key("ts").between(_encode(from_ts), _encode(to_ts))
        )
        return [_decode(item) for item in response.get("Items", [])]


class DynamoAlertStore(AlertStore):
    """AlertStore on the alerts table.

    Alert records land under ``SK = ALERT#<ts>#<type>#<rand>``. Cooldown
    markers land under ``SK = COOLDOWN#<type>`` with a TTL so stale
    markers disappear on their own.
    """

    def __init__(self, table: Any, cooldown_ttl_days: int = 30) -> None:
        self.table = table
        self.cooldown_ttl_days = cooldown_ttl_days

    @staticmethod
    def _cooldown_sk(alert_type: str) -> str:
        return f"COOLDOWN#{alert_type}"

    def put_alert(self, alert: Mapping[str, Any]) -> None:
        tank_id = alert.get("tank_id") or UNKNOWN_TANK
        ts = alert.get("ts")
        ts_part = "no-ts" if ts is None else str(ts)
        item: dict[str, Any] = {
            "tankId": tank_id,
            "sk": f"ALERT#{ts_part}#{alert.get('alert_type')}#{uuid4().hex[:8]}",
            "alertType": alert.get("alert_type"),
            "severity": alert.get("severity"),
            "tankName": alert.get("tank_name"),
            "details": dict(alert.get("details") or {}),
            "notified": bool(alert.get("notified", False)),
        }
        for key in ("ts", "processed_ts", "processedTs"):
            if alert.get(key) is not None:
                item["ts" if key == "ts" else "processedTs"] = alert[key]
        if alert.get("notify_error") is not None:
            item["notifyError"] = alert["notify_error"]
        self.table.put_item(Item=_encode(item))

    def get_last_sent(
        self, alert_type: str, tank_id: Optional[str]
    ) -> Optional[float]:
        response = self.table.get_item(
            Key={
                "tankId": tank_id or UNKNOWN_TANK,
                "sk": self._cooldown_sk(alert_type),
            }
        )
        item = response.get("Item")
        if item is None or item.get("lastSentTs") is None:
            return None
        return _decode(item["lastSentTs"])

    def set_last_sent(
        self, alert_type: str, tank_id: Optional[str], ts: float
    ) -> None:
        self.table.put_item(
            Item=_encode(
                {
                    "tankId": tank_id or UNKNOWN_TANK,
                    "sk": self._cooldown_sk(alert_type),
                    "alertType": alert_type,
                    "lastSentTs": ts,
                    "expiresAt": int(ts) + self.cooldown_ttl_days * 86400,
                }
            )
        )
