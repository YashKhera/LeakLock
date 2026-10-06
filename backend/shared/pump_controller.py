"""Overflow-protection state machine for the tank pump.

Pure Python, no AWS dependencies. This is the device-safety logic from spec
sections 5.1/5.4: it must run on the ESP32 (mirrored by the simulator) and
never depends on the cloud. All thresholds are constructor arguments.

Event types match section 6.3: PUMP_CUT, PUMP_RESUME, SENSOR_FAULT,
MAX_RUNTIME_CUT.
"""

import math
from typing import NamedTuple, Optional

PUMP_CUT = "PUMP_CUT"
PUMP_RESUME = "PUMP_RESUME"
SENSOR_FAULT = "SENSOR_FAULT"
MAX_RUNTIME_CUT = "MAX_RUNTIME_CUT"


class PumpDecision(NamedTuple):
    """Result of one :meth:`PumpController.update` call."""

    pump_allowed: bool
    events: list


class PumpController:
    """Hysteresis + fault + dry-run protection for the pump relay.

    Rules implemented in :meth:`update`:

    * Cut when ``level_pct >= cut_off_pct``; stay cut until the level drops
      strictly below ``resume_pct`` (hysteresis, so the relay never
      chatters near the threshold).
    * Cut on sensor fault: ``fault`` flag set, or the reading is missing
      (None), NaN, infinite, or outside 0..100. Fails safe.
    * Dry-run guard: if the pump has been running continuously for longer
      than ``max_run_seconds`` without the level rising above the level
      recorded when the run started, cut. A rising level restarts the
      window. The cut latches until the level falls below ``resume_pct``;
      the MAX_RUNTIME_CUT event is what downstream alerting acts on.

    Events are edge-triggered: PUMP_CUT/PUMP_RESUME fire once per latch
    transition, SENSOR_FAULT once per fault episode, MAX_RUNTIME_CUT once
    per dry-run trip. Reason events precede the PUMP_CUT they caused.
    """

    def __init__(
        self, cut_off_pct: float, resume_pct: float, max_run_seconds: float
    ) -> None:
        for name, value in (
            ("cut_off_pct", cut_off_pct),
            ("resume_pct", resume_pct),
            ("max_run_seconds", max_run_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(
                    f"{name} must be a number, got {type(value).__name__}"
                )
            if math.isnan(value) or not math.isfinite(value):
                raise ValueError(f"{name} must be a finite number")
        if not 0.0 < cut_off_pct <= 100.0:
            raise ValueError(
                f"cut_off_pct must be in (0, 100], got {cut_off_pct}"
            )
        if not 0.0 <= resume_pct < 100.0:
            raise ValueError(
                f"resume_pct must be in [0, 100), got {resume_pct}"
            )
        if resume_pct >= cut_off_pct:
            raise ValueError(
                f"resume_pct ({resume_pct}) must be below cut_off_pct "
                f"({cut_off_pct}) to form a hysteresis band"
            )
        if max_run_seconds <= 0:
            raise ValueError(
                f"max_run_seconds must be > 0, got {max_run_seconds}"
            )

        self.cut_off_pct = float(cut_off_pct)
        self.resume_pct = float(resume_pct)
        self.max_run_seconds = float(max_run_seconds)

        self._cut = False
        self._fault_active = False
        self._run_start_ts: Optional[float] = None
        self._run_start_level: Optional[float] = None

    @staticmethod
    def _is_valid_level(level_pct: Optional[float]) -> bool:
        if level_pct is None:
            return False
        if isinstance(level_pct, bool) or not isinstance(level_pct, (int, float)):
            return False
        if math.isnan(level_pct) or not math.isfinite(level_pct):
            return False
        return 0.0 <= level_pct <= 100.0

    @property
    def is_cut(self) -> bool:
        """Current protection-latch state (True means the pump is held off)."""
        return self._cut

    def reset(self) -> None:
        """Clear the cut latch and runtime tracking (e.g. after servicing)."""
        self._cut = False
        self._fault_active = False
        self._run_start_ts = None
        self._run_start_level = None

    def update(
        self,
        level_pct: Optional[float],
        now_ts: float,
        fault: bool = False,
        pump_requested: bool = True,
    ) -> PumpDecision:
        """Evaluate one sensor sample and decide whether the pump may run."""
        if isinstance(now_ts, bool) or not isinstance(now_ts, (int, float)):
            raise TypeError(f"now_ts must be a number, got {type(now_ts).__name__}")
        if math.isnan(now_ts) or not math.isfinite(now_ts):
            raise ValueError("now_ts must be a finite number")

        events: list = []
        invalid = bool(fault) or not self._is_valid_level(level_pct)

        if invalid and not self._fault_active:
            events.append(SENSOR_FAULT)
        self._fault_active = invalid

        was_cut = self._cut
        if invalid:
            # Fail safe: hold the pump off. The latch releases on the next
            # valid reading below resume_pct via the normal hysteresis path.
            self._cut = True
        else:
            assert level_pct is not None  # narrowed by invalid == False
            level = float(level_pct)
            if not self._cut and level >= self.cut_off_pct:
                self._cut = True
            elif self._cut and level < self.resume_pct:
                self._cut = False

        running = bool(pump_requested) and not self._cut
        if running:
            assert not invalid  # running implies a valid reading held the latch open
            level = float(level_pct)  # type: ignore[arg-type]
            if self._run_start_ts is None:
                self._run_start_ts = float(now_ts)
                self._run_start_level = level
            else:
                assert self._run_start_level is not None
                elapsed = float(now_ts) - self._run_start_ts
                if elapsed < 0:
                    # Clock stepped backwards; restart the window.
                    self._run_start_ts = float(now_ts)
                    self._run_start_level = level
                elif level > self._run_start_level:
                    # Level is rising: the pump is making progress.
                    self._run_start_ts = float(now_ts)
                    self._run_start_level = level
                elif elapsed > self.max_run_seconds:
                    self._cut = True
                    self._run_start_ts = None
                    self._run_start_level = None
                    events.append(MAX_RUNTIME_CUT)
        else:
            self._run_start_ts = None
            self._run_start_level = None

        if self._cut and not was_cut:
            events.append(PUMP_CUT)
        elif not self._cut and was_cut:
            events.append(PUMP_RESUME)

        pump_allowed = bool(pump_requested) and not self._cut
        return PumpDecision(pump_allowed=pump_allowed, events=events)
