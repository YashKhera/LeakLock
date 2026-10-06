"""Device-event ingest Lambda. Thin: builds deps, calls process_event."""

import time

import boto3

from backend.shared.ingest_logic import process_event
from backend.shared.repository import EventsRepo, TableNames, TanksRepo


def lambda_handler(event: object, context: object) -> dict:
    """IoT rule deliveries arrive as one event dict or a list of them."""
    names = TableNames.from_env()
    dynamodb = boto3.resource("dynamodb")
    tanks_repo = TanksRepo(dynamodb.Table(names.tanks))
    events_repo = EventsRepo(dynamodb.Table(names.alerts))
    messages = event if isinstance(event, list) else [event]
    now_ts = time.time()
    results = [
        process_event(message, tanks_repo, events_repo, now_ts)
        for message in messages
    ]
    return {
        "processed": len(results),
        "stored": sum(1 for r in results if r.get("stored")),
    }
