"""Tests for backend.shared.alerts."""

import pytest

from backend.shared.alerts import (
    DEFAULT_COOLDOWNS,
    DEVICE_OFFLINE,
    LEAK_SUSPECTED,
    OVERFLOW_RISK,
    SENSOR_FAULT,
    SEVERITIES,
    FakeNotifier,
    InMemoryAlertStore,
    build_message,
    evaluate_offline,
    evaluate_overflow_risk,
    evaluate_sensor_fault,
    leak_alert_from_evaluation,
    process_alert,
    should_send,
)

TANK = {"tank_id": "tank-1", "name": "Hostel Block A"}


def with_tank(alert):
    alert = dict(alert)
    alert["tank_id"] = TANK["tank_id"]
    alert["tank_name"] = TANK["name"]
    return alert


def fault_reading(ts):
    return {"ts": ts, "levelPct": None, "pumpOn": False, "fault": True}


def ok_reading(ts, level=50.0):
    return {"ts": ts, "levelPct": level, "pumpOn": False}


class TestSeverities:
    def test_mapping(self):
        assert SEVERITIES == {
            OVERFLOW_RISK: "critical",
            LEAK_SUSPECTED: "warning",
            DEVICE_OFFLINE: "warning",
            SENSOR_FAULT: "warning",
        }


class TestOverflowRisk:
    def test_triggers_at_98_with_pump_on(self):
        alert = evaluate_overflow_risk(
            {"ts": 1000.0, "levelPct": 98.0, "pumpOn": True}, TANK
        )
        assert alert is not None
        assert alert["alert_type"] == OVERFLOW_RISK
        assert alert["severity"] == "critical"
        assert alert["tank_name"] == "Hostel Block A"
        assert alert["details"]["level_pct"] == pytest.approx(98.0)

    def test_just_below_threshold_does_not_trigger(self):
        assert (
            evaluate_overflow_risk(
                {"ts": 1000.0, "levelPct": 97.9, "pumpOn": True}, TANK
            )
            is None
        )

    def test_pump_off_does_not_trigger(self):
        assert (
            evaluate_overflow_risk(
                {"ts": 1000.0, "levelPct": 99.5, "pumpOn": False}, TANK
            )
            is None
        )

    def test_threshold_overridable(self):
        reading = {"ts": 1000.0, "levelPct": 90.0, "pumpOn": True}
        assert (
            evaluate_overflow_risk(reading, TANK, level_threshold_pct=90.0)
            is not None
        )
        assert (
            evaluate_overflow_risk(reading, TANK, level_threshold_pct=90.1) is None
        )


class TestSensorFault:
    def test_three_consecutive_faults_trigger(self):
        readings = [fault_reading(float(t)) for t in (0, 60, 120)]
        alert = evaluate_sensor_fault(readings)
        assert alert is not None
        assert alert["alert_type"] == SENSOR_FAULT
        assert alert["details"]["consecutive_faults"] == 3

    def test_nan_level_counts_as_fault(self):
        readings = [
            {"ts": 0.0, "levelPct": float("nan"), "pumpOn": False},
            {"ts": 60.0, "levelPct": None, "pumpOn": False},
            fault_reading(120.0),
        ]
        assert evaluate_sensor_fault(readings) is not None

    def test_good_reading_breaks_streak(self):
        readings = [fault_reading(0.0), fault_reading(60.0), ok_reading(120.0)]
        assert evaluate_sensor_fault(readings) is None

    def test_too_few_readings_is_not_enough_evidence(self):
        assert evaluate_sensor_fault([fault_reading(0.0), fault_reading(60.0)]) is None

    def test_consecutive_overridable(self):
        readings = [fault_reading(0.0), fault_reading(60.0)]
        alert = evaluate_sensor_fault(readings, consecutive=2)
        assert alert is not None
        assert alert["details"]["consecutive_faults"] == 2


class TestOffline:
    def test_triggers_after_three_missed_intervals(self):
        alert = evaluate_offline(0.0, 180.0, 60.0)
        assert alert is not None
        assert alert["alert_type"] == DEVICE_OFFLINE
        assert alert["details"]["gap_sec"] == pytest.approx(180.0)

    def test_just_inside_budget_does_not_trigger(self):
        assert evaluate_offline(0.0, 179.9, 60.0) is None

    def test_never_heard_from_triggers(self):
        assert evaluate_offline(None, 1000.0, 60.0) is not None

    def test_future_last_ts_does_not_trigger(self):
        assert evaluate_offline(2000.0, 1000.0, 60.0) is None


class TestLeakAlert:
    def test_builds_from_positive_evaluation(self):
        evaluation = {
            "leak": True,
            "rate": 1.5,
            "r_squared": 0.95,
            "hours_observed": 7.0,
            "threshold": 0.8,
            "confidence": "high",
            "estimated_loss_l": 105.0,
            "estimated_rupees": 6.30,
        }
        alert = leak_alert_from_evaluation(TANK, evaluation)
        assert alert is not None
        assert alert["alert_type"] == LEAK_SUSPECTED
        assert alert["severity"] == "warning"
        assert alert["details"]["estimated_loss_l"] == pytest.approx(105.0)
        assert alert["details"]["estimated_rupees"] == pytest.approx(6.30)

    def test_negative_evaluation_gives_none(self):
        assert leak_alert_from_evaluation(TANK, {"leak": False, "rate": 0.1}) is None
        assert leak_alert_from_evaluation(TANK, None) is None


class TestCooldowns:
    def test_defaults(self):
        assert DEFAULT_COOLDOWNS == {
            OVERFLOW_RISK: 30 * 60,
            LEAK_SUSPECTED: 12 * 3600,
            DEVICE_OFFLINE: 3600,
            SENSOR_FAULT: 3600,
        }

    def test_never_sent_sends(self):
        assert should_send(OVERFLOW_RISK, None, 1000.0) is True

    def test_inside_cooldown_suppresses(self):
        assert should_send(OVERFLOW_RISK, 1000.0, 1000.0 + 29 * 60) is False

    def test_exactly_at_cooldown_sends_again(self):
        assert should_send(OVERFLOW_RISK, 1000.0, 1000.0 + 30 * 60) is True

    def test_overrides_merge_with_defaults(self):
        assert should_send(OVERFLOW_RISK, 1000.0, 1060.0, {"OVERFLOW_RISK": 60}) is True
        assert should_send(DEVICE_OFFLINE, 1000.0, 4600.0, {"OVERFLOW_RISK": 60}) is True
        assert should_send(DEVICE_OFFLINE, 1000.0, 2000.0, {"OVERFLOW_RISK": 60}) is False

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError):
            should_send("NOPE", None, 0.0)


class TestProcessAlert:
    def test_records_and_sends_first_time(self):
        store, notifier = InMemoryAlertStore(), FakeNotifier()
        alert = with_tank(
            evaluate_overflow_risk({"ts": 0.0, "levelPct": 99.0, "pumpOn": True}, TANK)
        )
        result = process_alert(alert, store, notifier, now_ts=5000.0)
        assert result == {"sent": True, "error": None}
        assert len(store.alerts) == 1
        assert store.alerts[0]["notified"] is True
        assert len(notifier.sent) == 1

    def test_duplicate_inside_cooldown_recorded_but_not_sent(self):
        store, notifier = InMemoryAlertStore(), FakeNotifier()
        alert = with_tank(
            evaluate_overflow_risk({"ts": 0.0, "levelPct": 99.0, "pumpOn": True}, TANK)
        )
        process_alert(alert, store, notifier, now_ts=5000.0)
        result = process_alert(alert, store, notifier, now_ts=5000.0 + 10 * 60)
        assert result["sent"] is False
        assert len(store.alerts) == 2  # recorded anyway
        assert len(notifier.sent) == 1  # but not re-sent

    def test_sends_again_after_cooldown(self):
        store, notifier = InMemoryAlertStore(), FakeNotifier()
        alert = with_tank(
            evaluate_overflow_risk({"ts": 0.0, "levelPct": 99.0, "pumpOn": True}, TANK)
        )
        process_alert(alert, store, notifier, now_ts=5000.0)
        result = process_alert(alert, store, notifier, now_ts=5000.0 + 31 * 60)
        assert result["sent"] is True
        assert len(notifier.sent) == 2

    def test_notifier_failure_does_not_crash(self):
        store, notifier = InMemoryAlertStore(), FakeNotifier(fail=True)
        alert = with_tank(
            evaluate_overflow_risk({"ts": 0.0, "levelPct": 99.0, "pumpOn": True}, TANK)
        )
        result = process_alert(alert, store, notifier, now_ts=5000.0)
        assert result["sent"] is False
        assert "RuntimeError" in result["error"]
        assert len(store.alerts) == 1
        assert store.alerts[0]["notified"] is False
        assert store.alerts[0]["notify_error"] is not None
        # Last-sent untouched, so a later retry may still deliver.
        assert store.get_last_sent(OVERFLOW_RISK, "tank-1") is None


class TestMessages:
    def test_overflow_message(self):
        alert = with_tank(
            evaluate_overflow_risk({"ts": 0.0, "levelPct": 98.5, "pumpOn": True}, TANK)
        )
        subject, body = build_message(alert, TANK)
        assert "Hostel Block A" in subject
        assert "98.5" in subject + body
        assert "Switch the pump off" in body

    def test_leak_message(self):
        alert = leak_alert_from_evaluation(
            TANK,
            {"leak": True, "rate": 1.5, "r_squared": 0.95, "hours_observed": 7.0,
             "threshold": 0.8, "confidence": "high",
             "estimated_loss_l": 105.0, "estimated_rupees": 6.30},
        )
        subject, body = build_message(alert, TANK)
        assert "Hostel Block A" in subject + body
        assert "105.0" in body and "6.30" in body and "1.50" in body
        assert "Inspect the tank outlet valve" in body

    def test_offline_message(self):
        alert = with_tank(evaluate_offline(0.0, 7200.0, 60.0))
        subject, body = build_message(alert, TANK)
        assert "Hostel Block A" in subject + body
        assert "120 minutes" in body
        assert "power supply" in body

    def test_sensor_fault_message(self):
        alert = with_tank(evaluate_sensor_fault([fault_reading(float(t)) for t in (0, 60, 120)]))
        subject, body = build_message(alert, TANK)
        assert "Hostel Block A" in subject + body
        assert "3 faulty" in body
        assert "Wipe the ultrasonic sensor" in body


class TestExampleEmails:
    def test_print_one_email_per_type(self):
        examples = [
            with_tank(evaluate_overflow_risk(
                {"ts": 1000.0, "levelPct": 98.5, "pumpOn": True}, TANK)),
            leak_alert_from_evaluation(
                TANK,
                {"leak": True, "rate": 1.5, "r_squared": 0.95, "hours_observed": 7.0,
                 "threshold": 0.8, "confidence": "high",
                 "estimated_loss_l": 105.0, "estimated_rupees": 6.30}),
            with_tank(evaluate_offline(0.0, 7200.0, 60.0)),
            with_tank(evaluate_sensor_fault(
                [fault_reading(float(t)) for t in (0, 60, 120)])),
        ]
        for alert in examples:
            assert alert is not None
            message = build_message(alert, TANK)
            print(f"EMAIL [{alert['alert_type']}] subject: {message.subject}")
            print(message.body + "\n")
