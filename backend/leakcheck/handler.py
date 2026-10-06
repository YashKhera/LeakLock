"""Scheduled leak-check Lambda. Thin: builds deps, calls the job."""

import os
import time

import boto3

from backend.shared.jobs import Repos, run_leak_check
from backend.shared.notifier import SnsNotifier
from backend.shared.repository import (
    DailyRepo,
    DynamoAlertStore,
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
    topic_arn = os.environ.get("LEAKLOCK_ALERTS_TOPIC_ARN", "")
    notifier = SnsNotifier(topic_arn, boto3.client("sns"))
    return run_leak_check(
        time.time(),
        repos,
        DynamoAlertStore(dynamodb.Table(names.alerts)),
        notifier,
        quiet_start_hour=float(os.environ.get("LEAKLOCK_QUIET_START_HOUR", "0")),
        quiet_end_hour=float(os.environ.get("LEAKLOCK_QUIET_END_HOUR", "5")),
        abs_min_pct_per_hr=float(os.environ.get("LEAKLOCK_ABS_MIN_LEAK_RATE", "0.8")),
    )
