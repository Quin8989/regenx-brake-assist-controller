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
#      requested_level (0–1) × max current → assist command.
#      The VESC inner FOC loop handles actual current control / ramping.
#
#    REGEN:
#      On entry the controller immediately commands REGEN_COMMAND_MAX_A.
#      Each cycle an integral controller compares the ratio of actual
#      motor current to commanded current against REGEN_TARGET_RATIO:
#        ratio < target → carrier slipping → command ramps down
#        ratio > target → motor absorbing well → command ramps up
#        ratio = target → equilibrium → command holds
#      Starting at max ensures the carrier never freezes on entry.
#      Regen is disabled above VCAP_SOFT_REGEN_CUTOFF.
#
#    Any other state: both commands zero, regen target reset.
#
# 4. CommandManager transmits the computed values to the VESC over UART.
#
# Inputs read from shared state:
#   system_state, inhibit_motor_commands
#   requested_level (0..1), cap_voltage_v
#   vesc_motor_current_a
#
# Outputs written to shared state:
#   assist_command_request (A), regen_command_request (A)

from config.settings import (
    CONTROL_LOOP_PERIOD_MS,
    MOTOR_CURRENT_MAX_A,
    REGEN_COMMAND_MAX_A,
    REGEN_KI_A_PER_S,
    REGEN_TARGET_RATIO,
    VCAP_SOFT_REGEN_CUTOFF,
)
from core import SystemState
from utils import clamp

# Precomputed loop constant
_DT_S = CONTROL_LOOP_PERIOD_MS / 1000.0
_KI_DELTA = REGEN_KI_A_PER_S * _DT_S


class ControlLoop:
    """Command-shaping layer between state machine and command transmitter.

    This class owns regen current backoff dynamics.  It intentionally keeps
    these in one place so that safety/state logic remains simple and command
    transmission remains a pure output step.  The VESC's inner FOC loop
    handles actual current ramping — no Pico-side slew limiting is needed.
    """

    def __init__(self, shared_state):
        self._state = shared_state
        # Current regen command target (decays via backoff).
        self._regen_target_a = 0.0

    def update(self):
        """Compute this cycle's assist/regen requests.

        Behavior summary:
        - If inhibited: zero everything and reset regen target.
        - ASSIST state: run assist mapping only; regen zeroed.
        - REGEN state: start at max, back off when actual << commanded.
        - Any other state: hold at zero and reset regen target.
        """
        s = self._state

        # Assist is always computed from scratch each cycle.
        s.assist_command_request = 0.0

        # Inhibit is the master safety gate:
        # no current commands and no dynamic state accumulation.
        if s.inhibit_motor_commands:
            s.regen_command_request = 0.0
            self._regen_target_a = 0.0
            return

        if s.system_state == SystemState.ASSIST:
            s.regen_command_request = 0.0
            self._regen_target_a = 0.0
            self._compute_assist()
        elif s.system_state == SystemState.REGEN:
            self._compute_regen()
        else:
            # OFF / PRECHARGE / FAULT / standstill: remain at zero.
            # Next entry into ASSIST/REGEN starts from a known baseline.
            s.regen_command_request = 0.0
            self._regen_target_a = 0.0

    def _compute_assist(self):
        """Compute forward-drive current request in ASSIST state."""
        s = self._state
        s.assist_command_request = clamp(s.requested_level, 0.0, 1.0) * MOTOR_CURRENT_MAX_A

    def _compute_regen(self):
        """Compute regen braking current via integral ratio tracking.

        Starts at REGEN_COMMAND_MAX_A on entry.  Each cycle the integral
        adjusts the target based on actual/commanded ratio vs setpoint:
          error = ratio - target
          target += KI * error * dt
        Positive error (motor absorbing) ramps up, negative (slipping)
        ramps down.  Starting at max avoids carrier freeze on entry.
        """
        s = self._state

        # Safety gate — always checked every cycle.
        if s.cap_voltage_v >= VCAP_SOFT_REGEN_CUTOFF:
            s.regen_command_request = 0.0
            self._regen_target_a = 0.0
            return

        # On first regen cycle (target still at 0), jump to max.
        if self._regen_target_a == 0.0:
            self._regen_target_a = REGEN_COMMAND_MAX_A
            s.regen_command_request = self._regen_target_a
            return

        # Integral controller on actual/commanded ratio.
        actual_a = abs(s.vesc_motor_current_a)
        commanded_a = s.regen_command_request
        if commanded_a > 0.0:
            error = (actual_a / commanded_a) - REGEN_TARGET_RATIO
            self._regen_target_a += _KI_DELTA * error

        s.regen_command_request = clamp(self._regen_target_a, 0.0, REGEN_COMMAND_MAX_A)
