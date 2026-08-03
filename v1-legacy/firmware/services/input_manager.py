# services/input_manager.py — Read rider inputs and determine requested mode
#
# Mode decision (two-state + holdoff):
#   Throttle applied                                → ASSIST  (rider wants forward power)
#   Throttle off + motor RPM rising above threshold → REGEN   (carrier locked by brake)
#   Throttle off + motor RPM below threshold        → REGEN with level 0 (coasting)
#
# When the rider releases the throttle, a brief holdoff period suppresses
# regen entry to prevent false triggers from motor inertia after assist.
# Once the holdoff expires, regen is allowed when motor RPM exceeds the
# entry threshold.  Regen exits when motor RPM drops below a lower exit
# threshold (hysteresis prevents chatter).

from time import ticks_diff, ticks_ms

from config.settings import (
    REGEN_ENTRY_RPM,
    REGEN_EXIT_RPM,
    REGEN_HOLDOFF_MS,
)
from core import CommandMode


class InputManager:
    def __init__(self, throttle_driver, shared_state):
        self._throttle = throttle_driver
        self._state = shared_state
        # Post-assist holdoff — timestamp when throttle last went to zero.
        # None means throttle is currently active (no holdoff in progress).
        self._throttle_off_ms = None
        # Regen-active flag for hysteresis (entry vs. exit threshold).
        self._regen_active = False

    def update(self):
        """Sample rider inputs and update shared_state."""
        self._throttle.update()
        t = self._throttle
        s = self._state
        s.throttle_raw = t.raw
        s.throttle_valid = t.is_valid

        # Rider intent:
        #   Throttle applied → ASSIST (always wins)
        #   Throttle off + holdoff expired + motor RPM above threshold → REGEN (level 1)
        #   Otherwise → REGEN with level 0 (coasting / standstill)
        if t.is_valid and t.fraction > 0.0:
            s.requested_mode = CommandMode.ASSIST
            s.requested_level = t.fraction
            # Reset holdoff — will start counting when throttle releases.
            self._throttle_off_ms = None
            self._regen_active = False
        else:
            self._decide_regen(s)

    def _decide_regen(self, s):
        """Determine active REGEN vs idle REGEN when throttle is off."""
        now_ms = ticks_ms()
        # Only positive motor RPM → forward wheel motion through planetary gear.
        # Negative RPM means the bike is rolling backward — regen in that
        # direction would brake the wrong way and confuse the rider.
        motor_rpm = s.vesc_mech_rpm

        # Start holdoff timer on first cycle with throttle off.
        if self._throttle_off_ms is None:
            self._throttle_off_ms = now_ms

        holdoff_elapsed = ticks_diff(now_ms, self._throttle_off_ms)

        if holdoff_elapsed < REGEN_HOLDOFF_MS:
            # Still in holdoff window — stay idle.
            s.requested_mode = CommandMode.REGEN
            s.requested_level = 0.0
            self._regen_active = False
            return

        # Hysteresis: entry threshold to engage, exit threshold to disengage.
        if self._regen_active:
            if motor_rpm < REGEN_EXIT_RPM:
                self._regen_active = False
        else:
            if motor_rpm >= REGEN_ENTRY_RPM:
                self._regen_active = True

        if self._regen_active:
            s.requested_mode = CommandMode.REGEN
            s.requested_level = 1.0
        else:
            s.requested_mode = CommandMode.REGEN
            s.requested_level = 0.0

