"""Ingest-path tests against moto-mocked DynamoDB."""

import boto3
import pytest
from moto import mock_aws

from backend.shared.alerts import OVERFLOW_RISK, FakeNotifier
from backend.shared.ingest_logic import process_telemetry
from backend.shared.repository import DynamoAlertStore, ReadingsRepo, TanksRepo
from backend.shared.tables import create_tables

REGION = "ap-south-1"
BASE_TS = 1_700_000_000.0
RETENTION = 14 * 86400

TANK = {
    "tankId": "tank-1",
    "name": "Test Tank",
    "dEmptyCm": 200.0,
    "dFullCm": 20.0,
    "capacityL": 1000.0,
}


@pytest.fixture
def ctx():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=REGION)
        tables = create_tables(resource)
        tanks = TanksRepo(tables["tanks"])
        tanks.put_tank(dict(TANK))
        yield {
            "tanks": tanks,
            "readings": ReadingsRepo(tables["readings"]),
            "alerts": DynamoAlertStore(tables["alerts"]),
            "alerts_table": tables["alerts"],
            "notifier": FakeNotifier(),
        }


def msg(**overrides):
    base = {"tankId": "tank-1", "ts": BASE_TS, "distanceCm": 110.0, "pumpOn": False}
    base.update(overrides)
    return base


def run(ctx, message, now_ts=BASE_TS):
    return process_telemetry(
        message,
        ctx["tanks"],
        ctx["readings"],
        ctx["alerts"],
        ctx["notifier"],
        now_ts,
    )


def stored(ctx, tank_id="tank-1"):
    return ctx["readings"].query_readings(tank_id, 0.0, 2_000_000_000.0)


class TestValidMessage:
    def test_stored_with_level_and_volume(self, ctx):
        result = run(ctx, msg())
        assert result["ok"] is True
        rows = stored(ctx)
        assert len(rows) == 1
        assert rows[0]["levelPct"] == pytest.approx(50.0)
        assert rows[0]["volumeL"] == pytest.approx(500.0)
        assert rows[0]["faults"] == []

    def test_expires_at_14_days_ahead(self, ctx):
        run(ctx, msg())
        assert stored(ctx)[0]["expiresAt"] == int(BASE_TS) + RETENTION

    def test_tank_latest_updated(self, ctx):
        run(ctx, msg(distanceCm=110.0, pumpOn=True))
        tank = ctx["tanks"].get_tank("tank-1")
        assert tank["lastTs"] == BASE_TS
        assert tank["lastDistanceCm"] == pytest.approx(110.0)
        assert tank["lastLevelPct"] == pytest.approx(50.0)
        assert tank["lastVolumeL"] == pytest.approx(500.0)
        assert tank["pumpOn"] is True
        assert tank["status"] == "OK"

    def test_missing_ts_uses_now(self, ctx):
        message = msg()
        del message["ts"]
        result = run(ctx, message, now_ts=BASE_TS + 500.0)
        assert result["ok"] is True
        assert result["ts"] == BASE_TS + 500.0
        assert len(stored(ctx)) == 1


class TestUnknownTank:
    def test_dropped_with_reason(self, ctx):
        result = run(ctx, msg(tankId="nope"))
        assert result == {"ok": False, "reason": "unknown_tank", "tank_id": "nope"}
        assert stored(ctx, "nope") == []


class TestDuplicatesAndOrder:
    def test_duplicate_overwrites_one_row(self, ctx):
        run(ctx, msg(distanceCm=110.0))
        run(ctx, msg(distanceCm=128.0))  # same (tankId, ts): 40%
        rows = stored(ctx)
        assert len(rows) == 1
        assert rows[0]["levelPct"] == pytest.approx(40.0)

    def test_out_of_order_stored_but_latest_kept(self, ctx):
        run(ctx, msg(ts=BASE_TS + 200.0, distanceCm=110.0))
        result = run(ctx, msg(ts=BASE_TS + 100.0, distanceCm=128.0))
        assert result["latest_updated"] is False
        assert len(stored(ctx)) == 2
        assert ctx["tanks"].get_tank("tank-1")["lastTs"] == BASE_TS + 200.0


class TestInvalidMessages:
    @pytest.mark.parametrize(
        "message",
        [
            {},
            None,
            "tank-1",
            {"tankId": 123, "distanceCm": 110.0},
            {"tankId": "", "distanceCm": 110.0},
            msg(ts="yesterday"),
            msg(pumpOn="yes"),
        ],
    )
    def test_rejected_without_crashing(self, ctx, message):
        result = run(ctx, message)
        assert result["ok"] is False
        assert result["reason"] == "invalid_message"
        assert stored(ctx) == []


class TestFaultyReadings:
    def test_device_fault_stored_with_null_level(self, ctx):
        result = run(ctx, msg(fault=True, distanceCm=110.0))
        assert result["ok"] is True
        assert "device_fault" in result["faults"]
        rows = stored(ctx)
        assert len(rows) == 1
        assert rows[0]["levelPct"] is None
        assert rows[0]["volumeL"] is None
        assert ctx["tanks"].get_tank("tank-1")["status"] == "FAULT"

    def test_invalid_distance_stored_with_null_level(self, ctx):
        result = run(ctx, msg(distanceCm=-5.0))
        assert "invalid_distance" in result["faults"]
        assert stored(ctx)[0]["levelPct"] is None

    def test_missing_distance_stored_with_null_level(self, ctx):
        message = msg()
        del message["distanceCm"]
        result = run(ctx, message)
        assert "missing_distance" in result["faults"]
        assert stored(ctx)[0]["levelPct"] is None


class TestOverflowAlert:
    def test_overflow_creates_alert_and_sends(self, ctx):
        # distance 22 cm -> (200-22)/180*100 = 98.9% with pump on.
        result = run(ctx, msg(distanceCm=22.0, pumpOn=True))
        assert result["alert"] == {"sent": True, "error": None}
        assert len(ctx["notifier"].sent) == 1
        assert ctx["alerts"].get_last_sent(OVERFLOW_RISK, "tank-1") == BASE_TS

    def test_cooldown_records_but_suppresses_resend(self, ctx):
        run(ctx, msg(distanceCm=22.0, pumpOn=True))
        result = run(
            ctx, msg(distanceCm=22.0, pumpOn=True), now_ts=BASE_TS + 10 * 60
        )
        assert result["alert"] == {"sent": False, "error": None}
        assert len(ctx["notifier"].sent) == 1
        records = [
            item
            for item in ctx["alerts_table"].scan().get("Items", [])
            if str(item["sk"]).startswith("ALERT#")
        ]
        assert len(records) == 2  # both recorded
        assert {item["tankId"] for item in records} == {"tank-1"}

    def test_sends_again_after_cooldown(self, ctx):
        run(ctx, msg(distanceCm=22.0, pumpOn=True))
        result = run(
            ctx, msg(distanceCm=22.0, pumpOn=True), now_ts=BASE_TS + 31 * 60
        )
        assert result["alert"]["sent"] is True
        assert len(ctx["notifier"].sent) == 2

    def test_no_alert_below_threshold_or_pump_off(self, ctx):
        assert run(ctx, msg(distanceCm=24.0, pumpOn=True))["alert"] is None
        assert run(ctx, msg(distanceCm=22.0, pumpOn=False))["alert"] is None
        assert len(ctx["notifier"].sent) == 0
