"""Tests for backend.shared.leak_detection.

No simulator/ exists in the repo yet, so readings are produced by the
local deterministic generator below, which emits the same telemetry shape
the simulator will produce: dicts with ts, levelPct and pumpOn.
"""

import json
import random

import pytest

from backend.shared.leak_detection import (
    baseline,
    evaluate_leak,
    fit_loss_rate,
    is_clean_night,
    nightly_rate,
    resample_median,
)

CAPACITY_L = 1000.0
TARIFF = 60.0


def make_night(
    hours=8.0,
    step_s=60.0,
    start_level=80.0,
    loss_per_hr=0.0,
    noise_std=0.0,
    seed=0,
    pump_windows=(),
    refill_at=None,
    refill_pct=12.0,
):
    """Synthetic overnight telemetry: linear fall + gaussian sensor noise."""
    rng = random.Random(seed)
    count = int(hours * 3600 / step_s)
    readings = []
    for i in range(count):
        ts = i * step_s
        level = start_level - loss_per_hr * ts / 3600.0
        if refill_at is not None and ts >= refill_at:
            level += refill_pct
        if noise_std:
            level += rng.gauss(0.0, noise_std)
        pump = any(start <= ts < end for start, end in pump_windows)
        readings.append({"ts": ts, "levelPct": level, "pumpOn": pump})
    return readings


def past_quiet_nights():
    return [
        {"rate": 0.05, "r_squared": 0.4, "hours_observed": 7.5},
        {"rate": -0.02, "r_squared": 0.3, "hours_observed": 7.0},
        {"rate": 0.08, "r_squared": 0.5, "hours_observed": 8.0},
        {"rate": 0.03, "r_squared": 0.2, "hours_observed": 6.5},
    ]


class TestResampleMedian:
    def test_buckets_and_medians(self):
        readings = [{"ts": float(t), "levelPct": lvl, "pumpOn": False} for t, lvl in [
            (0, 10.0), (60, 50.0), (120, 20.0), (180, 40.0), (240, 30.0),
            (300, 1.0), (360, 5.0), (420, 2.0), (480, 4.0), (540, 3.0),
        ]]
        assert resample_median(readings, bucket_seconds=300) == [
            (0, 30.0),
            (300, 3.0),
        ]

    def test_skips_bad_readings_and_empty(self):
        readings = [
            {"ts": 0.0, "levelPct": None, "pumpOn": False},
            {"ts": 60.0, "levelPct": float("nan"), "pumpOn": False},
            {"ts": None, "levelPct": 50.0, "pumpOn": False},
        ]
        assert resample_median(readings) == []
        assert resample_median([]) == []

    def test_bad_bucket_size_raises(self):
        with pytest.raises(ValueError):
            resample_median([], bucket_seconds=0)


class TestIsCleanNight:
    def test_flat_night_is_clean(self):
        assert is_clean_night(make_night(loss_per_hr=0.0)) is True

    def test_falling_level_is_still_clean(self):
        # A leak is what the night is for measuring; only rises disqualify.
        assert is_clean_night(make_night(loss_per_hr=1.5)) is True

    def test_pump_on_fails(self):
        readings = make_night(pump_windows=[(7200.0, 9000.0)])
        assert is_clean_night(readings) is False

    def test_refill_fails(self):
        assert is_clean_night(make_night(refill_at=4 * 3600.0)) is False

    def test_empty_fails(self):
        assert is_clean_night([]) is False


class TestFitLossRate:
    def test_exact_one_pct_per_hour(self):
        rate, r2 = fit_loss_rate([(0.0, 80.0), (3600.0, 79.0)])
        assert rate == pytest.approx(1.0)
        assert r2 == pytest.approx(1.0)

    def test_flat_line(self):
        rate, r2 = fit_loss_rate([(0.0, 50.0), (3600.0, 50.0), (7200.0, 50.0)])
        assert rate == pytest.approx(0.0)
        assert r2 == pytest.approx(1.0)

    def test_rising_level_gives_negative_rate(self):
        rate, _ = fit_loss_rate([(0.0, 50.0), (3600.0, 52.0)])
        assert rate == pytest.approx(-2.0)

    def test_too_few_points(self):
        assert fit_loss_rate([]) == (0.0, 0.0)
        assert fit_loss_rate([(0.0, 50.0)]) == (0.0, 0.0)


class TestNightlyRate:
    def test_clean_flat_night(self):
        result = nightly_rate(make_night(hours=8.0, noise_std=0.1, seed=7))
        assert result is not None
        assert result["rate"] == pytest.approx(0.0, abs=0.1)
        assert result["hours_observed"] == pytest.approx(8.0, abs=0.2)

    def test_noisy_flat_night(self):
        readings = make_night(hours=7.0, noise_std=0.15, seed=3)
        assert is_clean_night(readings) is True
        result = nightly_rate(readings)
        assert result is not None
        assert abs(result["rate"]) < 0.3

    def test_steady_leak_night(self):
        result = nightly_rate(
            make_night(hours=7.0, loss_per_hr=1.5, noise_std=0.05, seed=11)
        )
        assert result is not None
        assert result["rate"] == pytest.approx(1.5, abs=0.1)
        assert result["r_squared"] > 0.9

    def test_pump_on_night_has_no_result(self):
        assert nightly_rate(make_night(pump_windows=[(7200.0, 9000.0)])) is None

    def test_refill_night_has_no_result(self):
        assert nightly_rate(make_night(refill_at=4 * 3600.0)) is None

    def test_under_90_minutes_has_no_result(self):
        readings = make_night(hours=1.0)
        assert is_clean_night(readings) is True  # clean, just too short
        assert nightly_rate(readings) is None


class TestBaseline:
    def test_median_and_mad(self):
        med, mad = baseline([0.05, 0.10, 0.0, 0.15, 0.08])
        assert med == pytest.approx(0.08)
        assert mad == pytest.approx(0.03)

    def test_accepts_nightly_dicts(self):
        med, mad = baseline(past_quiet_nights())
        assert med == pytest.approx(0.04)
        assert mad == pytest.approx(0.025)

    def test_empty(self):
        assert baseline([]) == (0.0, 0.0)


class TestEvaluateLeak:
    def test_clean_flat_night_no_leak(self):
        tonight = nightly_rate(make_night(hours=8.0, noise_std=0.1, seed=7))
        result = evaluate_leak(
            tonight, past_quiet_nights(), capacity_l=CAPACITY_L, tariff_per_kl=TARIFF
        )
        assert result["leak"] is False

    def test_noisy_flat_night_no_leak(self):
        tonight = nightly_rate(make_night(hours=7.0, noise_std=0.15, seed=3))
        result = evaluate_leak(
            tonight, past_quiet_nights(), capacity_l=CAPACITY_L, tariff_per_kl=TARIFF
        )
        assert result["leak"] is False

    def test_steady_leak_flagged_with_loss_estimate(self):
        tonight = nightly_rate(
            make_night(hours=7.0, loss_per_hr=1.5, noise_std=0.05, seed=11)
        )
        result = evaluate_leak(
            tonight, past_quiet_nights(), capacity_l=CAPACITY_L, tariff_per_kl=TARIFF
        )
        assert result["leak"] is True
        assert result["confidence"] == "high"
        expected_l = (
            result["rate"] * result["hours_observed"] * CAPACITY_L / 100.0
        )
        assert result["estimated_loss_l"] == pytest.approx(expected_l)
        assert result["estimated_loss_l"] == pytest.approx(104.0, abs=8.0)
        assert result["estimated_rupees"] == pytest.approx(
            result["estimated_loss_l"] / 1000.0 * TARIFF
        )

    def test_no_result_means_no_leak(self):
        result = evaluate_leak(None, past_quiet_nights(), capacity_l=CAPACITY_L,
                               tariff_per_kl=TARIFF)
        assert result["leak"] is False
        assert result["estimated_loss_l"] == 0.0
        assert result["estimated_rupees"] == 0.0

    def test_fewer_than_three_past_nights_uses_absolute_threshold(self):
        tonight = {"rate": 1.2, "r_squared": 0.95, "hours_observed": 5.0}
        past = [{"rate": 0.1, "r_squared": 0.5, "hours_observed": 7.0},
                {"rate": 0.0, "r_squared": 0.4, "hours_observed": 7.0}]
        assert evaluate_leak(tonight, past)["leak"] is True  # 1.2 > 0.8
        calm = {"rate": 0.5, "r_squared": 0.9, "hours_observed": 5.0}
        assert evaluate_leak(calm, past)["leak"] is False  # 0.5 < 0.8

    def test_baseline_normal_variation_does_not_false_alarm(self):
        past = [0.10, 0.12, 0.09, 0.11]
        tonight = {"rate": 0.11, "r_squared": 0.6, "hours_observed": 6.0}
        result = evaluate_leak(tonight, past, abs_min_pct_per_hr=0.05)
        assert result["leak"] is False
        assert result["threshold"] == pytest.approx(0.135)

    def test_adaptive_threshold_catches_real_leak(self):
        past = [0.10, 0.12, 0.09, 0.11]
        tonight = {"rate": 0.5, "r_squared": 0.95, "hours_observed": 6.0}
        result = evaluate_leak(tonight, past, abs_min_pct_per_hr=0.05)
        assert result["leak"] is True
        assert result["confidence"] == "high"

    def test_confidence_tiers(self):
        high = evaluate_leak(
            {"rate": 1.0, "r_squared": 0.95, "hours_observed": 4.0}, past_quiet_nights()
        )
        assert high["confidence"] == "high"
        short = evaluate_leak(
            {"rate": 1.0, "r_squared": 0.95, "hours_observed": 2.0}, past_quiet_nights()
        )
        assert short["confidence"] == "medium"  # high R^2 but < 3 hours
        medium = evaluate_leak(
            {"rate": 1.0, "r_squared": 0.75, "hours_observed": 5.0}, past_quiet_nights()
        )
        assert medium["confidence"] == "medium"
        low = evaluate_leak(
            {"rate": 1.0, "r_squared": 0.5, "hours_observed": 5.0}, past_quiet_nights()
        )
        assert low["confidence"] == "low"


class TestLeakExample:
    def test_print_leak_example(self):
        tonight = nightly_rate(
            make_night(
                hours=7.0,
                start_level=80.0,
                loss_per_hr=1.5,
                noise_std=0.05,
                seed=11,
            )
        )
        result = evaluate_leak(
            tonight, past_quiet_nights(), capacity_l=CAPACITY_L, tariff_per_kl=TARIFF
        )
        print("LEAK_EXAMPLE " + json.dumps(result, indent=2, sort_keys=True))
        assert result["leak"] is True
