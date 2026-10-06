"""AWS Lambda entry point for the ingest path. No logic lives here.

Builds the repos from environment variables and delegates every message
to :func:`backend.shared.ingest_logic.process_telemetry`, so the exact
same code runs locally (moto) and in Lambda. Delivery (SNS) comes later;
until then notifications go to the Lambda logs via a logging notifier.
"""

import logging
import os
import time

import boto3

from backend.shared.alerts import Notifier
from backend.shared.ingest_logic import (
    DEFAULT_RETENTION_DAYS,
    process_telemetry,
)
from backend.shared.repository import (
    DynamoAlertStore,
    ReadingsRepo,
    TableNames,
    TanksRepo,
)

logger = logging.getLogger(__name__)


class LoggingNotifier(Notifier):
    """Placeholder Notifier until the SNS version lands."""

    def send(self, subject: str, body: str) -> None:
        logger.warning("ALERT (not delivered, SNS pending) subject=%r", subject)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return float(raw)


def lambda_handler(event: object, context: object) -> dict:
    """IoT rule deliveries arrive as one message dict or a list of them."""
    names = TableNames.from_env()
    dynamodb = boto3.resource("dynamodb")
    tanks_repo = TanksRepo(dynamodb.Table(names.tanks))
    readings_repo = ReadingsRepo(dynamodb.Table(names.readings))
    alert_store = DynamoAlertStore(dynamodb.Table(names.alerts))
    notifier = LoggingNotifier()

    retention_days = int(_env_float("LEAKLOCK_RETENTION_DAYS", DEFAULT_RETENTION_DAYS))
    overflow_threshold = _env_float("LEAKLOCK_OVERFLOW_THRESHOLD_PCT", 98.0)

    if isinstance(event, list):
        messages = event
    else:
        messages = [event]
    now_ts = time.time()
    results = [
        process_telemetry(
            message,
            tanks_repo,
            readings_repo,
            alert_store,
            notifier,
            now_ts,
            retention_days=retention_days,
            overflow_threshold_pct=overflow_threshold,
        )
        for message in messages
    ]
    return {
        "processed": len(results),
        "stored": sum(1 for r in results if r.get("stored")),
    }
