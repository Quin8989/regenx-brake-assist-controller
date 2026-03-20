# services/control_loop.py — Motor current command computation
#
# HOW ASSIST AND REGEN WORK (user intent → motor current)
# ========================================================
#
# 1. InputManager reads throttle, wheel speed, and motor speed each cycle:
#      Throttle applied                          → ASSIST request
#      Throttle off + carrier locked (braking)   → REGEN request
#      Throttle off + carrier free (coasting)    → NEUTRAL
#
#    Carrier lock is inferred from motor RPM: when the rider squeezes the
#    mechanical brake, the carrier locks and the wheel drives the motor
#    through the gear train (motor RPM ≈ wheel RPM × gear ratio).
#    When coasting, the freewheel clutch disengages and motor RPM ≈ 0.
#
# 2. StateMachine gates transitions with safety checks (cap voltage, faults):
#      READY + ASSIST request → ASSIST state
#      READY + REGEN request  → REGEN state
#      Direct ASSIST ↔ REGEN transitions are allowed.
#
# 3. This module (ControlLoop) converts state + intent into current commands:
#
#    ASSIST:
#      requested_level (0–1) × max current → slew-limited assist command.
#      The VESC inner loop handles actual torque control.
#
#    REGEN:
#      PI slip controller regulates braking current to supplement the
#      rider's mechanical braking.  The PI loop measures carrier slip
#      and adjusts brake current to hold a fixed slip target (2%).
#      Regen is disabled entirely above VCAP_SOFT_REGEN_CUTOFF.
#
#    Any other state: both commands zero, all dynamics reset.
#
# 4. CommandManager transmits the computed values to the VESC over UART.
#
# Inputs read from shared state:
#   system_state, inhibit_motor_commands
#   requested_level (0..1), cap_voltage_v
#   wheel_speed_rpm, wheel_speed_valid, vesc_mech_rpm
#
# Outputs written to shared state:
#   assist_command_request (A), regen_command_request (A)
#   gear_carrier_speed_rpm (estimated), regen_speed_error_rpm (PI debug)

from config.settings import (
    ASSIST_CURRENT_LIMIT_A,
    CONTROL_LOOP_PERIOD_MS,
    REGEN_CURRENT_LIMIT_A,
    REGEN_LOCKED_RATIO,
    REGEN_MIN_WHEEL_RPM,
    REGEN_PI_INTEGRAL_LIMIT_A,
    REGEN_PI_KI_A_PER_RPM_S,
    REGEN_PI_KP_A_PER_RPM,
    REGEN_TARGET_SLIP_FRAC,
    VCAP_SOFT_REGEN_CUTOFF,
)
from core import SystemState
from utils import SlewLimiter, clamp

# Precomputed loop constants
_DT_S = CONTROL_LOOP_PERIOD_MS / 1000.0
_SLEW_A_PER_S = 20.0
_SLEW_DELTA = _SLEW_A_PER_S * _DT_S


class ControlLoop:
    """Command-shaping layer between state machine and command transmitter.

    This class owns dynamic control behavior (slew-rate limiting and PI control).
    It intentionally keeps these dynamics in one place so that safety/state logic
    remains simple and command transmission remains a pure output step.
    """

    def __init__(self, shared_state):
        self._state = shared_state
        # Slew limiters prevent abrupt torque/current steps that feel harsh and can
        # stress drivetrain/electronics.
        self._assist_slew = SlewLimiter(max_delta=_SLEW_DELTA)
        self._regen_slew = SlewLimiter(max_delta=_SLEW_DELTA)
        # Integral state for regen PI loop. Represents accumulated correction (A).
        self._regen_integral_a = 0.0

    def update(self):
        """Compute this cycle's assist/regen requests.

        Behavior summary:
        - Always starts from neutral (0 A requests).
        - If inhibited: zero everything and reset dynamic states.
        - ASSIST state: run assist mapping only.
        - REGEN state: PI slip controller regulates braking current.
        - Any other state: hold neutral and reset dynamics.
        """
        s = self._state

        # Default to neutral each cycle; active modes explicitly write non-zero values.
        s.assist_command_request = 0.0
        s.regen_command_request = 0.0

        # Inhibit is the master safety gate:
        # no current commands and no integrator windup.
        if s.inhibit_motor_commands:
            self._reset_assist()
            self._reset_regen()
            return

        if s.system_state == SystemState.ASSIST:
            self._reset_regen()
            self._compute_assist()
        elif s.system_state == SystemState.REGEN:
            self._reset_assist()
            self._compute_regen()
        else:
            # READY / OFF / PRECHARGE / FAULT: remain neutral and clear dynamics.
            # Next entry into ASSIST/REGEN starts from a known smooth baseline.
            self._reset_assist()
            self._reset_regen()

    def _compute_assist(self):
        """Compute forward-drive current request in ASSIST state."""
        s = self._state
        target = clamp(s.requested_level, 0.0, 1.0) * ASSIST_CURRENT_LIMIT_A
        s.assist_command_request = self._assist_slew.update(target)

    def _compute_regen(self):
        """Compute regen braking current via PI slip control."""
        s = self._state

        if s.cap_voltage_v >= VCAP_SOFT_REGEN_CUTOFF:
            self._reset_regen()
            return
        if not s.wheel_speed_valid:
            self._reset_regen()
            return
        wheel_rpm = max(0.0, s.wheel_speed_rpm)
        if wheel_rpm < REGEN_MIN_WHEEL_RPM:
            self._reset_regen()
            s.gear_carrier_speed_rpm = wheel_rpm
            s.regen_speed_error_rpm = 0.0
            return

        # Carrier slip estimate
        carrier_rpm = self._estimate_carrier_rpm(wheel_rpm, s.vesc_mech_rpm)
        s.gear_carrier_speed_rpm = carrier_rpm

        # Fixed slip target
        target_rpm = wheel_rpm * REGEN_TARGET_SLIP_FRAC

        # PI controller
        error_rpm = carrier_rpm - target_rpm
        s.regen_speed_error_rpm = error_rpm
        base_current_a = self._pi_update(error_rpm)

        # Scale by rider authority and smooth
        rider = clamp(s.requested_level, 0.0, 1.0)
        target = clamp(base_current_a * rider, 0.0, REGEN_CURRENT_LIMIT_A)
        s.regen_command_request = self._regen_slew.update(target)

    # -- Regen helpers (one job each) --

    @staticmethod
    def _estimate_carrier_rpm(wheel_rpm, motor_rpm):
        """Infer carrier slip from wheel and motor speed.

        When the rider brakes, the carrier locks and the motor spins at
        wheel_rpm × gear_ratio (lock_fraction → 1, carrier_rpm → 0).
        When coasting, the freewheel disengages and motor RPM ≈ 0
        (lock_fraction → 0, carrier_rpm → wheel_rpm = full slip).
        """
        motor_rpm = max(0.0, motor_rpm)
        locked_motor_rpm = max(1e-6, wheel_rpm * REGEN_LOCKED_RATIO)
        lock_fraction = clamp(motor_rpm / locked_motor_rpm, 0.0, 1.0)
        return wheel_rpm * (1.0 - lock_fraction)

    def _pi_update(self, error_rpm):
        """Standard PI controller: error (RPM) → regen current (A)."""
        p_term = REGEN_PI_KP_A_PER_RPM * error_rpm

        self._regen_integral_a += REGEN_PI_KI_A_PER_RPM_S * error_rpm * _DT_S
        self._regen_integral_a = clamp(
            self._regen_integral_a,
            -REGEN_PI_INTEGRAL_LIMIT_A,
            REGEN_PI_INTEGRAL_LIMIT_A,
        )

        return clamp(p_term + self._regen_integral_a, 0.0, REGEN_CURRENT_LIMIT_A)

    def _reset_assist(self):
        self._assist_slew.reset(0.0)

    def _reset_regen(self):
        self._regen_slew.reset(0.0)
        self._regen_integral_a = 0.0
