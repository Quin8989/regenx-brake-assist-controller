# services/input_manager.py — Read rider inputs and determine requested mode
#
# Mode decision (three-state):
#   Throttle applied              → ASSIST  (rider wants forward power)
#   Throttle off + carrier locked → REGEN   (rider is braking, motor coupled)
#   Throttle off + carrier free   → NEUTRAL (coasting, freewheel disengaged)
#
# Carrier lock detection:
#   The geared hub has a one-way freewheel clutch on the planetary carrier.
#   When the rider applies the mechanical brake, the carrier locks and the
#   wheel drives the motor through the gear train.  The VESC reports motor
#   RPM from back-EMF even with no commanded current, so we can infer:
#     motor_rpm ≈ wheel_rpm × gear_ratio → carrier locked → rider is braking
#     motor_rpm ≈ 0                       → carrier free   → rider is coasting
#
#   Hysteresis prevents chatter at the engagement boundary:
#     Enter REGEN when carrier slip < REGEN_ENGAGE_SLIP_FRAC   (30%)
#     Exit  REGEN when carrier slip > REGEN_DISENGAGE_SLIP_FRAC (50%)

from config.settings import (
    REGEN_DISENGAGE_SLIP_FRAC,
    REGEN_ENGAGE_SLIP_FRAC,
    REGEN_LOCKED_RATIO,
    REGEN_MIN_WHEEL_RPM,
)
from core import CommandMode


class InputManager:
    def __init__(self, throttle_driver, shared_state, wheel_speed_driver=None):
        self._throttle = throttle_driver
        self._state = shared_state
        self._wheel = wheel_speed_driver
        self._in_regen = False

    def update(self):
        """Sample rider inputs and update shared_state."""
        if self._wheel is not None:
            wheel_rpm, wheel_valid = self._wheel.update()
            self._state.wheel_speed_rpm = wheel_rpm
            self._state.wheel_speed_valid = wheel_valid

        self._throttle.update()
        t = self._throttle
        s = self._state
        s.throttle_raw = t.raw
        s.throttle_valid = t.is_valid

        # Rider intent:
        #   Throttle applied              → ASSIST
        #   Throttle off + carrier locked  → REGEN  (mechanical brake engaged)
        #   Throttle off + carrier free    → NEUTRAL (coasting)
        if t.is_valid and t.fraction > 0.0:
            s.requested_mode = CommandMode.ASSIST
            s.requested_level = t.fraction
            self._in_regen = False
        elif self._carrier_is_locked():
            s.requested_mode = CommandMode.REGEN
            s.requested_level = 1.0
            self._in_regen = True
        else:
            s.requested_mode = CommandMode.NEUTRAL
            s.requested_level = 0.0
            self._in_regen = False

    def _carrier_is_locked(self):
        """Infer whether the rider is braking from motor-to-wheel coupling.

        When the freewheel clutch is disengaged (coast), motor RPM ≈ 0.
        When the rider brakes the carrier, it locks and the wheel drives
        the motor at ≈ wheel_rpm × gear_ratio.

        Uses hysteresis: tighter threshold to enter REGEN, looser to exit.
        """
        s = self._state
        if not s.wheel_speed_valid or s.wheel_speed_rpm < REGEN_MIN_WHEEL_RPM:
            return False

        locked_motor_rpm = s.wheel_speed_rpm * REGEN_LOCKED_RATIO
        lock_frac = min(max(0.0, s.vesc_mech_rpm) / locked_motor_rpm, 1.0)
        carrier_slip = 1.0 - lock_frac

        if self._in_regen:
            return carrier_slip < REGEN_DISENGAGE_SLIP_FRAC
        return carrier_slip < REGEN_ENGAGE_SLIP_FRAC
