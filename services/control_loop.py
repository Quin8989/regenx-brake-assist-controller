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

from time import ticks_diff, ticks_ms

from config.settings import (
    COMMAND_SLEW_A_PER_S,
    CONTROL_LOOP_PERIOD_MS,
    MOTOR_CURRENT_MAX_A,
    REGEN_COMMAND_MAX_A,
    REGEN_LOCKED_RATIO,
    REGEN_LOCK_SNAP_THRESHOLD,
    REGEN_MIN_WHEEL_RPM,
    REGEN_PI_INTEGRAL_LIMIT_A,
    REGEN_PI_KI_A_PER_RPM_S,
    REGEN_PI_KI_DECAY_SCALE,
    REGEN_PI_KP_A_PER_RPM,
    REGEN_TARGET_SLIP_RPM,
    VCAP_SOFT_REGEN_CUTOFF,
)
from core import SystemState
from utils import SlewLimiter, clamp

# Precomputed loop constants
_DT_S = CONTROL_LOOP_PERIOD_MS / 1000.0
_SLEW_DELTA = COMMAND_SLEW_A_PER_S * _DT_S

# Carrier RPM rate limiter: allow free drops (carrier locking) but limit how
# fast carrier_rpm can rise.  This caps the damage from a stale motor-RPM
# telemetry update making the carrier *appear* to unlock suddenly.
# 50 RPM/s ≈ full carrier range over ~1.3 s — fast enough for real unlock,
# slow enough to absorb a single 50–100 ms telemetry gap.
_CARRIER_MAX_RISE_RPM_PER_S = 50.0


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
        # PI target for the slew limiter — updated on fresh edges, ramped every cycle.
        self._regen_pi_target_a = 0.0
        # Held carrier RPM — rate-limited to prevent stale motor RPM spikes.
        self._carrier_rpm = 0.0
        # Timestamp of last fresh wheel edge used for real PI dt measurement.
        # None = no edge yet.
        self._last_edge_ms = None

    def update(self):
        """Compute this cycle's assist/regen requests.

        Behavior summary:
        - If inhibited: zero everything and reset dynamic states.
        - ASSIST state: run assist mapping only; regen zeroed.
        - REGEN state: PI slip controller regulates braking current.
          Between fresh wheel edges the previous regen command is held.
        - Any other state: hold at zero and reset dynamics.
        """
        s = self._state

        # Assist is always computed from scratch each cycle.
        s.assist_command_request = 0.0

        # Inhibit is the master safety gate:
        # no current commands and no integrator windup.
        if s.inhibit_motor_commands:
            s.regen_command_request = 0.0
            self._reset_assist()
            self._reset_regen()
            return

        if s.system_state == SystemState.ASSIST:
            s.regen_command_request = 0.0
            self._reset_regen()
            self._compute_assist()
        elif s.system_state == SystemState.REGEN:
            self._reset_assist()
            self._compute_regen()
        else:
            # OFF / PRECHARGE / FAULT / standstill: remain at zero and clear dynamics.
            # Next entry into ASSIST/REGEN starts from a known smooth baseline.
            s.regen_command_request = 0.0
            self._reset_assist()
            self._reset_regen()

    def _compute_assist(self):
        """Compute forward-drive current request in ASSIST state."""
        s = self._state
        target = clamp(s.requested_level, 0.0, 1.0) * MOTOR_CURRENT_MAX_A
        s.assist_command_request = self._assist_slew.update(target)

    def _compute_regen(self):
        """Compute regen braking current via PI slip control.

        The PI only advances on fresh wheel-speed edges so that wheel and
        motor readings are naturally synchronized.  Between edges the last
        regen command is held — safety gates still run every cycle.
        """
        s = self._state

        # Safety gates — always checked, even between edges.
        if s.cap_voltage_v >= VCAP_SOFT_REGEN_CUTOFF:
            s.regen_command_request = 0.0
            self._reset_regen()
            return
        if not s.wheel_speed_valid:
            s.regen_command_request = 0.0
            self._reset_regen()
            return
        wheel_rpm = max(0.0, s.wheel_speed_rpm)
        if wheel_rpm < REGEN_MIN_WHEEL_RPM:
            s.regen_command_request = 0.0
            self._reset_regen()
            s.gear_carrier_speed_rpm = wheel_rpm
            s.regen_speed_error_rpm = 0.0
            return

        # PI advances only on a fresh wheel edge.
        if s.wheel_speed_fresh:
            # Measure real dt since last edge for accurate integration.
            now_ms = ticks_ms()
            first_edge = self._last_edge_ms is None
            if not first_edge:
                dt_s = max(ticks_diff(now_ms, self._last_edge_ms) / 1000.0, _DT_S)
            else:
                dt_s = _DT_S  # fallback for the very first edge
            self._last_edge_ms = now_ms

            # Pair raw wheel speed with current motor speed for carrier estimation.
            # Raw (unfiltered) wheel RPM avoids the filter lag that creates phantom
            # slip during braking — both signals update at their true sensor rate.
            raw_wheel_rpm = max(0.0, s.wheel_speed_raw_rpm)
            raw_carrier = self._estimate_carrier_rpm(raw_wheel_rpm, s.vesc_mech_rpm)

            # Rate-limit carrier RPM: it can drop freely (carrier locking)
            # but can only rise at a bounded rate.  This prevents a single
            # stale motor-RPM update from spiking carrier and nuking the integral.
            # On the very first edge after reset, accept the raw value as-is.
            if first_edge:
                self._carrier_rpm = raw_carrier
            else:
                max_rise = _CARRIER_MAX_RISE_RPM_PER_S * dt_s
                if raw_carrier > self._carrier_rpm + max_rise:
                    self._carrier_rpm += max_rise
                else:
                    self._carrier_rpm = raw_carrier
            s.gear_carrier_speed_rpm = self._carrier_rpm

            # Fixed deadband — carrier must be this far below wheel speed
            # before the PI commands regen current.
            target_rpm = REGEN_TARGET_SLIP_RPM

            # PI controller
            error_rpm = target_rpm - self._carrier_rpm
            s.regen_speed_error_rpm = error_rpm
            base_current_a = self._pi_update(error_rpm, dt_s)

            self._regen_pi_target_a = clamp(base_current_a, 0.0, REGEN_COMMAND_MAX_A)

        # Slew runs every cycle so ramp rate is time-accurate (~100 Hz).
        s.regen_command_request = self._regen_slew.update(self._regen_pi_target_a)

    # -- Regen helpers (one job each) --

    @staticmethod
    def _estimate_carrier_rpm(wheel_rpm, motor_rpm):
        """Infer carrier slip from wheel and motor speed.

        Carrier locked (braking): motor spins at wheel_rpm × gear_ratio,
        lock_fraction → 1, carrier_rpm → 0 → positive PI error → brake.
        Carrier free (coasting): motor RPM ≈ 0, lock_fraction → 0,
        carrier_rpm → wheel_rpm → negative error → integral stays at 0.

        Lock-fraction deadband: when lock_fraction ≥ LOCK_SNAP_THRESHOLD
        (e.g. 0.90), snap to fully locked (carrier_rpm = 0).  This absorbs
        sensor noise from the 3-magnet wheel hall that would otherwise
        create phantom carrier slip at riding speeds.
        """
        motor_rpm = abs(motor_rpm)
        locked_motor_rpm = max(1e-6, wheel_rpm * REGEN_LOCKED_RATIO)
        lock_fraction = clamp(motor_rpm / locked_motor_rpm, 0.0, 1.0)
        if lock_fraction >= REGEN_LOCK_SNAP_THRESHOLD:
            return 0.0
        return wheel_rpm * (1.0 - lock_fraction)

    def _pi_update(self, error_rpm, dt_s):
        """Standard PI controller: error (RPM) → regen current (A).

        *dt_s* is the measured interval between wheel-speed edges so the
        integral accumulates at the true sensor rate, not the fixed loop rate.

        Asymmetric integration: positive error (carrier locked, need more
        brake) integrates at full KI.  Negative error (apparent slip or
        stale motor RPM spike) decays the integral at KI × DECAY_SCALE.
        This prevents a single bad telemetry reading from nuking the
        accumulated brake command.
        """
        p_term = REGEN_PI_KP_A_PER_RPM * error_rpm

        if error_rpm >= 0.0:
            self._regen_integral_a += REGEN_PI_KI_A_PER_RPM_S * error_rpm * dt_s
        else:
            self._regen_integral_a += (REGEN_PI_KI_A_PER_RPM_S * REGEN_PI_KI_DECAY_SCALE) * error_rpm * dt_s
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
        self._regen_pi_target_a = 0.0
        self._carrier_rpm = 0.0
        self._last_edge_ms = None
