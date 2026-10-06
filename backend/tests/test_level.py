"""Tests for backend.shared.level."""

import math

import pytest

from backend.shared.level import (
    is_valid_distance,
    level_pct,
    median,
    volume_l,
)

D_EMPTY = 200.0  # distance (cm) when the tank is empty
D_FULL = 20.0  # distance (cm) when the tank is full


class TestLevelPct:
    def test_empty_reads_zero(self):
        assert level_pct(D_EMPTY, D_EMPTY, D_FULL) == pytest.approx(0.0)

    def test_full_reads_hundred(self):
        assert level_pct(D_FULL, D_EMPTY, D_FULL) == pytest.approx(100.0)

    def test_halfway_reads_fifty(self):
        assert level_pct(110.0, D_EMPTY, D_FULL) == pytest.approx(50.0)

    def test_quarter(self):
        # 25% full -> distance 3/4 of the way from full to empty
        assert level_pct(155.0, D_EMPTY, D_FULL) == pytest.approx(25.0)

    def test_clamps_above_full(self):
        # Sensor closer than the full mark (e.g. ripples): still 100, not more.
        assert level_pct(5.0, D_EMPTY, D_FULL) == 100.0
        assert level_pct(0.0, D_EMPTY, D_FULL) == 100.0

    def test_clamps_below_empty(self):
        assert level_pct(250.0, D_EMPTY, D_FULL) == 0.0

    def test_nan_propagates(self):
        assert math.isnan(level_pct(float("nan"), D_EMPTY, D_FULL))

    def test_zero_span_raises(self):
        with pytest.raises(ValueError):
            level_pct(100.0, 100.0, 100.0)

    def test_inverted_calibration_raises(self):
        with pytest.raises(ValueError):
            level_pct(100.0, D_FULL, D_EMPTY)

    def test_non_numeric_raises(self):
        with pytest.raises(TypeError):
            level_pct(None, D_EMPTY, D_FULL)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            level_pct("110", D_EMPTY, D_FULL)  # type: ignore[arg-type]


class TestVolumeL:
    def test_half_tank(self):
        assert volume_l(50.0, 1000.0) == pytest.approx(500.0)

    def test_boundaries(self):
        assert volume_l(0.0, 1000.0) == pytest.approx(0.0)
        assert volume_l(100.0, 1000.0) == pytest.approx(1000.0)

    def test_clamps_out_of_range_pct(self):
        assert volume_l(150.0, 1000.0) == pytest.approx(1000.0)
        assert volume_l(-10.0, 1000.0) == pytest.approx(0.0)

    def test_zero_capacity(self):
        assert volume_l(50.0, 0.0) == pytest.approx(0.0)

    def test_negative_capacity_raises(self):
        with pytest.raises(ValueError):
            volume_l(50.0, -100.0)


class TestIsValidDistance:
    def test_mid_range_valid(self):
        assert is_valid_distance(110.0, D_EMPTY, D_FULL, 10.0) is True

    def test_exact_bounds_valid(self):
        assert is_valid_distance(D_FULL, D_EMPTY, D_FULL, 10.0) is True
        assert is_valid_distance(D_EMPTY, D_EMPTY, D_FULL, 10.0) is True

    def test_within_margin_valid(self):
        assert is_valid_distance(D_FULL - 10.0, D_EMPTY, D_FULL, 10.0) is True
        assert is_valid_distance(D_EMPTY + 10.0, D_EMPTY, D_FULL, 10.0) is True

    def test_outside_margin_invalid(self):
        assert is_valid_distance(D_FULL - 10.1, D_EMPTY, D_FULL, 10.0) is False
        assert is_valid_distance(D_EMPTY + 10.1, D_EMPTY, D_FULL, 10.0) is False
        assert is_valid_distance(500.0, D_EMPTY, D_FULL, 10.0) is False

    def test_rejects_garbage(self):
        assert is_valid_distance(None, D_EMPTY, D_FULL, 10.0) is False
        assert is_valid_distance(float("nan"), D_EMPTY, D_FULL, 10.0) is False
        assert is_valid_distance(float("inf"), D_EMPTY, D_FULL, 10.0) is False
        assert is_valid_distance(-5.0, D_EMPTY, D_FULL, 10.0) is False
        assert is_valid_distance("110", D_EMPTY, D_FULL, 10.0) is False

    def test_negative_margin_raises(self):
        with pytest.raises(ValueError):
            is_valid_distance(110.0, D_EMPTY, D_FULL, -1.0)


class TestMedian:
    def test_odd_count(self):
        assert median([30.0, 10.0, 20.0]) == pytest.approx(20.0)

    def test_even_count(self):
        assert median([10.0, 40.0, 20.0, 30.0]) == pytest.approx(25.0)

    def test_single_value(self):
        assert median([42.0]) == pytest.approx(42.0)

    def test_ignores_invalid_readings(self):
        assert median([None, float("nan"), 20.0, float("inf"), 10.0]) == pytest.approx(
            15.0
        )

    def test_empty_returns_none(self):
        assert median([]) is None

    def test_all_invalid_returns_none(self):
        assert median([None, float("nan"), float("-inf")]) is None
