# services/control_loop.py — Motor current command computation
#
# HOW ASSIST AND REGEN WORK (user intent → motor current)
# ========================================================
#
# 1. InputManager reads throttle and motor RPM each cycle:
#      Throttle applied                                → ASSIST request
#      Throttle off + motor RPM above entry threshold  → REGEN request
#      Throttle off + motor RPM below exit threshold   → coasting (no regen)
#
# 2. SystemSupervisor gates transitions with safety checks (cap voltage, faults).
#    Direct ASSIST ↔ REGEN transitions are allowed.
#
# 3. This module (ControlLoop) converts state + intent into current commands:
#
#    ASSIST:
#      requested_level (0–1) × max current → positive assist command.
#
#    REGEN:
#      Stateless feedforward: I = k × λ·ωe / R_phase, clamped to [0, I_max].
#      k = REGEN_COPPER_LOSS_FRACTION (default 0.25).  Current scales
#      linearly with motor RPM — gentle at low RPM, stronger at high RPM.
#      No integrator, no memory of previous cycles.  Whatever RPM the VESC
#      reports this cycle is the only input.
#
#      The planetary gear + carrier freewheel means motor RPM is
#      proportional to band-brake squeeze force: harder squeeze → carrier
#      held more firmly → more motor RPM → more regen current.
#
#      If motor reaction torque exceeds brake holding force, the carrier
#      slips, RPM drops, current drops, brake catches, RPM recovers —
#      a limit-cycle hunt at ~50–100 Hz.
#
#      Regen is disabled above VCAP_SOFT_REGEN_CUTOFF.
#
#    Current commands are applied directly — no slew limiting.  The VESC
#    FOC loop handles electrical ramping at 20+ kHz.
#
#    Any other state: both commands zero.
#
# 4. The computed values are transmitted to the VESC over UART.
#
# Inputs read from shared state:
#   system_state, inhibit_motor_commands
#   requested_level (0..1 for assist), cap_voltage_v, vesc_mech_rpm
#
# Outputs written to shared state:
#   assist_command_request (A), regen_command_request (A), motor_command_a (A)

from config.settings import (
    FLUX_LINKAGE_WB,
    MOTOR_COMMAND_LIMIT_A,
    MOTOR_PHASE_RESISTANCE_OHM,
    REGEN_COPPER_LOSS_FRACTION,
    REGEN_CURRENT_MAX_A,
    VCAP_SOFT_REGEN_CUTOFF,
    VESC_MOTOR_POLE_PAIRS,
)
from core import SystemState
from utils import clamp

import math

# Precomputed constants
_RPM_TO_ELEC_RAD_S = VESC_MOTOR_POLE_PAIRS * 2.0 * math.pi / 60.0
_REGEN_I_COEFF = REGEN_COPPER_LOSS_FRACTION * FLUX_LINKAGE_WB / MOTOR_PHASE_RESISTANCE_OHM


class ControlLoop:
    """Command-shaping layer between state machine and command transmitter.

    Safety/state logic and command transmission remain separate concerns.
    """

    def __init__(self, shared_state):
        self._state = shared_state

    def update(self):
        """Compute this cycle's motor command.

        Behavior per system state:
        - Inhibited: zero everything.
        - ASSIST: map throttle level to current; regen zeroed.
        - REGEN: efficiency-optimal current from motor RPM.
        - Other: hold at zero.
        """
        s = self._state

        s.assist_command_request = 0.0
        s.regen_command_request = 0.0

        if not s.inhibit_motor_commands:
            if s.system_state == SystemState.ASSIST:
                self._compute_assist()
            elif s.system_state == SystemState.REGEN:
                self._compute_regen()

        s.motor_command_a = s.assist_command_request - s.regen_command_request

    def _compute_assist(self):
        """Compute forward-drive current request in ASSIST state."""
        s = self._state
        s.assist_command_request = clamp(s.requested_level, 0.0, 1.0) * MOTOR_COMMAND_LIMIT_A

    def _compute_regen(self):
        """Compute regen current via efficiency-optimal model.

        I = k × λ × ωe / R_phase, clamped to [0, I_max].
        k = REGEN_COPPER_LOSS_FRACTION — the fraction of mechanical input
        allowed as copper loss.  Current scales with speed so efficiency
        is constant across the RPM range.
        """
        s = self._state

        if s.cap_voltage_v >= VCAP_SOFT_REGEN_CUTOFF:
            return

        omega_e = s.vesc_mech_rpm * _RPM_TO_ELEC_RAD_S
        i_regen = _REGEN_I_COEFF * omega_e
        s.regen_command_request = clamp(i_regen, 0.0, REGEN_CURRENT_MAX_A)
