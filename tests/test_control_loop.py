# tests/test_control_loop.py — ControlLoop assist mapping and regen PI slip

import tests.conftest as _ct
from core import CommandMode, SharedState, SystemState
from services.control_loop import ControlLoop


def _make():
    state = SharedState()
    cl = ControlLoop(state)
    return state, cl


def _ready_regen(state, wheel_rpm=100.0, motor_rpm=400.0, cap_v=25.0, level=1.0):
    """Set shared state to a standard regen scenario."""
    state.system_state = SystemState.REGEN
    state.inhibit_motor_commands = False
    state.cap_voltage_v = cap_v
    state.wheel_speed_rpm = wheel_rpm
    state.wheel_speed_valid = True
    state.wheel_speed_fresh = True
    state.vesc_mech_rpm = motor_rpm
    state.requested_level = level
    state.requested_mode = CommandMode.REGEN


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

    def test_inhibit_resets_slew(self):
        s, cl = _make()
        s.system_state = SystemState.ASSIST
        s.inhibit_motor_commands = False
        s.requested_level = 1.0
        cl.update()  # ramp up a little
        s.inhibit_motor_commands = True
        cl.update()
        s.inhibit_motor_commands = False
        s.requested_level = 0.0
        cl.update()
        assert s.assist_command_request == 0.0


# ---- ASSIST ----

class TestAssist:
    def test_zero_request_zero_output(self):
        s, cl = _make()
        s.system_state = SystemState.ASSIST
        s.inhibit_motor_commands = False
        s.requested_level = 0.0
        cl.update()
        assert s.assist_command_request == 0.0

    def test_full_request_ramps_up(self):
        s, cl = _make()
        s.system_state = SystemState.ASSIST
        s.inhibit_motor_commands = False
        s.requested_level = 1.0
        # First cycle won't jump to 40A — slew limited
        cl.update()
        assert 0.0 < s.assist_command_request <= 40.0

    def test_full_request_reaches_max_after_many_cycles(self):
        s, cl = _make()
        s.system_state = SystemState.ASSIST
        s.inhibit_motor_commands = False
        s.requested_level = 1.0
        for _ in range(10000):
            cl.update()
        assert abs(s.assist_command_request - 40.0) < 0.01

    def test_regen_stays_zero_during_assist(self):
        s, cl = _make()
        s.system_state = SystemState.ASSIST
        s.inhibit_motor_commands = False
        s.requested_level = 0.5
        cl.update()
        assert s.regen_command_request == 0.0

    def test_assist_clamped_to_limit(self):
        s, cl = _make()
        s.system_state = SystemState.ASSIST
        s.inhibit_motor_commands = False
        s.requested_level = 2.0  # > 1.0
        for _ in range(10000):
            cl.update()
        assert s.assist_command_request <= 40.0


# ---- REGEN ----

class TestRegen:
    def test_regen_zero_when_above_soft_cutoff(self):
        s, cl = _make()
        _ready_regen(s, cap_v=41.0)  # above 40V soft cutoff
        cl.update()
        assert s.regen_command_request == 0.0

    def test_regen_zero_when_wheel_invalid(self):
        s, cl = _make()
        _ready_regen(s)
        s.wheel_speed_valid = False
        cl.update()
        assert s.regen_command_request == 0.0

    def test_regen_zero_when_wheel_below_min(self):
        s, cl = _make()
        _ready_regen(s, wheel_rpm=5.0)  # below 20 RPM minimum
        cl.update()
        assert s.regen_command_request == 0.0

    def test_regen_produces_nonzero_when_carrier_too_locked(self):
        s, cl = _make()
        # Carrier near full lock (slip below target) should command regen to
        # open the slip back toward the target.
        _ready_regen(s, wheel_rpm=100.0, motor_rpm=500.0, cap_v=25.0)
        for _ in range(50):
            cl.update()
        assert s.regen_command_request > 0.0

    def test_regen_zero_when_carrier_already_slipping_freely(self):
        s, cl = _make()
        # Carrier already has far more slip than target, so regen should back off.
        _ready_regen(s, wheel_rpm=100.0, motor_rpm=50.0, cap_v=25.0)
        for _ in range(50):
            cl.update()
        assert s.regen_command_request == 0.0

    def test_regen_handles_negative_motor_rpm_sign(self):
        s, cl = _make()
        # Telemetry sign can be negative depending on observer orientation.
        _ready_regen(s, wheel_rpm=100.0, motor_rpm=-500.0, cap_v=25.0)
        for _ in range(50):
            cl.update()
        assert s.regen_command_request > 0.0

    def test_regen_output_consistent_regardless_of_level(self):
        """With rider multiplier removed, level should not affect regen output."""
        s1, cl1 = _make()
        _ready_regen(s1, wheel_rpm=100.0, motor_rpm=500.0, cap_v=25.0, level=1.0)
        for _ in range(300):
            cl1.update()
        full = s1.regen_command_request

        s2, cl2 = _make()
        _ready_regen(s2, wheel_rpm=100.0, motor_rpm=500.0, cap_v=25.0, level=0.5)
        for _ in range(300):
            cl2.update()
        half = s2.regen_command_request

        # Both should converge to the same value since rider level is not used
        assert abs(full - half) < 0.01

    def test_regen_clamped_to_limit(self):
        s, cl = _make()
        _ready_regen(s, wheel_rpm=200.0, motor_rpm=10.0, cap_v=20.0, level=1.0)
        for _ in range(10000):
            cl.update()
        from config.settings import REGEN_COMMAND_MAX_A
        assert s.regen_command_request <= REGEN_COMMAND_MAX_A

    def test_carrier_speed_populated(self):
        s, cl = _make()
        _ready_regen(s, wheel_rpm=100.0, motor_rpm=240.0, cap_v=25.0)
        cl.update()
        # With motor_rpm=240 and locked_motor_rpm = 100*4 = 400,
        # lock_fraction = 240/400 = 0.6, carrier_rpm = 100*(1-0.6) = 40
        assert abs(s.gear_carrier_speed_rpm - 40.0) < 0.1

    def test_assist_stays_zero_during_regen(self):
        s, cl = _make()
        _ready_regen(s)
        cl.update()
        assert s.assist_command_request == 0.0


# ---- Neutral / other states ----

class TestNeutralStates:
    def test_ready_state_zeros_output(self):
        s, cl = _make()
        s.system_state = SystemState.OFF
        s.inhibit_motor_commands = False
        s.requested_level = 1.0
        cl.update()
        assert s.assist_command_request == 0.0
        assert s.regen_command_request == 0.0

    def test_off_state_zeros_output(self):
        s, cl = _make()
        s.system_state = SystemState.OFF
        cl.update()
        assert s.assist_command_request == 0.0
        assert s.regen_command_request == 0.0

    def test_integral_resets_on_mode_switch(self):
        s, cl = _make()
        # Build up integral in regen
        _ready_regen(s, wheel_rpm=100.0, motor_rpm=500.0, cap_v=25.0)
        for _ in range(200):
            _ct._tick_ms += 10
            cl.update()
        regen_val = s.regen_command_request
        assert regen_val > 0.0

        # Switch to OFF — should reset dynamics
        s.system_state = SystemState.OFF
        cl.update()

        # Re-enter REGEN — should start fresh (low value, not the previous accumulated)
        _ready_regen(s, wheel_rpm=100.0, motor_rpm=500.0, cap_v=25.0)
        _ct._tick_ms += 10
        cl.update()
        assert s.regen_command_request < regen_val


# ---- PI anti-windup recovery ----

class TestPIAntiWindup:
    def test_integral_recovers_after_saturation(self):
        """After integral saturates at positive limit, reversing error should bring it down."""
        s, cl = _make()
        # Near-full lock gives positive error with the corrected sign, so the
        # integral should wind up.
        _ready_regen(s, wheel_rpm=100.0, motor_rpm=500.0, cap_v=20.0, level=1.0)
        for _ in range(5000):
            _ct._tick_ms += 10
            cl.update()
        high_cmd = s.regen_command_request

        # Now move to a freer carrier with excess slip; command should fall.
        s.vesc_mech_rpm = 50.0
        for _ in range(5000):
            _ct._tick_ms += 10
            cl.update()
        low_cmd = s.regen_command_request

        assert low_cmd < high_cmd


class TestFreshEdgeSync:
    def test_regen_holds_when_not_fresh(self):
        """Regen command and carrier RPM should hold when no fresh edge arrives."""
        s, cl = _make()
        _ready_regen(s, wheel_rpm=100.0, motor_rpm=280.0, cap_v=25.0)
        cl.update()
        first_carrier = s.gear_carrier_speed_rpm
        first_regen = s.regen_command_request

        # Simulate stale wheel (motor drifts but no new wheel edge)
        s.wheel_speed_fresh = False
        s.vesc_mech_rpm = 200.0
        cl.update()
        # Nothing should change — PI did not run
        assert s.gear_carrier_speed_rpm == first_carrier
        assert s.regen_command_request == first_regen

    def test_carrier_rpm_updates_when_fresh(self):
        """Carrier RPM should update when a fresh wheel edge arrives."""
        s, cl = _make()
        _ready_regen(s, wheel_rpm=100.0, motor_rpm=280.0, cap_v=25.0)
        cl.update()
        first_carrier = s.gear_carrier_speed_rpm

        # Fresh reading with changed motor RPM
        s.wheel_speed_fresh = True
        s.vesc_mech_rpm = 250.0
        cl.update()
        assert s.gear_carrier_speed_rpm != first_carrier
