"""Scheduled offline-check Lambda. Thin: builds deps, calls the job."""

import os
import time

import boto3

from backend.shared.jobs import Repos, run_offline_check
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
    return run_offline_check(
        time.time(),
        repos,
        DynamoAlertStore(dynamodb.Table(names.alerts)),
        notifier,
        default_interval_sec=float(os.environ.get("LEAKLOCK_REPORT_INTERVAL_SEC", "300")),
        missed_intervals=int(os.environ.get("LEAKLOCK_MISSED_INTERVALS", "3")),
    )
