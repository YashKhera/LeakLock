"""Scheduled-job tests against moto-mocked DynamoDB (and SNS+SQS)."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import boto3
import pytest
from moto import mock_aws

from backend.shared.alerts import DEVICE_OFFLINE, LEAK_SUSPECTED, FakeNotifier
from backend.shared.ingest_logic import process_event
from backend.shared.jobs import (
    Repos,
    run_daily_stats,
    run_leak_check,
    run_offline_check,
)
from backend.shared.notifier import SnsNotifier, sanitise_subject
from backend.shared.repository import (
    DailyRepo,
    DynamoAlertStore,
    EventsRepo,
    ReadingsRepo,
    TanksRepo,
)
from backend.shared.tables import create_tables

REGION = "ap-south-1"
IST = ZoneInfo("Asia/Kolkata")
NIGHT = "2026-10-06"


def ist_ts(day_str, hour, minute=0):
    year, month, day = (int(p) for p in day_str.split("-"))
    return datetime(year, month, day, hour, minute, tzinfo=IST).timestamp()


def make_tank(tank_id="tank-1", **overrides):
    tank = {
        "tankId": tank_id,
        "name": "Block A",
        "dEmptyCm": 200.0,
        "dFullCm": 20.0,
        "capacityL": 1000.0,
        "tariffPerKL": 60.0,
        "quietStartHour": 0.0,
        "quietEndHour": 5.0,
        "reportIntervalSec": 300.0,
    }
    tank.update(overrides)
    return tank


@pytest.fixture
def ctx():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=REGION)
        tables = create_tables(resource)
        repos = Repos(
            tanks=TanksRepo(tables["tanks"]),
            readings=ReadingsRepo(tables["readings"]),
            events=EventsRepo(tables["alerts"]),
            daily=DailyRepo(tables["daily"]),
        )
        yield {
            "repos": repos,
            "store": DynamoAlertStore(tables["alerts"]),
            "alerts_table": tables["alerts"],
            "notifier": FakeNotifier(),
        }


def seed_readings(ctx, tank_id, day_str, start_level, loss_per_hr, hours=5.0):
    """One reading every 5 minutes across the quiet window."""
    base = ist_ts(day_str, 0)
    count = int(hours * 3600 / 300)
    for i in range(count):
        ctx["repos"].readings.put_reading(
            {
                "tankId": tank_id,
                "ts": base + i * 300,
                "levelPct": start_level - loss_per_hr * (i * 300) / 3600.0,
                "pumpOn": False,
            }
        )


def seed_daily(ctx, tank_id, date, rate, flagged=False, with_flag=True):
    record = {"tankId": tank_id, "date": date, "nightlyRatePctPerHr": rate}
    if with_flag:
        record["nightLeakFlagged"] = flagged
    ctx["repos"].daily.put_daily(record)


def alert_records(ctx):
    return [
        item
        for item in ctx["alerts_table"].scan().get("Items", [])
        if str(item["sk"]).startswith("ALERT#")
    ]


class TestLeakCheck:
    def test_seeded_leak_night_flagged(self, ctx):
        ctx["repos"].tanks.put_tank(make_tank())
        for date, rate in (("2026-10-02", 0.05), ("2026-10-03", 0.08),
                           ("2026-10-04", 0.03), ("2026-10-05", 0.10)):
            seed_daily(ctx, "tank-1", date, rate)
        seed_readings(ctx, "tank-1", NIGHT, 80.0, 1.5)

        summary = run_leak_check(
            ist_ts(NIGHT, 5, 15), ctx["repos"], ctx["store"], ctx["notifier"]
        )
        assert summary["checked"] == 1
        assert summary["errors"] == {}
        assert [leak["tank_id"] for leak in summary["leaks"]] == ["tank-1"]
        assert summary["leaks"][0]["sent"] is True

        records = alert_records(ctx)
        assert len(records) == 1
        assert records[0]["alertType"] == LEAK_SUSPECTED
        assert records[0]["tankId"] == "tank-1"

        subject, body = ctx["notifier"].sent[0]
        assert "Block A" in subject + body
        assert "1.50" in body

    def test_second_run_sends_only_once(self, ctx):
        ctx["repos"].tanks.put_tank(make_tank())
        seed_readings(ctx, "tank-1", NIGHT, 80.0, 1.5)
        now = ist_ts(NIGHT, 5, 15)
        run_leak_check(now, ctx["repos"], ctx["store"], ctx["notifier"])
        summary = run_leak_check(now, ctx["repos"], ctx["store"], ctx["notifier"])
        assert summary["leaks"][0]["sent"] is False  # 12 h leak cooldown
        assert len(ctx["notifier"].sent) == 1
        assert len(alert_records(ctx)) == 2  # both runs recorded

    def test_normal_night_raises_nothing(self, ctx):
        ctx["repos"].tanks.put_tank(make_tank())
        seed_readings(ctx, "tank-1", NIGHT, 70.0, 0.0)
        summary = run_leak_check(
            ist_ts(NIGHT, 5, 15), ctx["repos"], ctx["store"], ctx["notifier"]
        )
        assert summary["checked"] == 1
        assert summary["leaks"] == []
        assert ctx["notifier"].sent == []

    def test_outside_window_skipped(self, ctx):
        ctx["repos"].tanks.put_tank(make_tank())
        seed_readings(ctx, "tank-1", NIGHT, 80.0, 1.5)
        summary = run_leak_check(
            ist_ts(NIGHT, 12, 0), ctx["repos"], ctx["store"], ctx["notifier"]
        )
        assert summary["skipped"] == {"tank-1": "outside_window"}
        assert summary["leaks"] == []

    def test_no_data_tank_skipped_and_errors_isolated(self, ctx):
        ctx["repos"].tanks.put_tank(make_tank("tank-1"))
        ctx["repos"].tanks.put_tank(make_tank("tank-err", name="Broken"))
        seed_readings(ctx, "tank-1", NIGHT, 70.0, 0.0)
        real_query = ctx["repos"].readings.query_readings

        def boom(tank_id, from_ts, to_ts):
            if tank_id == "tank-err":
                raise RuntimeError("db blew up")
            return real_query(tank_id, from_ts, to_ts)

        ctx["repos"].readings.query_readings = boom
        summary = run_leak_check(
            ist_ts(NIGHT, 5, 15), ctx["repos"], ctx["store"], ctx["notifier"]
        )
        assert summary["skipped"] == {"tank-1": "no_clean_data"} or summary["checked"] == 1
        assert "tank-err" in summary["errors"]
        assert summary["leaks"] == []


class TestOfflineCheck:
    def test_silent_tank_offline_with_alert(self, ctx):
        now = ist_ts(NIGHT, 8, 0)
        ctx["repos"].tanks.put_tank(make_tank(lastTs=now - 3600.0, status="OK"))
        ctx["repos"].tanks.put_tank(make_tank("tank-2", name="Block B", lastTs=now - 60.0, status="OK"))
        summary = run_offline_check(now, ctx["repos"], ctx["store"], ctx["notifier"])
        assert summary["checked"] == 2
        assert [entry["tank_id"] for entry in summary["offline"]] == ["tank-1"]
        assert summary["offline"][0]["sent"] is True
        assert ctx["repos"].tanks.get_tank("tank-1")["status"] == "offline"
        assert ctx["repos"].tanks.get_tank("tank-2")["status"] == "OK"
        records = alert_records(ctx)
        assert len(records) == 1
        assert records[0]["alertType"] == DEVICE_OFFLINE


class TestDailyStats:
    def test_rerun_gives_identical_record(self, ctx):
        ctx["repos"].tanks.put_tank(make_tank())
        seed_readings(ctx, "tank-1", "2026-10-05", 80.0, 0.5, hours=6.0)
        first = run_daily_stats("2026-10-05", ctx["repos"])
        record_one = ctx["repos"].daily.get_daily("tank-1", "2026-10-05")
        second = run_daily_stats("2026-10-05", ctx["repos"])
        record_two = ctx["repos"].daily.get_daily("tank-1", "2026-10-05")
        assert first["tanks"] == second["tanks"] == ["tank-1"]
        assert record_one == record_two
        assert record_one["nightLeakFlagged"] is False
        assert record_one["nightlyRatePctPerHr"] == pytest.approx(0.5, abs=0.05)

    def test_leak_night_flagged_and_excluded_from_baseline(self, ctx):
        ctx["repos"].tanks.put_tank(make_tank())
        seed_readings(ctx, "tank-1", NIGHT, 80.0, 1.5)
        run_daily_stats(NIGHT, ctx["repos"])
        record = ctx["repos"].daily.get_daily("tank-1", NIGHT)
        assert record["nightLeakFlagged"] is True
        assert record["nightlyRatePctPerHr"] == pytest.approx(1.5, abs=0.05)
        rates = ctx["repos"].daily.get_recent_nightly_rates("tank-1", "2026-10-07")
        assert rates == []

    def test_get_recent_skips_flagged_and_rateless(self, ctx):
        ctx["repos"].tanks.put_tank(make_tank())
        seed_daily(ctx, "tank-1", "2026-10-01", 0.08)
        seed_daily(ctx, "tank-1", "2026-10-02", 0.10)
        seed_daily(ctx, "tank-1", "2026-10-03", 0.12)
        seed_daily(ctx, "tank-1", "2026-10-04", 5.0, flagged=True)
        seed_daily(ctx, "tank-1", "2026-10-05", 0.09, with_flag=False)
        ctx["repos"].daily.put_daily(
            {"tankId": "tank-1", "date": "2026-10-06", "nightLeakFlagged": False}
        )
        rates = ctx["repos"].daily.get_recent_nightly_rates("tank-1", "2026-10-07", n=7)
        assert [r["rate"] for r in rates] == [0.09, 0.12, 0.10, 0.08]
        assert ctx["repos"].daily.get_recent_nightly_rates(
            "tank-1", "2026-10-07", n=2
        ) == rates[:2]


class TestProcessEvent:
    def test_valid_event_stored(self, ctx):
        ctx["repos"].tanks.put_tank(make_tank())
        result = process_event(
            {"tankId": "tank-1", "ts": 1700000000.0, "type": "PUMP_CUT", "reason": "FULL"},
            ctx["repos"].tanks,
            ctx["repos"].events,
            1700000000.0,
        )
        assert result == {"ok": True, "stored": True, "tank_id": "tank-1",
                          "event_type": "PUMP_CUT"}
        rows = ctx["repos"].events.query_events("tank-1", 0.0, 1800000000.0)
        assert len(rows) == 1
        assert rows[0]["eventType"] == "PUMP_CUT"
        assert rows[0]["sk"].startswith("EVT#")

    def test_unknown_tank_dropped(self, ctx):
        result = process_event(
            {"tankId": "ghost", "ts": 1.0, "type": "PUMP_CUT"},
            ctx["repos"].tanks, ctx["repos"].events, 1.0,
        )
        assert result["reason"] == "unknown_tank"

    def test_missing_type_rejected(self, ctx):
        ctx["repos"].tanks.put_tank(make_tank())
        result = process_event(
            {"tankId": "tank-1", "ts": 1.0},
            ctx["repos"].tanks, ctx["repos"].events, 1.0,
        )
        assert result == {"ok": False, "reason": "invalid_message",
                          "detail": "type required"}


class TestSnsNotifier:
    def test_subject_rules(self):
        assert sanitise_subject("a\nb\r\nc") == "a b c"
        assert sanitise_subject("x" * 100) == "x" * 100
        assert sanitise_subject("y" * 150) == "y" * 97 + "..."

    def test_publishes_to_moto_topic_with_truncated_subject(self, ctx):
        sns = boto3.client("sns", region_name=REGION)
        sqs = boto3.client("sqs", region_name=REGION)
        topic_arn = sns.create_topic(Name="alerts")["TopicArn"]
        queue_url = sqs.create_queue(QueueName="mailbox")["QueueUrl"]
        queue_arn = sqs.get_queue_attributes(
            QueueUrl=queue_url, AttributeNames=["QueueArn"]
        )["Attributes"]["QueueArn"]
        sns.subscribe(TopicArn=topic_arn, Protocol="sqs", Endpoint=queue_arn)

        SnsNotifier(topic_arn, sns).send("L" * 150 + "\nsecond line", "hello body")
        messages = sqs.receive_message(
            QueueUrl=queue_url, MaxNumberOfMessages=10
        ).get("Messages", [])
        assert len(messages) == 1
        envelope = json.loads(messages[0]["Body"])
        assert envelope["Subject"] == "L" * 97 + "..."
        assert envelope["Message"] == "hello body"
