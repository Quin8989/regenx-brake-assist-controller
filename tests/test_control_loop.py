# tests/test_control_loop.py — ControlLoop assist mapping and regen PI slip

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

    def test_regen_produces_nonzero_with_slip(self):
        s, cl = _make()
        # Carrier is free (motor_rpm << locked_motor_rpm) → lots of slip
        _ready_regen(s, wheel_rpm=100.0, motor_rpm=50.0, cap_v=25.0)
        for _ in range(50):
            cl.update()
        assert s.regen_command_request > 0.0

    def test_regen_rider_authority_scales(self):
        s1, cl1 = _make()
        _ready_regen(s1, wheel_rpm=100.0, motor_rpm=50.0, cap_v=25.0, level=1.0)
        for _ in range(300):
            cl1.update()
        full = s1.regen_command_request

        s2, cl2 = _make()
        _ready_regen(s2, wheel_rpm=100.0, motor_rpm=50.0, cap_v=25.0, level=0.5)
        for _ in range(300):
            cl2.update()
        half = s2.regen_command_request

        # Half authority should produce less current (approximately half)
        assert half < full
        assert half > 0.0

    def test_regen_clamped_to_limit(self):
        s, cl = _make()
        _ready_regen(s, wheel_rpm=200.0, motor_rpm=10.0, cap_v=20.0, level=1.0)
        for _ in range(10000):
            cl.update()
        assert s.regen_command_request <= 40.0

    def test_carrier_speed_populated(self):
        s, cl = _make()
        _ready_regen(s, wheel_rpm=100.0, motor_rpm=400.0, cap_v=25.0)
        cl.update()
        # With motor_rpm=400 and locked_motor_rpm = 100*5 = 500,
        # lock_fraction = 400/500 = 0.8, carrier_rpm = 100*(1-0.8) = 20
        assert abs(s.gear_carrier_speed_rpm - 20.0) < 0.1

    def test_assist_stays_zero_during_regen(self):
        s, cl = _make()
        _ready_regen(s)
        cl.update()
        assert s.assist_command_request == 0.0


# ---- Neutral / other states ----

class TestNeutralStates:
    def test_ready_state_zeros_output(self):
        s, cl = _make()
        s.system_state = SystemState.COAST
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
        _ready_regen(s, wheel_rpm=100.0, motor_rpm=50.0, cap_v=25.0)
        for _ in range(200):
            cl.update()
        regen_val = s.regen_command_request
        assert regen_val > 0.0

        # Switch to COAST — should reset dynamics
        s.system_state = SystemState.COAST
        cl.update()

        # Re-enter REGEN — should start fresh (low value, not the previous accumulated)
        _ready_regen(s, wheel_rpm=100.0, motor_rpm=50.0, cap_v=25.0)
        cl.update()
        assert s.regen_command_request < regen_val


# ---- PI anti-windup recovery ----

class TestPIAntiWindup:
    def test_integral_recovers_after_saturation(self):
        """After integral saturates at positive limit, reversing error should bring it down."""
        s, cl = _make()
        # Lots of positive error → integral winds up
        _ready_regen(s, wheel_rpm=100.0, motor_rpm=10.0, cap_v=20.0, level=1.0)
        for _ in range(5000):
            cl.update()
        high_cmd = s.regen_command_request

        # Now reduce error (motor closer to locked speed → less slip)
        s.vesc_mech_rpm = 95.0 * 5.0  # motor near locked → low carrier rpm
        for _ in range(5000):
            cl.update()
        low_cmd = s.regen_command_request

        assert low_cmd < high_cmd
