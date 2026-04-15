# services/control_loop.py — Motor current command computation
#
# HOW ASSIST AND REGEN WORK (user intent → motor current)
# ========================================================
#
# 1. InputManager reads throttle and motor RPM each cycle:
#      Throttle applied                                → ASSIST request
#      Throttle off + motor RPM above entry threshold  → REGEN request
#      Throttle off + motor RPM below exit threshold   → NEUTRAL
#
# 2. StateMachine gates transitions with safety checks (cap voltage, faults).
#    Direct ASSIST ↔ REGEN transitions are allowed.
#
# 3. This module (ControlLoop) converts state + intent into current commands:
#
#    ASSIST:
#      requested_level (0–1) × max current → positive assist command.
#
#    REGEN:
#      Regen current targets efficiency-optimal recovery:
#        I = (1−η) × λ·ωe / R_phase, clamped to [I_min, I_max].
#      At the target efficiency (default 75%), copper losses are a fixed
#      fraction of mechanical input.  Current still scales linearly with
#      speed (gentle at low RPM, stronger at high RPM), and is clamped to
#      a floor (guaranteed braking feel) and ceiling (thermal limit).
#      Regen is disabled above VCAP_SOFT_REGEN_CUTOFF.
#
#    All outputs pass through an upward-only slew limiter that caps
#    the rate of change to COMMAND_SLEW_RATE_A_PER_S.  Downward
#    changes are immediate (safety: fault shutdown, throttle release).
#
#    Any other state: both commands zero.
#
# 4. CommandManager transmits the computed values to the VESC over UART.
#
# Inputs read from shared state:
#   system_state, inhibit_motor_commands
#   requested_level (0..1), cap_voltage_v
#
# Outputs written to shared state:
#   assist_command_request (A), regen_command_request (A), motor_command_a (A)

from config.settings import (
    COMMAND_SLEW_RATE_A_PER_S,
    CONTROL_LOOP_PERIOD_MS,
    FLUX_LINKAGE_WB,
    MOTOR_COMMAND_LIMIT_A,
    MOTOR_PHASE_RESISTANCE_OHM,
    REGEN_CURRENT_MAX_A,
    REGEN_EFFICIENCY_TARGET,
    VCAP_SOFT_REGEN_CUTOFF,
    VESC_MOTOR_POLE_PAIRS,
)
from core import CommandMode, SystemState
from utils import clamp

import math

# Precomputed constants
_SLEW_DELTA = COMMAND_SLEW_RATE_A_PER_S * (CONTROL_LOOP_PERIOD_MS / 1000.0)
_RPM_TO_ELEC_RAD_S = VESC_MOTOR_POLE_PAIRS * 2.0 * math.pi / 60.0
_REGEN_I_COEFF = (1.0 - REGEN_EFFICIENCY_TARGET) * FLUX_LINKAGE_WB / MOTOR_PHASE_RESISTANCE_OHM


class ControlLoop:
    """Command-shaping layer between state machine and command transmitter.

    Owns output slew limiting.  Safety/state logic and command
    transmission remain separate concerns.
    """

    def __init__(self, shared_state):
        self._state = shared_state
        self._prev_command_a = 0.0

    def update(self):
        """Compute this cycle's motor command.

        Behavior per system state:
        - Inhibited: zero everything.
        - ASSIST: map throttle level to current; regen zeroed.
        - REGEN: set brake ceiling; VESC limits actual current by back-EMF.
        - Other: hold at zero.

        A single slew limiter on the net command smooths transitions.
        Moves toward zero are always immediate (safety); all other
        changes are rate-limited.
        """
        s = self._state

        s.assist_command_request = 0.0
        s.regen_command_request = 0.0

        if not s.inhibit_motor_commands:
            if s.system_state == SystemState.ASSIST:
                self._compute_assist()
            elif s.system_state == SystemState.REGEN:
                self._compute_regen()

        # Single slew limiter on the actual value the VESC receives.
        raw_net = s.assist_command_request - s.regen_command_request
        s.motor_command_a = self._slew(raw_net)
        self._prev_command_a = s.motor_command_a

    def _slew(self, target):
        """Rate-limit the net command sent to the VESC.

        Toward zero → immediate (safety: fault shutdown, throttle release).
        All other changes → rate-limited to _SLEW_DELTA per cycle.
        This naturally handles sign reversals without a separate guard.
        """
        prev = self._prev_command_a
        # Toward zero (same sign, shrinking magnitude) — always immediate
        if prev >= 0 and 0 <= target <= prev:
            return target
        if prev <= 0 and prev <= target <= 0:
            return target
        # Everything else: ramp-up, direction reversal — rate-limit
        delta = target - prev
        if abs(delta) > _SLEW_DELTA:
            return prev + (_SLEW_DELTA if delta > 0 else -_SLEW_DELTA)
        return target

    def _compute_assist(self):
        """Compute forward-drive current request in ASSIST state."""
        s = self._state
        s.assist_command_request = clamp(s.requested_level, 0.0, 1.0) * MOTOR_COMMAND_LIMIT_A

    def _compute_regen(self):
        """Compute regen current via efficiency-optimal model.

        I = (1−η) × λ × ωe / R_phase, clamped to [I_min, I_max].
        At the target efficiency point, copper losses are a known fraction
        of mechanical input — current scales with speed so efficiency is
        constant across the RPM range.
        """
        s = self._state

        if s.cap_voltage_v >= VCAP_SOFT_REGEN_CUTOFF:
            return

        if s.requested_mode != CommandMode.REGEN:
            return

        omega_e = s.vesc_mech_rpm * _RPM_TO_ELEC_RAD_S
        i_regen = _REGEN_I_COEFF * omega_e
        s.regen_command_request = clamp(i_regen, 0.0, REGEN_CURRENT_MAX_A)
