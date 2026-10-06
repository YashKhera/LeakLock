"""Scheduled daily-stats Lambda. Thin: builds deps, calls the job.

The EventBridge event may carry ``{"date": "YYYY-MM-DD"}`` to backfill;
otherwise yesterday in IST is computed.
"""

import boto3

from backend.shared.jobs import Repos, run_daily_stats
from backend.shared.repository import (
    DailyRepo,
    EventsRepo,
    ReadingsRepo,
    TableNames,
    TanksRepo,
)


def lambda_handler(event: object, context: object) -> dict:
    names = TableNames.from_env()
    dynamodb = boto3.resource("dynamodb")
    repos = Repos(
        tanks=TanksRepo(dynamodb.Table(names.tanks)),
        readings=ReadingsRepo(dynamodb.Table(names.readings)),
        events=EventsRepo(dynamodb.Table(names.alerts)),
        daily=DailyRepo(dynamodb.Table(names.daily)),
    )
    date_str = event.get("date") if isinstance(event, dict) else None
    return run_daily_stats(date_str, repos)
