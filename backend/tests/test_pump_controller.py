"""Tests for backend.shared.pump_controller."""

import pytest

from backend.shared.pump_controller import (
    MAX_RUNTIME_CUT,
    PUMP_CUT,
    PUMP_RESUME,
    SENSOR_FAULT,
    PumpController,
)

CUT = 95.0
RESUME = 90.0
MAX_RUN = 60.0


def make() -> PumpController:
    return PumpController(
        cut_off_pct=CUT, resume_pct=RESUME, max_run_seconds=MAX_RUN
    )


class TestConstructorValidation:
    def test_resume_must_be_below_cutoff(self):
        with pytest.raises(ValueError):
            PumpController(cut_off_pct=90.0, resume_pct=90.0, max_run_seconds=60.0)
        with pytest.raises(ValueError):
            PumpController(cut_off_pct=85.0, resume_pct=90.0, max_run_seconds=60.0)

    def test_cutoff_range(self):
        with pytest.raises(ValueError):
            PumpController(cut_off_pct=0.0, resume_pct=0.0, max_run_seconds=60.0)
        with pytest.raises(ValueError):
            PumpController(cut_off_pct=101.0, resume_pct=90.0, max_run_seconds=60.0)

    def test_max_run_must_be_positive(self):
        with pytest.raises(ValueError):
            PumpController(cut_off_pct=CUT, resume_pct=RESUME, max_run_seconds=0.0)


class TestNormalOperation:
    def test_low_level_allows_pump(self):
        d = make().update(50.0, now_ts=0.0, fault=False, pump_requested=True)
        assert d.pump_allowed is True
        assert d.events == []

    def test_not_requested_blocks_without_events(self):
        d = make().update(50.0, now_ts=0.0, fault=False, pump_requested=False)
        assert d.pump_allowed is False
        assert d.events == []

    def test_decision_unpacks_like_tuple(self):
        allowed, events = make().update(50.0, now_ts=0.0)
        assert allowed is True
        assert events == []


class TestCutOffBoundary:
    def test_just_below_cutoff_stays_allowed(self):
        c = make()
        d = c.update(94.9, now_ts=0.0, fault=False, pump_requested=True)
        assert d.pump_allowed is True
        assert d.events == []

    def test_exactly_at_cutoff_cuts(self):
        c = make()
        d = c.update(CUT, now_ts=0.0, fault=False, pump_requested=True)
        assert d.pump_allowed is False
        assert d.events == [PUMP_CUT]

    def test_while_cut_repeated_high_level_emits_nothing_new(self):
        c = make()
        c.update(CUT, now_ts=0.0, fault=False, pump_requested=True)
        d = c.update(99.0, now_ts=1.0, fault=False, pump_requested=True)
        assert d.pump_allowed is False
        assert d.events == []


class TestHysteresis:
    def test_no_chatter_near_threshold(self):
        """Oscillating around cut_off must produce exactly one PUMP_CUT."""
        c = make()
        seen: list = []
        for i, level in enumerate([94.0, 96.0, 94.0, 96.0, 94.0]):
            d = c.update(level, now_ts=float(i), fault=False, pump_requested=True)
            seen.extend(d.events)
            assert d.pump_allowed is False or level < CUT
        assert seen.count(PUMP_CUT) == 1
        assert c.is_cut is True  # still latched despite dips below cut_off

    def test_stays_cut_inside_hysteresis_band(self):
        c = make()
        c.update(96.0, now_ts=0.0, fault=False, pump_requested=True)
        for i, level in enumerate([94.0, 92.0, RESUME], start=1):
            d = c.update(level, now_ts=float(i), fault=False, pump_requested=True)
            assert d.pump_allowed is False
            assert d.events == []

    def test_resume_only_below_resume(self):
        c = make()
        c.update(96.0, now_ts=0.0, fault=False, pump_requested=True)
        d = c.update(RESUME - 0.1, now_ts=1.0, fault=False, pump_requested=True)
        assert d.pump_allowed is True
        assert d.events == [PUMP_RESUME]

    def test_full_cycle_cut_then_resume(self):
        c = make()
        assert c.update(50.0, now_ts=0.0, pump_requested=True).pump_allowed is True
        d = c.update(97.0, now_ts=10.0, pump_requested=True)
        assert (d.pump_allowed, d.events) == (False, [PUMP_CUT])
        d = c.update(50.0, now_ts=20.0, pump_requested=True)
        assert (d.pump_allowed, d.events) == (True, [PUMP_RESUME])


class TestSensorFault:
    def test_fault_flag_cuts_with_both_events(self):
        c = make()
        d = c.update(50.0, now_ts=0.0, fault=True, pump_requested=True)
        assert d.pump_allowed is False
        assert d.events == [SENSOR_FAULT, PUMP_CUT]

    def test_none_reading_is_fault(self):
        c = make()
        d = c.update(None, now_ts=0.0, fault=False, pump_requested=True)
        assert d.pump_allowed is False
        assert SENSOR_FAULT in d.events
        assert PUMP_CUT in d.events

    def test_nan_reading_is_fault(self):
        c = make()
        d = c.update(float("nan"), now_ts=0.0, fault=False, pump_requested=True)
        assert d.pump_allowed is False
        assert SENSOR_FAULT in d.events

    def test_persistent_fault_does_not_repeat_events(self):
        c = make()
        c.update(50.0, now_ts=0.0, fault=True, pump_requested=True)
        d = c.update(50.0, now_ts=1.0, fault=True, pump_requested=True)
        assert d.pump_allowed is False
        assert d.events == []

    def test_recovery_after_fault_resumes(self):
        c = make()
        c.update(50.0, now_ts=0.0, fault=True, pump_requested=True)
        d = c.update(50.0, now_ts=1.0, fault=False, pump_requested=True)
        assert d.pump_allowed is True
        assert d.events == [PUMP_RESUME]

    def test_fault_while_already_cut_reports_fault_only_once(self):
        c = make()
        c.update(96.0, now_ts=0.0, fault=False, pump_requested=True)
        d = c.update(96.0, now_ts=1.0, fault=True, pump_requested=True)
        assert d.pump_allowed is False
        assert d.events == [SENSOR_FAULT]  # already cut: no second PUMP_CUT


class TestMaxRuntime:
    def test_constant_level_trips_after_max_run(self):
        c = make()
        assert c.update(50.0, now_ts=0.0, pump_requested=True).pump_allowed is True
        d = c.update(50.0, now_ts=MAX_RUN, pump_requested=True)
        assert d.pump_allowed is True  # strictly *longer than* max_run_seconds
        assert d.events == []
        d = c.update(50.0, now_ts=MAX_RUN + 1.0, pump_requested=True)
        assert d.pump_allowed is False
        assert d.events == [MAX_RUNTIME_CUT, PUMP_CUT]

    def test_rising_level_restarts_window(self):
        """A pump that is making progress must not be cut."""
        c = make()
        t = 0.0
        level = 20.0
        for _ in range(10):  # 200 s of pumping, well past MAX_RUN, always rising
            d = c.update(level, now_ts=t, fault=False, pump_requested=True)
            assert d.pump_allowed is True
            assert MAX_RUNTIME_CUT not in d.events
            t += 20.0
            level += 5.0

    def test_stopped_pump_does_not_accrue_runtime(self):
        c = make()
        c.update(50.0, now_ts=0.0, pump_requested=False)
        d = c.update(50.0, now_ts=MAX_RUN + 100.0, pump_requested=False)
        assert d.pump_allowed is False
        assert d.events == []
        # Requesting later starts a fresh window.
        d = c.update(50.0, now_ts=MAX_RUN + 101.0, pump_requested=True)
        assert d.pump_allowed is True
        assert d.events == []

    def test_runtime_cut_latches_until_level_drops(self):
        # Hold the level inside the hysteresis band (>= resume) so the
        # normal hysteresis path does not release the latch early.
        c = make()
        c.update(92.0, now_ts=0.0, pump_requested=True)
        c.update(92.0, now_ts=MAX_RUN + 1.0, pump_requested=True)
        d = c.update(92.0, now_ts=MAX_RUN + 2.0, pump_requested=True)
        assert d.pump_allowed is False
        assert d.events == []  # latched: no repeat MAX_RUNTIME_CUT
        d = c.update(10.0, now_ts=MAX_RUN + 3.0, pump_requested=True)
        assert d.pump_allowed is True
        assert d.events == [PUMP_RESUME]
