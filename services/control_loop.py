# services/control_loop.py — Motor current command computation
#
# HOW ASSIST AND REGEN WORK (user intent → motor current)
# ========================================================
#
# 1. InputManager reads throttle and wheel speed each cycle:
#      Throttle applied                          → ASSIST request
#      Throttle off + wheel valid + above min    → REGEN request
#      Throttle off + wheel stopped / invalid    → NEUTRAL
#
# 2. StateMachine gates transitions with safety checks (cap voltage, faults).
#    Direct ASSIST ↔ REGEN transitions are allowed.
#
# 3. This module (ControlLoop) converts state + intent into current commands:
#
#    ASSIST:
#      requested_level (0–1) × max current → slew-limited assist command.
#      The VESC inner loop handles actual torque control.
#
#    REGEN:
#      PI slip controller regulates braking current.  The PI loop
#      measures carrier slip and adjusts brake current to hold a
#      fixed slip target.  When the carrier is free (coasting),
#      carrier_rpm ≈ wheel_rpm → negative error → integral floored
#      at 0 → zero brake current naturally.  When the rider brakes
#      and the carrier locks, positive error builds the integral.
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
    COMMAND_SLEW_A_PER_S,
    CONTROL_LOOP_PERIOD_MS,
    MOTOR_CURRENT_MAX_A,
    REGEN_COMMAND_MAX_A,
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
_SLEW_DELTA = COMMAND_SLEW_A_PER_S * _DT_S


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
        # Last carrier RPM from a fresh wheel edge — held between edges to
        # prevent phantom slip from asynchronous sensor update rates.
        # None = no edge yet → assume carrier free (no braking).
        self._held_carrier_rpm = None

    def update(self):
        """Compute this cycle's assist/regen requests.

        Behavior summary:
        - Always starts from zero (0 A requests).
        - If inhibited: zero everything and reset dynamic states.
        - ASSIST state: run assist mapping only.
        - REGEN state: PI slip controller regulates braking current.
        - Any other state: hold at zero and reset dynamics.
        """
        s = self._state

        # Default to zero each cycle; active modes explicitly write non-zero values.
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
            # OFF / PRECHARGE / FAULT / standstill: remain at zero and clear dynamics.
            # Next entry into ASSIST/REGEN starts from a known smooth baseline.
            self._reset_assist()
            self._reset_regen()

    def _compute_assist(self):
        """Compute forward-drive current request in ASSIST state."""
        s = self._state
        target = clamp(s.requested_level, 0.0, 1.0) * MOTOR_CURRENT_MAX_A
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

        if s.wheel_speed_fresh:
            self._held_carrier_rpm = self._estimate_carrier_rpm(wheel_rpm, s.vesc_mech_rpm)
        # Before first fresh edge, assume carrier is free (no braking).
        carrier_rpm = self._held_carrier_rpm if self._held_carrier_rpm is not None else wheel_rpm
        s.gear_carrier_speed_rpm = carrier_rpm

        # Fixed slip target
        target_rpm = wheel_rpm * REGEN_TARGET_SLIP_FRAC

        # PI controller
        # When carrier slip falls below target (carrier_rpm too low), increase
        # regen torque to open the slip back toward target.
        error_rpm = target_rpm - carrier_rpm
        s.regen_speed_error_rpm = error_rpm
        base_current_a = self._pi_update(error_rpm)

        # Smooth and clamp
        target = clamp(base_current_a, 0.0, REGEN_COMMAND_MAX_A)
        s.regen_command_request = self._regen_slew.update(target)

    # -- Regen helpers (one job each) --

    @staticmethod
    def _estimate_carrier_rpm(wheel_rpm, motor_rpm):
        """Infer carrier slip from wheel and motor speed.

        Carrier locked (braking): motor spins at wheel_rpm × gear_ratio,
        lock_fraction → 1, carrier_rpm → 0 → positive PI error → brake.
        Carrier free (coasting): motor RPM ≈ 0, lock_fraction → 0,
        carrier_rpm → wheel_rpm → negative error → integral stays at 0.
        """
        motor_rpm = abs(motor_rpm)
        locked_motor_rpm = max(1e-6, wheel_rpm * REGEN_LOCKED_RATIO)
        lock_fraction = clamp(motor_rpm / locked_motor_rpm, 0.0, 1.0)
        return wheel_rpm * (1.0 - lock_fraction)

    def _pi_update(self, error_rpm):
        """Standard PI controller: error (RPM) → regen current (A)."""
        p_term = REGEN_PI_KP_A_PER_RPM * error_rpm

        self._regen_integral_a += REGEN_PI_KI_A_PER_RPM_S * error_rpm * _DT_S
        self._regen_integral_a = clamp(
            self._regen_integral_a,
            0.0,
            REGEN_PI_INTEGRAL_LIMIT_A,
        )

        return clamp(p_term + self._regen_integral_a, 0.0, REGEN_COMMAND_MAX_A)

    def _reset_assist(self):
        self._assist_slew.reset(0.0)

    def _reset_regen(self):
        self._regen_slew.reset(0.0)
        self._regen_integral_a = 0.0
        self._held_carrier_rpm = None
