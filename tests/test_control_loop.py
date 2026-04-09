# tests/test_control_loop.py — ControlLoop assist mapping and regen backoff

import tests.conftest as _ct
from core import CommandMode, SharedState, SystemState
from services.control_loop import ControlLoop


def _make():
    state = SharedState()
    cl = ControlLoop(state)
    return state, cl


def _ready_regen(state, motor_rpm=400.0, actual_a=0.0, cap_v=25.0):
    """Set shared state to a standard regen scenario."""
    state.system_state = SystemState.REGEN
    state.inhibit_motor_commands = False
    state.cap_voltage_v = cap_v
    state.vesc_mech_rpm = motor_rpm
    state.vesc_motor_current_a = actual_a
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

    def test_inhibit_resets_regen_target(self):
        s, cl = _make()
        _ready_regen(s, motor_rpm=400.0, actual_a=20.0, cap_v=25.0)
        cl.update()  # regen target set
        s.inhibit_motor_commands = True
        cl.update()
        s.inhibit_motor_commands = False
        s.system_state = SystemState.OFF
        cl.update()
        assert s.regen_command_request == 0.0


# ---- ASSIST ----

class TestAssist:
    def test_zero_request_zero_output(self):
        s, cl = _make()
        s.system_state = SystemState.ASSIST
        s.inhibit_motor_commands = False
        s.requested_level = 0.0
        cl.update()
        assert s.assist_command_request == 0.0

    def test_full_request_gives_max(self):
        s, cl = _make()
        s.system_state = SystemState.ASSIST
        s.inhibit_motor_commands = False
        s.requested_level = 1.0
        cl.update()
        assert s.assist_command_request == 40.0

    def test_half_request_gives_half_max(self):
        s, cl = _make()
        s.system_state = SystemState.ASSIST
        s.inhibit_motor_commands = False
        s.requested_level = 0.5
        cl.update()
        assert abs(s.assist_command_request - 20.0) < 0.01

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
        cl.update()
        assert s.assist_command_request == 40.0


# ---- REGEN ----

class TestRegen:
    def test_regen_zero_when_above_soft_cutoff(self):
        s, cl = _make()
        _ready_regen(s, cap_v=41.0)
        cl.update()
        assert s.regen_command_request == 0.0

    def test_regen_starts_at_max(self):
        """First regen cycle should immediately command REGEN_COMMAND_MAX_A."""
        from config.settings import REGEN_COMMAND_MAX_A
        s, cl = _make()
        _ready_regen(s, motor_rpm=400.0, actual_a=20.0, cap_v=25.0)
        cl.update()
        assert s.regen_command_request == REGEN_COMMAND_MAX_A

    def test_regen_holds_max_when_actual_tracks(self):
        """With actual tracking commanded (no backoff), stays at max."""
        from config.settings import REGEN_COMMAND_MAX_A
        s, cl = _make()
        _ready_regen(s, motor_rpm=400.0, cap_v=25.0)
        for _ in range(100):
            s.vesc_motor_current_a = s.regen_command_request
            cl.update()
        assert s.regen_command_request == REGEN_COMMAND_MAX_A

    def test_regen_backs_off_when_actual_much_lower(self):
        """When actual << commanded the target should decay."""
        from config.settings import REGEN_COMMAND_MAX_A
        s, cl = _make()
        _ready_regen(s, motor_rpm=400.0, actual_a=0.0, cap_v=25.0)
        cl.update()
        assert s.regen_command_request == REGEN_COMMAND_MAX_A

        # Actual stays near zero — carrier slipping → backoff kicks in
        s.vesc_motor_current_a = 1.0
        for _ in range(200):
            cl.update()
        assert s.regen_command_request < REGEN_COMMAND_MAX_A

    def test_regen_output_consistent_regardless_of_level(self):
        """Rider level does not affect regen output."""
        from config.settings import REGEN_COMMAND_MAX_A
        s1, cl1 = _make()
        _ready_regen(s1, motor_rpm=400.0, cap_v=25.0)
        s1.requested_level = 1.0
        s1.vesc_motor_current_a = REGEN_COMMAND_MAX_A
        cl1.update()

        s2, cl2 = _make()
        _ready_regen(s2, motor_rpm=400.0, cap_v=25.0)
        s2.requested_level = 0.5
        s2.vesc_motor_current_a = REGEN_COMMAND_MAX_A
        cl2.update()

        assert s1.regen_command_request == s2.regen_command_request

    def test_regen_clamped_to_limit(self):
        from config.settings import REGEN_COMMAND_MAX_A
        s, cl = _make()
        _ready_regen(s, motor_rpm=500.0, cap_v=20.0)
        s.vesc_motor_current_a = REGEN_COMMAND_MAX_A
        cl.update()
        assert s.regen_command_request == REGEN_COMMAND_MAX_A

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

    def test_dynamics_reset_on_mode_switch(self):
        from config.settings import REGEN_COMMAND_MAX_A
        s, cl = _make()
        # Build up regen command, then trigger backoff
        _ready_regen(s, motor_rpm=400.0, cap_v=25.0)
        cl.update()  # target = max
        s.vesc_motor_current_a = 1.0  # actual << commanded → backoff
        for _ in range(200):
            cl.update()
        decayed_cmd = s.regen_command_request
        assert decayed_cmd < REGEN_COMMAND_MAX_A

        # Switch to OFF — should reset regen target
        s.system_state = SystemState.OFF
        cl.update()

        # Re-enter REGEN — should start fresh at max
        _ready_regen(s, motor_rpm=400.0, cap_v=25.0)
        cl.update()
        assert s.regen_command_request == REGEN_COMMAND_MAX_A


# ---- Backoff recovery ----

class TestBackoffRecovery:
    def test_backoff_recovers_when_actual_tracks_again(self):
        """After backing off, if actual starts tracking commanded again, command holds."""
        from config.settings import REGEN_COMMAND_MAX_A
        s, cl = _make()
        _ready_regen(s, motor_rpm=400.0, cap_v=20.0)
        cl.update()
        assert s.regen_command_request == REGEN_COMMAND_MAX_A

        # Actual drops — trigger backoff
        s.vesc_motor_current_a = 1.0
        for _ in range(500):
            cl.update()
        mid_cmd = s.regen_command_request
        assert mid_cmd < REGEN_COMMAND_MAX_A

        # Actual tracks again — command should at least hold (no further decay)
        for _ in range(500):
            s.vesc_motor_current_a = s.regen_command_request
            cl.update()
        final_cmd = s.regen_command_request
        assert final_cmd >= mid_cmd - 0.01
