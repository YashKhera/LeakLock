"""Load simulator JSONL telemetry into moto-mocked tables via the ingest path.

Usage:
    python scripts/load_sim_jsonl.py [telemetry.jsonl]
    python scripts/load_sim_jsonl.py --leak-demo

Each input line is one device message, e.g.
``{"tankId": "tank-a", "ts": 1700000000, "distanceCm": 110.0, "pumpOn": false}``.
With no file argument (the simulator is not built yet) a small deterministic
sample is generated and saved next to this script for reuse.

``--leak-demo`` instead seeds a leak-night history (four clean baseline
nights plus tonight's falling levels), runs ``run_leak_check`` at 05:15 IST,
and prints the stored alert record and the email body.

Runs entirely against moto: creates the tables, seeds the demo tanks, feeds
every line through ingest_logic.process_telemetry, then prints the latest
state of each tank plus alert activity.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import boto3
from moto import mock_aws

sys.path.insert(0, ".")

from backend.shared.alerts import FakeNotifier  # noqa: E402
from backend.shared.ingest_logic import process_telemetry  # noqa: E402
from backend.shared.jobs import Repos, run_leak_check  # noqa: E402
from backend.shared.repository import (  # noqa: E402
    DailyRepo,
    DynamoAlertStore,
    EventsRepo,
    ReadingsRepo,
    TanksRepo,
)
from backend.shared.tables import create_tables  # noqa: E402

REGION = "ap-south-1"
IST = ZoneInfo("Asia/Kolkata")

DEMO_TANKS = [
    {
        "tankId": "tank-a",
        "name": "Hostel Block A",
        "dEmptyCm": 200.0,
        "dFullCm": 20.0,
        "capacityL": 1000.0,
    },
    {
        "tankId": "tank-b",
        "name": "Hostel Block B",
        "dEmptyCm": 150.0,
        "dFullCm": 15.0,
        "capacityL": 750.0,
    },
]


def generate_sample(now_ts: int) -> list[dict]:
    """Deterministic demo telemetry: drains, fills, one overflow-risk
    episode on tank-a and one faulty reading on tank-b."""
    messages: list[dict] = []
    step = 300
    # tank-a: drain 80% -> 40%, fill back, then ride to 99% with pump on.
    legs = [
        (80.0, 40.0, 24, False),
        (40.0, 85.0, 12, True),
        (85.0, 99.0, 4, True),
        (99.0, 60.0, 12, False),
    ]
    ts = now_ts - (sum(n for _, _, n, _ in legs) + 2) * step
    for start, end, count, pump in legs:
        for i in range(count):
            frac = i / max(count - 1, 1)
            messages.append(
                {
                    "tankId": "tank-a",
                    "ts": ts,
                    "distanceCm": round(200.0 - (start + frac * (end - start)) / 100.0 * 180.0, 1),
                    "pumpOn": pump,
                }
            )
            ts += step
    # tank-b: gentle drain plus one faulty reading.
    for i in range(12):
        messages.append(
            {"tankId": "tank-b", "ts": ts, "distanceCm": 90.0 - i, "pumpOn": False}
        )
        ts += step
    messages.append({"tankId": "tank-b", "ts": ts, "pumpOn": False, "fault": True})
    return messages


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--leak-demo":
        leak_demo()
        return
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as handle:
            messages = [json.loads(line) for line in handle if line.strip()]
        print(f"Loaded {len(messages)} messages from {sys.argv[1]}")
    else:
        now_ts = int(datetime.now(tz=timezone.utc).timestamp())
        messages = generate_sample(now_ts)
        sample_path = "scripts/sample-telemetry.jsonl"
        with open(sample_path, "w", encoding="utf-8") as handle:
            for message in messages:
                handle.write(json.dumps(message) + "\n")
        print(f"No file given: generated {len(messages)} sample messages -> {sample_path}")

    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=REGION)
        tables = create_tables(resource)
        tanks_repo = TanksRepo(tables["tanks"])
        readings_repo = ReadingsRepo(tables["readings"])
        alert_store = DynamoAlertStore(tables["alerts"])
        notifier = FakeNotifier()
        for tank in DEMO_TANKS:
            tanks_repo.put_tank(dict(tank))

        now_ts = max(m["ts"] for m in messages if isinstance(m.get("ts"), (int, float)))
        ok = dropped = 0
        for message in messages:
            result = process_telemetry(
                message, tanks_repo, readings_repo, alert_store, notifier, now_ts
            )
            ok += result.get("ok", False)
            dropped += not result.get("ok", False)
        print(f"processed={len(messages)} stored_ok={ok} dropped={dropped}")

        for tank in DEMO_TANKS:
            latest = tanks_repo.get_tank(tank["tankId"])
            count = len(readings_repo.query_readings(tank["tankId"], 0.0, float(now_ts) + 1))
            print(
                f"latest[{tank['tankId']}] name={latest.get('name')} "
                f"ts={latest.get('lastTs')} levelPct={latest.get('lastLevelPct')} "
                f"volumeL={latest.get('lastVolumeL')} pumpOn={latest.get('pumpOn')} "
                f"status={latest.get('status')} readings={count}"
            )
        print(f"alerts sent: {len(notifier.sent)}")
        for subject, _ in notifier.sent:
            print(f"  - {subject}")


def leak_demo() -> None:
    """Seed a leak-night history, run the morning leak check, print results."""
    today = datetime.now(tz=IST).strftime("%Y-%m-%d")

    def ist_ts(hour, minute=0):
        year, month, day = (int(p) for p in today.split("-"))
        return datetime(year, month, day, hour, minute, tzinfo=IST).timestamp()

    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=REGION)
        tables = create_tables(resource)
        repos = Repos(
            tanks=TanksRepo(tables["tanks"]),
            readings=ReadingsRepo(tables["readings"]),
            events=EventsRepo(tables["alerts"]),
            daily=DailyRepo(tables["daily"]),
        )
        store = DynamoAlertStore(tables["alerts"])
        notifier = FakeNotifier()
        repos.tanks.put_tank(
            {**DEMO_TANKS[0], "quietStartHour": 0.0, "quietEndHour": 5.0}
        )
        for back, rate in enumerate((0.05, 0.08, 0.03, 0.10), start=1):
            date = (
                datetime.strptime(today, "%Y-%m-%d") - timedelta(days=back)
            ).strftime("%Y-%m-%d")
            repos.daily.put_daily(
                {"tankId": "tank-a", "date": date,
                 "nightlyRatePctPerHr": rate, "nightLeakFlagged": False}
            )
        for i in range(61):  # tonight: 80% -> 72.5% over five quiet hours
            repos.readings.put_reading(
                {"tankId": "tank-a", "ts": ist_ts(0) + i * 300,
                 "levelPct": 80.0 - 1.5 * (i * 300) / 3600.0, "pumpOn": False}
            )

        now_ts = ist_ts(5, 15)
        summary = run_leak_check(now_ts, repos, store, notifier)
        print(f"leak-check summary: {summary}")
        stored = [
            item
            for item in tables["alerts"].scan().get("Items", [])
            if str(item["sk"]).startswith("ALERT#")
        ]
        print(f"stored alert records: {len(stored)}")
        print(json.dumps(stored[0], indent=2, sort_keys=True, default=str))
        subject, body = notifier.sent[0]
        print(f"email subject: {subject}")
        print(f"email body: {body}")


if __name__ == "__main__":
    main()
