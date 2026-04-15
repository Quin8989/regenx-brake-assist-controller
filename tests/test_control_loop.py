# tests/test_control_loop.py — ControlLoop assist, regen, direct command

import math
import pytest
from config.settings import (
    FLUX_LINKAGE_WB,
    MOTOR_COMMAND_LIMIT_A,
    MOTOR_PHASE_RESISTANCE_OHM,
    REGEN_COPPER_LOSS_FRACTION,
    REGEN_CURRENT_MAX_A,
    VESC_MOTOR_POLE_PAIRS,
)
from core import CommandMode, SharedState, SystemState
from services.control_loop import ControlLoop


def _make():
    state = SharedState()
    cl = ControlLoop(state)
    return state, cl


def _ready_assist(state, level=1.0):
    state.system_state = SystemState.ASSIST
    state.inhibit_motor_commands = False
    state.requested_mode = CommandMode.ASSIST
    state.requested_level = level


def _ready_regen(state, cap_v=25.0, rpm=200.0):
    state.system_state = SystemState.REGEN
    state.inhibit_motor_commands = False
    state.cap_voltage_v = cap_v
    state.requested_mode = CommandMode.REGEN
    state.requested_level = 1.0
    state.vesc_mech_rpm = rpm


# ---- Inhibit ----

def test_inhibit_zeros_all():
    """Inhibit zeros all commands."""
    s, cl = _make()
    _ready_assist(s)
    cl.update()
    assert s.motor_command_a > 0.0

    s.inhibit_motor_commands = True
    cl.update()
    assert s.motor_command_a == 0.0
    assert s.assist_command_request == 0.0
    assert s.regen_command_request == 0.0

    # Re-enable — reaches target immediately (no slew)
    s.inhibit_motor_commands = False
    _ready_assist(s)
    cl.update()
    assert abs(s.motor_command_a - MOTOR_COMMAND_LIMIT_A) < 0.01


# ---- Assist ----

@pytest.mark.parametrize("level,expected", [
    pytest.param(0.0, 0.0, id="zero_request"),
    pytest.param(0.5, MOTOR_COMMAND_LIMIT_A / 2.0, id="half_request"),
    pytest.param(1.0, MOTOR_COMMAND_LIMIT_A, id="full_request"),
    pytest.param(2.0, MOTOR_COMMAND_LIMIT_A, id="clamped_above_one"),
])
def test_assist_level(level, expected):
    """Assist reaches level × max in one cycle."""
    s, cl = _make()
    _ready_assist(s, level=level)
    cl.update()
    assert abs(s.motor_command_a - expected) < 0.01
    assert s.regen_command_request == 0.0


def test_assist_immediate_on_throttle_release():
    """Releasing throttle drops to zero in one cycle."""
    s, cl = _make()
    _ready_assist(s)
    cl.update()
    assert s.motor_command_a > 0.0
    s.requested_level = 0.0
    cl.update()
    assert s.motor_command_a == 0.0


# ---- Regen ----

@pytest.mark.parametrize("rpm,cap_v,expect_zero", [
    pytest.param(800.0, 25.0, False, id="high_rpm_active"),
    pytest.param(0.0, 25.0, True, id="zero_rpm"),
    pytest.param(200.0, 41.0, True, id="above_soft_cutoff"),
])
def test_regen_command(rpm, cap_v, expect_zero):
    s, cl = _make()
    _ready_regen(s, cap_v=cap_v, rpm=rpm)
    cl.update()
    if expect_zero:
        assert s.regen_command_request == 0.0
    else:
        assert s.regen_command_request > 0.0
    assert s.assist_command_request == 0.0


def test_regen_clamped_at_ceiling():
    s, cl = _make()
    _ready_regen(s, cap_v=25.0, rpm=800.0)
    cl.update()
    assert s.regen_command_request == REGEN_CURRENT_MAX_A


def test_regen_proportional_to_rpm():
    """Doubling RPM doubles regen current in the linear region."""
    s1, cl1 = _make()
    _ready_regen(s1, cap_v=25.0, rpm=60.0)
    cl1.update()
    s2, cl2 = _make()
    _ready_regen(s2, cap_v=25.0, rpm=120.0)
    cl2.update()
    assert s1.regen_command_request > 0.0
    assert s2.regen_command_request < REGEN_CURRENT_MAX_A
    assert abs(s2.regen_command_request - 2.0 * s1.regen_command_request) < 0.2


def test_regen_matches_physics():
    """Spot-check against efficiency-optimal formula at 80 RPM."""
    s, cl = _make()
    _ready_regen(s, cap_v=25.0, rpm=80.0)
    cl.update()
    omega_e = 80.0 * VESC_MOTOR_POLE_PAIRS * 2.0 * math.pi / 60.0
    expected = REGEN_COPPER_LOSS_FRACTION * FLUX_LINKAGE_WB * omega_e / MOTOR_PHASE_RESISTANCE_OHM
    assert abs(s.regen_command_request - expected) < 0.1


def test_regen_reaches_target_immediately():
    """Regen at high RPM reaches negative ceiling in one cycle (no slew)."""
    s, cl = _make()
    _ready_regen(s, cap_v=25.0, rpm=800.0)
    cl.update()
    assert abs(s.motor_command_a - (-REGEN_CURRENT_MAX_A)) < 0.01


def test_assist_to_regen_immediate():
    """Switching ASSIST→REGEN changes sign in one cycle."""
    s, cl = _make()
    _ready_assist(s)
    cl.update()
    assert s.motor_command_a > 0.0
    _ready_regen(s, cap_v=25.0, rpm=800.0)
    cl.update()
    assert s.motor_command_a < 0.0
    assert abs(s.motor_command_a - (-REGEN_CURRENT_MAX_A)) < 0.01


# ---- Precharge ----

def test_precharge_zeros_output():
    s, cl = _make()
    s.system_state = SystemState.PRECHARGE
    cl.update()
    assert s.assist_command_request == 0.0
    assert s.regen_command_request == 0.0
