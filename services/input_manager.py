# services/input_manager.py — Read rider inputs and determine requested mode
#
# Mode decision (two-state + safety gate):
#   Throttle applied                        → ASSIST  (rider wants forward power)
#   Throttle off + wheel valid + above min  → REGEN   (PI controller decides current)
#   Throttle off + wheel invalid / too slow  → NEUTRAL (standstill / no data)
#
# The PI slip controller in ControlLoop naturally produces zero brake current
# when the carrier is free (coasting) because carrier_rpm ≈ wheel_rpm gives
# a large negative error that the integral floor clamps to zero.  When the
# rider squeezes the mechanical brake and the carrier locks, positive slip
# error builds the integral and brake current ramps up.  This eliminates
# the need for carrier-lock detection and hysteresis in the mode decision,
# preventing the engage/disengage chatter that reset the PI integral.

from time import ticks_diff, ticks_ms

from config.settings import (
    REGEN_MIN_WHEEL_RPM,
    WHEEL_SPEED_MAX_ACCEL_KPH_PER_S,
    WHEEL_SPEED_MAX_DECEL_KPH_PER_S,
    WHEEL_SPEED_INVALID_HOLD_MS,
    WHEEL_SPEED_MAX_RPM,
    WHEEL_CIRCUMFERENCE_M,
)
from core import CommandMode
from utils import clamp


_NOMINAL_FILTER_DT_MS = 10
_KPH_TO_RPM = (1000.0 / 60.0) / max(WHEEL_CIRCUMFERENCE_M, 1e-6)
_MAX_ACCEL_RPM_PER_S = WHEEL_SPEED_MAX_ACCEL_KPH_PER_S * _KPH_TO_RPM
_MAX_DECEL_RPM_PER_S = WHEEL_SPEED_MAX_DECEL_KPH_PER_S * _KPH_TO_RPM


class InputManager:
    def __init__(self, throttle_driver, shared_state, wheel_speed_driver=None):
        self._throttle = throttle_driver
        self._state = shared_state
        self._wheel = wheel_speed_driver
        self._filtered_wheel_rpm = 0.0
        self._last_wheel_update_ms = None

    def update(self):
        """Sample rider inputs and update shared_state."""
        if self._wheel is not None:
            wheel_rpm, wheel_valid, wheel_fresh = self._wheel.update()
            self._update_wheel_speed(wheel_rpm, wheel_valid, wheel_fresh)

        self._throttle.update()
        t = self._throttle
        s = self._state
        s.throttle_raw = t.raw
        s.throttle_valid = t.is_valid

        # Rider intent:
        #   Throttle applied                         → ASSIST
        #   Throttle off + wheel valid + above min   → REGEN
        #   Otherwise                                → NEUTRAL
        if t.is_valid and t.fraction > 0.0:
            s.requested_mode = CommandMode.ASSIST
            s.requested_level = t.fraction
        elif s.wheel_speed_valid and s.wheel_speed_rpm >= REGEN_MIN_WHEEL_RPM:
            s.requested_mode = CommandMode.REGEN
            s.requested_level = 1.0
        else:
            s.requested_mode = CommandMode.NEUTRAL
            s.requested_level = 0.0

    def _update_wheel_speed(self, raw_wheel_rpm, wheel_valid, wheel_fresh):
        s = self._state
        s.wheel_speed_fresh = wheel_fresh
        now_ms = ticks_ms()
        if not wheel_valid:
            if self._last_wheel_update_ms is not None:
                invalid_age_ms = ticks_diff(now_ms, self._last_wheel_update_ms)
                if invalid_age_ms <= WHEEL_SPEED_INVALID_HOLD_MS:
                    s.wheel_speed_rpm = self._filtered_wheel_rpm
                    s.wheel_speed_valid = self._filtered_wheel_rpm > 0.0
                    return
            self._filtered_wheel_rpm = 0.0
            self._last_wheel_update_ms = None
            s.wheel_speed_rpm = 0.0
            s.wheel_speed_valid = False
            return

        raw_wheel_rpm = max(0.0, raw_wheel_rpm)

        if raw_wheel_rpm > WHEEL_SPEED_MAX_RPM:
            if self._last_wheel_update_ms is not None:
                s.wheel_speed_rpm = self._filtered_wheel_rpm
                s.wheel_speed_valid = True
            else:
                s.wheel_speed_rpm = 0.0
                s.wheel_speed_valid = False
            return

        if self._last_wheel_update_ms is None:
            self._filtered_wheel_rpm = raw_wheel_rpm
            self._last_wheel_update_ms = now_ms
            s.wheel_speed_rpm = self._filtered_wheel_rpm
            s.wheel_speed_valid = True
            return

        dt_ms = max(ticks_diff(now_ms, self._last_wheel_update_ms), _NOMINAL_FILTER_DT_MS)
        dt_s = dt_ms / 1000.0

        min_rpm = max(0.0, self._filtered_wheel_rpm - (_MAX_DECEL_RPM_PER_S * dt_s))
        max_rpm = self._filtered_wheel_rpm + (_MAX_ACCEL_RPM_PER_S * dt_s)
        self._filtered_wheel_rpm = clamp(raw_wheel_rpm, min_rpm, max_rpm)
        self._last_wheel_update_ms = now_ms
        s.wheel_speed_rpm = self._filtered_wheel_rpm
        s.wheel_speed_valid = True

