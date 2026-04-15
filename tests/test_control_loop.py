# tests/test_control_loop.py — ControlLoop assist mapping, regen ceiling, slew limiting

import math
import tests.conftest as _ct
from config.settings import (
    COMMAND_SLEW_RATE_A_PER_S,
    CONTROL_LOOP_PERIOD_MS,
    FLUX_LINKAGE_WB,
    MOTOR_COMMAND_LIMIT_A,
    MOTOR_PHASE_RESISTANCE_OHM,
    REGEN_CURRENT_MAX_A,
    REGEN_EFFICIENCY_TARGET,
    VESC_MOTOR_POLE_PAIRS,
)
from core import CommandMode, SharedState, SystemState
from services.control_loop import ControlLoop

# Mirror the implementation constant for assertions.
_SLEW_DELTA = COMMAND_SLEW_RATE_A_PER_S * (CONTROL_LOOP_PERIOD_MS / 1000.0)

# Cycles needed to slew from 0 to MOTOR_COMMAND_LIMIT_A.
_CYCLES_TO_MAX = int(MOTOR_COMMAND_LIMIT_A / _SLEW_DELTA) + 1


def _make():
    state = SharedState()
    cl = ControlLoop(state)
    return state, cl


def _ready_regen(state, cap_v=25.0, rpm=200.0):
    """Set shared state to a standard regen scenario.

    rpm defaults to 200 (above saturation) for full-ceiling tests.
    """
    state.system_state = SystemState.REGEN
    state.inhibit_motor_commands = False
    state.cap_voltage_v = cap_v
    state.requested_mode = CommandMode.REGEN
    state.vesc_mech_rpm = rpm


def _ramp_regen(s, cl, n):
    """Run n regen cycles."""
    for _ in range(n):
        cl.update()


# ---- Inhibit ----

class TestInhibit:
    def test_inhibit_zeros_output(self):
        s, cl = _make()
        s.system_state = SystemState.ASSIST
        s.inhibit_motor_commands = True
        s.requested_level = 1.0
        cl.update()
        assert s.assist_command_request == 0.0
        assert s.regen_command_request == 0.0
        assert s.motor_command_a == 0.0

    def test_inhibit_resets_slew_state(self):
        """After inhibit, next ramp starts from zero."""
        s, cl = _make()
        s.system_state = SystemState.ASSIST
        s.inhibit_motor_commands = False
        s.requested_level = 1.0
        for _ in range(5):
            cl.update()
        assert s.motor_command_a > 0.0

        s.inhibit_motor_commands = True
        cl.update()
        assert s.motor_command_a == 0.0

        # Re-enable — first cycle starts from slew floor, not previous peak
        s.inhibit_motor_commands = False
        s.system_state = SystemState.ASSIST
        s.requested_level = 1.0
        cl.update()
        assert abs(s.motor_command_a - _SLEW_DELTA) < 0.01


# ---- ASSIST ----

class TestAssist:
    def test_zero_request_zero_output(self):
        s, cl = _make()
        s.system_state = SystemState.ASSIST
        s.inhibit_motor_commands = False
        s.requested_level = 0.0
        cl.update()
        assert s.motor_command_a == 0.0

    def test_first_cycle_slew_limited(self):
        """First assist cycle from rest outputs one slew step, not full max."""
        s, cl = _make()
        s.system_state = SystemState.ASSIST
        s.inhibit_motor_commands = False
        s.requested_level = 1.0
        cl.update()
        assert abs(s.motor_command_a - _SLEW_DELTA) < 0.01

    def test_full_request_reaches_max_after_ramp(self):
        """After enough cycles, assist reaches MOTOR_COMMAND_LIMIT_A."""
        s, cl = _make()
        s.system_state = SystemState.ASSIST
        s.inhibit_motor_commands = False
        s.requested_level = 1.0
        for _ in range(_CYCLES_TO_MAX):
            cl.update()
        assert abs(s.motor_command_a - MOTOR_COMMAND_LIMIT_A) < 0.01

    def test_half_request_reaches_half_max(self):
        """Steady-state half throttle gives half max current."""
        s, cl = _make()
        s.system_state = SystemState.ASSIST
        s.inhibit_motor_commands = False
        s.requested_level = 0.5
        half = MOTOR_COMMAND_LIMIT_A / 2.0
        cycles_needed = int(half / _SLEW_DELTA) + 1
        for _ in range(cycles_needed):
            cl.update()
        assert abs(s.motor_command_a - half) < 0.01

    def test_regen_stays_zero_during_assist(self):
        s, cl = _make()
        s.system_state = SystemState.ASSIST
        s.inhibit_motor_commands = False
        s.requested_level = 0.5
        cl.update()
        assert s.regen_command_request == 0.0

    def test_assist_clamped_to_limit(self):
        """requested_level > 1.0 is clamped; output cannot exceed max."""
        s, cl = _make()
        s.system_state = SystemState.ASSIST
        s.inhibit_motor_commands = False
        s.requested_level = 2.0
        for _ in range(_CYCLES_TO_MAX):
            cl.update()
        assert abs(s.motor_command_a - MOTOR_COMMAND_LIMIT_A) < 0.01

    def test_downward_is_immediate(self):
        """Releasing throttle drops output to zero in one cycle."""
        s, cl = _make()
        s.system_state = SystemState.ASSIST
        s.inhibit_motor_commands = False
        s.requested_level = 1.0
        for _ in range(_CYCLES_TO_MAX):
            cl.update()
        assert s.motor_command_a > 0.0

        s.requested_level = 0.0
        cl.update()
        assert s.motor_command_a == 0.0


# ---- Slew limiter ----

class TestSlewLimiter:
    def test_upward_steps_are_bounded(self):
        """Each cycle can increase by at most _SLEW_DELTA."""
        s, cl = _make()
        s.system_state = SystemState.ASSIST
        s.inhibit_motor_commands = False
        s.requested_level = 1.0
        prev = 0.0
        for _ in range(5):
            cl.update()
            delta = s.motor_command_a - prev
            assert delta <= _SLEW_DELTA + 0.001
            prev = s.motor_command_a

    def test_downward_immediate_assist(self):
        """Downward assist change is not slew limited."""
        s, cl = _make()
        s.system_state = SystemState.ASSIST
        s.inhibit_motor_commands = False
        s.requested_level = 1.0
        for _ in range(10):
            cl.update()
        high = s.motor_command_a
        assert high > 0.0

        s.requested_level = 0.0
        cl.update()
        assert s.motor_command_a == 0.0

    def test_downward_immediate_regen(self):
        """Regen drops to zero immediately on mode switch."""
        s, cl = _make()
        _ready_regen(s, cap_v=25.0)
        _ramp_regen(s, cl, 10)
        assert s.motor_command_a < 0.0  # negative = regen

        s.requested_mode = CommandMode.NEUTRAL
        cl.update()
        assert s.motor_command_a == 0.0

    def test_regen_first_cycle_slew_limited(self):
        """First regen cycle outputs slew delta, not full max."""
        s, cl = _make()
        _ready_regen(s, cap_v=25.0)
        cl.update()
        assert abs(s.motor_command_a - (-_SLEW_DELTA)) < 0.01

    def test_slew_resets_after_inhibit(self):
        """Inhibit resets slew memory — next ramp starts from zero."""
        s, cl = _make()
        _ready_regen(s, cap_v=25.0)
        _ramp_regen(s, cl, 10)
        assert s.motor_command_a < 0.0

        s.inhibit_motor_commands = True
        cl.update()

        s.inhibit_motor_commands = False
        _ready_regen(s, cap_v=25.0)
        cl.update()
        assert abs(s.motor_command_a - (-_SLEW_DELTA)) < 0.01

    def test_sign_reversal_is_slew_limited(self):
        """ASSIST→REGEN sign change is rate-limited through zero."""
        s, cl = _make()
        s.system_state = SystemState.ASSIST
        s.inhibit_motor_commands = False
        s.requested_level = 1.0
        for _ in range(_CYCLES_TO_MAX):
            cl.update()
        peak = s.motor_command_a
        assert peak > 0.0

        # Instant switch to regen — raw net jumps from +45 to -45
        _ready_regen(s, cap_v=25.0)
        cl.update()
        # Must not jump directly to negative — should step down by _SLEW_DELTA
        assert s.motor_command_a == peak - _SLEW_DELTA

    def test_regen_to_assist_is_slew_limited(self):
        """REGEN→ASSIST sign change is rate-limited through zero."""
        s, cl = _make()
        _ready_regen(s, cap_v=25.0)
        _ramp_regen(s, cl, 20)
        trough = s.motor_command_a
        assert trough < 0.0

        # Instant switch to assist
        s.system_state = SystemState.ASSIST
        s.inhibit_motor_commands = False
        s.requested_level = 1.0
        s.requested_mode = CommandMode.ASSIST
        cl.update()
        # Must not jump directly to positive — should step up by _SLEW_DELTA
        assert s.motor_command_a == trough + _SLEW_DELTA


# ---- REGEN ----

class TestRegen:
    def test_regen_zero_when_above_soft_cutoff(self):
        s, cl = _make()
        _ready_regen(s, cap_v=41.0)
        cl.update()
        assert s.regen_command_request == 0.0

    def test_regen_sets_ceiling_immediately(self):
        """Regen request is REGEN_CURRENT_MAX_A at very high RPM."""
        s, cl = _make()
        _ready_regen(s, cap_v=25.0, rpm=800.0)  # very high RPM saturates ceiling
        cl.update()
        assert s.regen_command_request == REGEN_CURRENT_MAX_A

    def test_regen_output_negative_after_slew(self):
        """Motor command goes negative during regen (slew-limited)."""
        s, cl = _make()
        _ready_regen(s, cap_v=25.0, rpm=800.0)
        _ramp_regen(s, cl, _CYCLES_TO_MAX)
        assert abs(s.motor_command_a - (-REGEN_CURRENT_MAX_A)) < 0.01

    def test_regen_output_consistent_regardless_of_level(self):
        """Rider throttle level does not affect regen output."""
        s1, cl1 = _make()
        _ready_regen(s1, cap_v=25.0)
        s1.requested_level = 1.0
        cl1.update()

        s2, cl2 = _make()
        _ready_regen(s2, cap_v=25.0)
        s2.requested_level = 0.5
        cl2.update()

        assert s1.regen_command_request == s2.regen_command_request

    def test_regen_clamped_to_limit(self):
        s, cl = _make()
        _ready_regen(s, cap_v=20.0, rpm=800.0)
        cl.update()
        assert s.regen_command_request == REGEN_CURRENT_MAX_A

    def test_assist_stays_zero_during_regen(self):
        s, cl = _make()
        _ready_regen(s)
        cl.update()
        assert s.assist_command_request == 0.0

    def test_regen_zero_when_requested_mode_neutral(self):
        """REGEN state but InputManager says NEUTRAL → no current."""
        s, cl = _make()
        _ready_regen(s, cap_v=25.0)
        s.requested_mode = CommandMode.NEUTRAL
        cl.update()
        assert s.regen_command_request == 0.0
        assert s.assist_command_request == 0.0

    def test_regen_proportional_to_rpm(self):
        """Doubling RPM doubles regen current (in the linear region)."""
        # Use RPMs where the model is above floor and below ceiling
        s1, cl1 = _make()
        _ready_regen(s1, cap_v=25.0, rpm=60.0)
        cl1.update()
        i_low = s1.regen_command_request

        s2, cl2 = _make()
        _ready_regen(s2, cap_v=25.0, rpm=120.0)
        cl2.update()
        i_high = s2.regen_command_request

        assert i_low > 0.0
        assert i_high < REGEN_CURRENT_MAX_A
        assert abs(i_high - 2.0 * i_low) < 0.2

    def test_regen_zero_at_zero_rpm(self):
        """At zero RPM, regen command is zero (no back-EMF)."""
        s, cl = _make()
        _ready_regen(s, cap_v=25.0, rpm=0.0)
        cl.update()
        assert s.regen_command_request == 0.0

    def test_regen_clamped_at_high_rpm(self):
        """At very high RPM, regen is clamped to REGEN_CURRENT_MAX_A."""
        s, cl = _make()
        _ready_regen(s, cap_v=25.0, rpm=800.0)
        cl.update()
        assert s.regen_command_request == REGEN_CURRENT_MAX_A

    def test_regen_small_at_low_rpm(self):
        """At low RPM, regen is a small fraction of ceiling."""
        s, cl = _make()
        _ready_regen(s, cap_v=25.0, rpm=30.0)
        cl.update()
        assert 0.0 < s.regen_command_request < REGEN_CURRENT_MAX_A * 0.25

    def test_regen_matches_physics(self):
        """Spot-check: regen at 80 RPM matches efficiency-optimal formula."""
        s, cl = _make()
        _ready_regen(s, cap_v=25.0, rpm=80.0)
        cl.update()
        omega_e = 80.0 * VESC_MOTOR_POLE_PAIRS * 2.0 * math.pi / 60.0
        expected = (1.0 - REGEN_EFFICIENCY_TARGET) * FLUX_LINKAGE_WB * omega_e / MOTOR_PHASE_RESISTANCE_OHM
        assert abs(s.regen_command_request - expected) < 0.1


# ---- Neutral / other states ----

class TestNeutralStates:
    def test_off_state_zeros_output(self):
        s, cl = _make()
        s.system_state = SystemState.OFF
        cl.update()
        assert s.assist_command_request == 0.0
        assert s.regen_command_request == 0.0

    def test_slew_resets_on_mode_switch(self):
        """Switching out of REGEN and back starts slew fresh."""
        s, cl = _make()
        _ready_regen(s, cap_v=25.0)
        _ramp_regen(s, cl, 10)
        assert s.motor_command_a < -_SLEW_DELTA

        # Switch to OFF — slew state goes to 0
        s.system_state = SystemState.OFF
        cl.update()

        # Re-enter REGEN — should start from slew floor
        _ready_regen(s, cap_v=25.0)
        cl.update()
        assert abs(s.motor_command_a - (-_SLEW_DELTA)) < 0.01
