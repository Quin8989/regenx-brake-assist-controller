# services/precharge_manager.py — Precharge sequence and motor enable interlocks
#
# Controls precharge path activation, monitors capacitor voltage,
# and inhibits motor activity until the system is electrically ready.
#
# Note: The VESC is OFF while cap voltage is below ~6 V.  During this blind
# phase (Pico running, no telemetry, cap_voltage_v stuck at 0.0) only the
# telemetry grace timer provides fault protection.  Progress windows are
# deferred until the first telemetry packet arrives.  If the cap starts
# above 6 V (residual charge), the VESC boots immediately and progress
# tracking begins on the first watchdog cycle.

from math import exp
from time import ticks_diff, ticks_ms

from config.settings import (
    CAPACITANCE_F,
    PRECHARGE_HARD_TIMEOUT_MS,
    PRECHARGE_MAX_BAD_WINDOWS,
    PRECHARGE_MIN_PROGRESS_RATIO,
    PRECHARGE_PROGRESS_WINDOW_MS,
    PRECHARGE_RESISTANCE_OHM,
    PRECHARGE_SOURCE_V,
    PRECHARGE_TELEMETRY_GRACE_MS,
    PRECHARGE_WATCHDOG_ENABLE,
    VCAP_MIN_OPERATING,
)
from core import FaultCode

# Precompute RC time constant (invariant for given hardware)
_TAU_S = PRECHARGE_RESISTANCE_OHM * CAPACITANCE_F


class PrechargeManager:
    def __init__(self, precharge_io, shared_state, fault_manager):
        self._io = precharge_io
        self._state = shared_state
        self._faults = fault_manager
        self._charging_started = False
        self._start_ms = 0
        self._last_window_ms = 0
        self._last_window_v = 0.0
        self._bad_windows = 0

    def update(self):
        """Run precharge ON/OFF policy each cycle.

        OFF condition:
        - Capacitor voltage at or above operating threshold, OR
        - Any active fault.

        ON condition:
        - No active fault, and capacitor voltage below threshold.
        """
        if (
            self._faults.has_fault()
            or self._state.cap_voltage_v >= VCAP_MIN_OPERATING
        ):
            # OFF: stop precharge output and hold idle state.
            self._set_idle_outputs()
            self._reset_watchdog()
            return

        # ON: keep precharge output active while charging up.
        self._io.enable_precharge()
        self._io.enable_boost()

        if PRECHARGE_WATCHDOG_ENABLE:
            self._run_watchdog()
            if FaultCode.PRECHARGE_STALL in self._state.fault_flags:
                self._set_idle_outputs()

    def _reset_watchdog(self):
        self._charging_started = False
        self._start_ms = 0
        self._last_window_ms = 0
        self._last_window_v = 0.0
        self._bad_windows = 0

    def _run_watchdog(self):
        now = ticks_ms()
        v_now = self._state.cap_voltage_v

        if not self._charging_started:
            self._start_watchdog(now, v_now)
            return

        elapsed_ms = ticks_diff(now, self._start_ms)

        # If telemetry never appears during early charge, something is wrong.
        if (
            self._state.last_vesc_rx_ms == 0
            and elapsed_ms > PRECHARGE_TELEMETRY_GRACE_MS
        ):
            self._trip_watchdog_fault()
            return

        # Absolute backup timeout.
        if elapsed_ms > PRECHARGE_HARD_TIMEOUT_MS:
            self._trip_watchdog_fault()
            return

        # Progress check every fixed window once telemetry is live.
        window_ms = ticks_diff(now, self._last_window_ms)
        if self._state.last_vesc_rx_ms == 0 or window_ms < PRECHARGE_PROGRESS_WINDOW_MS:
            return

        dt_s = window_ms / 1000.0
        expected_gain = (PRECHARGE_SOURCE_V - self._last_window_v) * (
            1.0 - exp(-dt_s / _TAU_S)
        )
        measured_gain = v_now - self._last_window_v

        if (
            expected_gain > 0.05
            and measured_gain < (expected_gain * PRECHARGE_MIN_PROGRESS_RATIO)
        ):
            self._bad_windows += 1
        else:
            self._bad_windows = 0

        self._last_window_ms = now
        self._last_window_v = v_now

        if self._bad_windows >= PRECHARGE_MAX_BAD_WINDOWS:
            self._trip_watchdog_fault()

    def _trip_watchdog_fault(self):
        self._faults.set_fault(FaultCode.PRECHARGE_STALL)

    def _set_idle_outputs(self):
        self._io.disable_all()

    def _start_watchdog(self, now_ms, cap_voltage_v):
        self._charging_started = True
        self._start_ms = now_ms
        self._last_window_ms = now_ms
        self._last_window_v = cap_voltage_v
