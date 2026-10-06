"""Tests for backend.shared.daily_stats."""

import json
import random
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from backend.shared.daily_stats import compute_daily

IST = ZoneInfo("Asia/Kolkata")
DATE = "2026-10-05"
NEXT = "2026-10-06"
TANK = {
    "tank_id": "tank-1",
    "name": "Hostel Block A",
    "capacityL": 1000.0,  # 1% level = 10 litres
    "pumpFlowLpm": 20.0,
    "assumedOverrunMin": 5.0,
    "tariffPerKL": 60.0,
}


def ts_at(date_str, hh, mm):
    y, m, d = (int(p) for p in date_str.split("-"))
    return datetime(y, m, d, hh, mm, tzinfo=IST).timestamp()


def gen_segments(date_str, segments, step_s=60.0, noise_std=0.0, seed=0):
    """segments: (start_min, end_min, start_level, end_level, pump_on)."""
    rng = random.Random(seed)
    readings = []
    for start_min, end_min, start_lvl, end_lvl, pump in segments:
        span = (end_min - start_min) * 60.0
        count = int(span / step_s)
        for i in range(count):
            frac = (i * step_s) / span if span else 0.0
            level = start_lvl + frac * (end_lvl - start_lvl)
            if noise_std:
                level += rng.gauss(0.0, noise_std)
            readings.append(
                {
                    "ts": ts_at(date_str, 0, 0) + (start_min * 60.0 + i * step_s),
                    "levelPct": level,
                    "pumpOn": pump,
                }
            )
    return readings


def two_fill_day():
    # Gentle 4 h drains and 2 h fills: 5-minute medians truncate roughly one
    # bucket at each segment edge, so expectations carry a few litres slack.
    return gen_segments(
        DATE,
        [
            (360, 600, 80.0, 40.0, False),  # 06:00-10:00 use 400 L
            (600, 720, 40.0, 85.0, True),  # 10:00-12:00 fill 450 L
            (720, 960, 85.0, 85.0, False),  # hold
            (960, 1200, 85.0, 50.0, False),  # 16:00-20:00 use 350 L
            (1200, 1320, 50.0, 90.0, True),  # 20:00-22:00 fill 400 L
            (1320, 1439, 90.0, 90.0, False),  # hold
        ],
    )


class TestNormalDay:
    def test_two_fills(self):
        stats = compute_daily(two_fill_day(), [], [], TANK, DATE)
        assert stats["dataMissing"] is False
        assert stats["consumedL"] == pytest.approx(750.0, abs=25.0)
        assert stats["filledL"] == pytest.approx(850.0, abs=35.0)
        assert stats["overflowCuts"] == 0
        assert stats["leakEvents"] == 0
        assert stats["wastedL"] == 0.0
        assert stats["savedL"] == 0.0

    def test_pump_on_drop_not_counted_as_consumption(self):
        # Level falls while pumping (leak during fill): ambiguous, skipped.
        readings = gen_segments(DATE, [(360, 420, 80.0, 70.0, True)])
        stats = compute_daily(readings, [], [], TANK, DATE)
        assert stats["consumedL"] == 0.0
        assert stats["filledL"] == 0.0


class TestLeakDay:
    def test_flagged_evaluation(self):
        readings = gen_segments(
            DATE,
            [
                (0, 360, 80.0, 71.0, False),  # overnight leak 1.5 %/hr
                (360, 1439, 71.0, 71.0, False),
            ],
        )
        evaluations = [
            {
                "leak": True,
                "rate": 1.5,
                "r_squared": 0.95,
                "hours_observed": 6.0,
                "estimated_loss_l": 90.0,
                "date": DATE,
            },
            {"leak": False, "rate": 0.1, "estimated_loss_l": 2.0, "date": DATE},
            {
                "leak": True,
                "rate": 2.0,
                "estimated_loss_l": 120.0,
                "date": "2026-10-04",  # another day: excluded
            },
        ]
        stats = compute_daily(readings, [], evaluations, TANK, DATE)
        assert stats["leakEvents"] == 1
        assert stats["wastedL"] == pytest.approx(90.0)
        assert stats["rupeesWasted"] == pytest.approx(90.0 / 1000.0 * 60.0)
        assert stats["nightlyRatePctPerHr"] == pytest.approx(0.1)  # last dated eval
        assert stats["consumedL"] == pytest.approx(90.0, abs=5.0)

    def test_dateless_evaluation_assumed_prefiltered(self):
        readings = gen_segments(DATE, [(0, 120, 80.0, 78.0, False)])
        evaluations = [{"leak": True, "rate": 2.0, "estimated_loss_l": 50.0}]
        stats = compute_daily(readings, [], evaluations, TANK, DATE)
        assert stats["leakEvents"] == 1
        assert stats["wastedL"] == pytest.approx(50.0)


class TestOverflowCut:
    def test_one_full_cut(self):
        readings = gen_segments(DATE, [(360, 420, 80.0, 70.0, False)])
        events = [
            {"type": "PUMP_CUT", "reason": "FULL", "ts": ts_at(DATE, 8, 5)},
            {"type": "PUMP_CUT", "reason": "SENSOR_FAULT", "ts": ts_at(DATE, 9, 0)},
            {"type": "PUMP_CUT", "reason": "FULL", "ts": ts_at(NEXT, 8, 5)},
            "PUMP_CUT",  # bare string carries no reason: not counted
        ]
        stats = compute_daily(readings, events, [], TANK, DATE)
        assert stats["overflowCuts"] == 1
        assert stats["savedL"] == pytest.approx(1 * 20.0 * 5.0)
        assert stats["rupeesSaved"] == pytest.approx(100.0 / 1000.0 * 60.0)


class TestNoData:
    def test_zeros_and_flag(self):
        stats = compute_daily([], [], [], TANK, DATE)
        assert stats["dataMissing"] is True
        for key in (
            "consumedL", "filledL", "overflowCuts", "leakEvents",
            "wastedL", "savedL", "rupeesWasted", "rupeesSaved",
        ):
            assert stats[key] == 0.0
        assert stats["nightlyRatePctPerHr"] is None


class TestNoise:
    def test_noise_only_changes_ignored(self):
        readings = gen_segments(
            DATE, [(0, 1439, 70.0, 70.0, False)], noise_std=0.15, seed=3
        )
        stats = compute_daily(readings, [], [], TANK, DATE)
        assert stats["dataMissing"] is False
        assert stats["consumedL"] == 0.0
        assert stats["filledL"] == 0.0

    def test_small_real_change_below_threshold_ignored(self):
        readings = gen_segments(DATE, [(0, 120, 70.0, 69.8, False)])
        stats = compute_daily(readings, [], [], TANK, DATE)
        assert stats["consumedL"] == 0.0


class TestDayBoundaries:
    def test_ist_midnight_splits_days(self):
        day_readings = gen_segments(
            DATE,
            [
                (720, 780, 80.0, 70.0, False),  # 12:00-13:00 use ~100 L
                (780, 1439, 70.0, 70.0, False),  # hold incl. 23:59
            ],
        )
        stray = {
            "ts": ts_at(NEXT, 0, 1),  # 00:01 next day, faraway level
            "levelPct": 20.0,
            "pumpOn": False,
        }
        stats = compute_daily(day_readings + [stray], [], [], TANK, DATE)
        assert stats["consumedL"] == pytest.approx(100.0, abs=5.0)

        next_readings = gen_segments(
            NEXT, [(1, 61, 20.0, 15.0, False)]  # 00:01-01:01 use ~50 L
        )
        next_stats = compute_daily(next_readings, [], [], TANK, NEXT)
        assert next_stats["dataMissing"] is False
        assert next_stats["consumedL"] == pytest.approx(50.0, abs=5.0)

    def test_bad_date_rejected(self):
        with pytest.raises(ValueError):
            compute_daily([], [], [], TANK, "05-10-2026")


class TestSimulatedDay:
    def test_print_simulated_day(self):
        readings = two_fill_day()
        events = [{"type": "PUMP_CUT", "reason": "FULL", "ts": ts_at(DATE, 18, 20)}]
        stats = compute_daily(readings, events, [], TANK, DATE)
        print("DAILY_STATS " + json.dumps(stats, indent=2, sort_keys=True))
        assert stats["overflowCuts"] == 1
