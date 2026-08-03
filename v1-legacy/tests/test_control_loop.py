# tests/test_control_loop.py — ControlLoop assist, regen, direct command

import math
import pytest
from config.settings import (
    MOTOR_COMMAND_LIMIT_A,
    REGEN_CURRENT_MAX_A,
    VCAP_REGEN_TAPER_END_V,
    VCAP_REGEN_TAPER_START_V,
    VESC_WATT_MAX,
)
from core import CommandMode, SharedState, SystemState
from services.control_loop import ControlLoop
from sim.strategies import AimdFfRegenStrategy
from tests.conftest import set_clock_ms


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


class _CaptureStrategy:
    def __init__(self, return_current=0.0):
        self.return_current = return_current
        self.last_ctx = None

    def update(self, ctx):
        self.last_ctx = ctx
        return self.return_current


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
    pytest.param(200.0, 42.0, True, id="above_taper_end"),
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
    """Regen never exceeds REGEN_CURRENT_MAX_A regardless of strategy output."""
    s, cl = _make()
    _ready_regen(s, cap_v=25.0, rpm=1100.0)
    # Run enough cycles for the strategy to ramp up
    for _ in range(200):
        cl.update()
    assert 0.0 < s.regen_command_request <= REGEN_CURRENT_MAX_A


def test_regen_higher_rpm_more_current():
    """Higher RPM produces more regen current (monotonic in linear region)."""
    s1, cl1 = _make()
    _ready_regen(s1, cap_v=25.0, rpm=60.0)
    cl1.update()
    s2, cl2 = _make()
    _ready_regen(s2, cap_v=25.0, rpm=120.0)
    cl2.update()
    assert s1.regen_command_request > 0.0
    assert s2.regen_command_request > s1.regen_command_request


def test_regen_nonzero_at_moderate_rpm():
    """Strategy produces a positive regen command at 200 RPM / 25 V."""
    s, cl = _make()
    _ready_regen(s, cap_v=25.0, rpm=200.0)
    cl.update()
    assert s.regen_command_request > 0.0


def test_regen_context_uses_fresh_lispbm_signals_when_available():
    s, cl = _make()
    capture = _CaptureStrategy()
    cl._strategy = capture
    _ready_regen(s, cap_v=25.0, rpm=200.0)
    s.vesc_iq_current_a = -3.0
    s.vesc_mech_rpm_fast = 260.0
    s.vesc_iq_mean_a = -5.5
    s.vesc_drpm_mean_mech = -111.0
    s.vesc_drpm_peak_neg_mech = -2222.0
    set_clock_ms(1000)
    s.last_push_iq_rx_ms = 990

    cl.update()

    assert capture.last_ctx is not None
    assert capture.last_ctx.rpm_fast == 260.0
    assert capture.last_ctx.iq_mean == -5.5
    assert capture.last_ctx.drpm_mean == -111.0
    assert capture.last_ctx.drpm_peak_neg == -2222.0
    assert capture.last_ctx.preferred_rpm == 260.0
    assert capture.last_ctx.preferred_iq == -5.5


def test_regen_context_falls_back_when_lispbm_signals_are_stale():
    s, cl = _make()
    capture = _CaptureStrategy()
    cl._strategy = capture
    _ready_regen(s, cap_v=25.0, rpm=200.0)
    s.vesc_iq_current_a = -3.0
    s.vesc_mech_rpm_fast = 260.0
    s.vesc_iq_mean_a = -5.5
    s.vesc_drpm_mean_mech = -111.0
    s.vesc_drpm_peak_neg_mech = -2222.0
    set_clock_ms(1000)
    s.last_push_iq_rx_ms = 900

    cl.update()

    assert capture.last_ctx is not None
    assert capture.last_ctx.rpm_fast is None
    assert capture.last_ctx.iq_mean is None
    assert capture.last_ctx.drpm_mean == 0.0
    assert capture.last_ctx.drpm_peak_neg == 0.0
    assert capture.last_ctx.preferred_rpm == s.vesc_mech_rpm
    assert capture.last_ctx.preferred_iq == s.vesc_iq_current_a


# ---- Power limiter ----

def test_regen_power_limit_scales_down():
    """Commanded power above VESC_WATT_MAX is scaled down."""
    s, cl = _make()
    _ready_regen(s, cap_v=40.0, rpm=800.0)
    # Fake strategy commanding the full current ceiling at 40 V cap.
    # Commanded power = 40 * 40 = 1600 W > 1500 W ⇒ scale by 1500/1600.
    big = _CaptureStrategy(return_current=REGEN_CURRENT_MAX_A)
    cl._strategy = big
    cl.update()
    expected = REGEN_CURRENT_MAX_A * (VESC_WATT_MAX / (REGEN_CURRENT_MAX_A * 40.0))
    assert abs(s.regen_command_request - expected) < 0.01


def test_regen_power_below_limit_no_effect():
    """Commanded power below the limit passes through unchanged."""
    s, cl = _make()
    _ready_regen(s, cap_v=25.0, rpm=200.0)
    small = _CaptureStrategy(return_current=5.0)  # 5 A × 25 V = 125 W ≪ 1500 W
    cl._strategy = small
    cl.update()
    assert abs(s.regen_command_request - 5.0) < 0.01


def test_regen_power_limit_clamps_aimd_ff_at_high_rpm():
    """End-to-end: with the production AIMD-FF strategy at high RPM and high
    cap voltage, the commanded-power limiter keeps the request at or below
    VESC_WATT_MAX / cap_voltage_v.  This locks in M1 against accidental
    regressions in either the strategy or the limiter.
    """
    s, cl = _make()
    cl._strategy = AimdFfRegenStrategy()
    _ready_regen(s, cap_v=40.0, rpm=1500.0)
    # High iq tracking so AIMD-FF probes toward its ceiling
    s.vesc_iq_current_a = -REGEN_CURRENT_MAX_A
    for _ in range(50):
        cl.update()
    cap_v = s.cap_voltage_v
    max_allowed = VESC_WATT_MAX / cap_v
    assert s.regen_command_request <= max_allowed + 0.05
    assert s.regen_command_request <= REGEN_CURRENT_MAX_A + 0.05


# ---- Duty saturation ----

def test_regen_duty_saturation_scales_down():
    """Duty > 0.95 reduces regen command."""
    s, cl = _make()
    _ready_regen(s, cap_v=25.0, rpm=600.0)
    s.vesc_duty_cycle = 0.98  # above 0.95 threshold
    s.vesc_iq_current_a = 0.0
    cl.update()
    s2, cl2 = _make()
    _ready_regen(s2, cap_v=25.0, rpm=600.0)
    s2.vesc_duty_cycle = 0.5  # normal
    s2.vesc_iq_current_a = 0.0
    cl2.update()
    assert s.regen_command_request < s2.regen_command_request


def test_regen_duty_at_one_zeros_command():
    """Duty = 1.0 means zero headroom → command scaled to zero."""
    s, cl = _make()
    _ready_regen(s, cap_v=25.0, rpm=600.0)
    s.vesc_duty_cycle = 1.0
    s.vesc_iq_current_a = 0.0
    cl.update()
    assert s.regen_command_request == 0.0


def test_regen_duty_below_threshold_no_effect():
    """Duty below 0.95 doesn't reduce command."""
    s, cl = _make()
    cl._strategy = _CaptureStrategy(return_current=10.0)
    _ready_regen(s, cap_v=25.0, rpm=400.0)
    s.vesc_duty_cycle = 0.90
    s.vesc_iq_current_a = 0.0
    cl.update()
    s2, cl2 = _make()
    cl2._strategy = _CaptureStrategy(return_current=10.0)
    _ready_regen(s2, cap_v=25.0, rpm=400.0)
    s2.vesc_duty_cycle = 0.0
    s2.vesc_iq_current_a = 0.0
    cl2.update()
    assert abs(s.regen_command_request - s2.regen_command_request) < 0.01


def test_regen_produces_negative_motor_command():
    """Regen at high RPM produces a negative motor command."""
    s, cl = _make()
    _ready_regen(s, cap_v=25.0, rpm=1100.0)
    cl.update()
    assert s.motor_command_a < 0.0


def test_assist_to_regen_immediate():
    """Switching ASSIST→REGEN changes sign in one cycle."""
    s, cl = _make()
    _ready_assist(s)
    cl.update()
    assert s.motor_command_a > 0.0
    _ready_regen(s, cap_v=25.0, rpm=1100.0)
    cl.update()
    assert s.motor_command_a < 0.0


# ---- Precharge ----

def test_precharge_zeros_output():
    s, cl = _make()
    s.system_state = SystemState.PRECHARGE
    cl.update()
    assert s.assist_command_request == 0.0
    assert s.regen_command_request == 0.0


# ---- Voltage taper ----

def test_regen_below_taper_start_full_current():
    """Below TAPER_START (38V), regen is not reduced by voltage taper."""
    s, cl = _make()
    _ready_regen(s, cap_v=25.0, rpm=600.0)
    cl.update()
    cmd_low = s.regen_command_request

    s2, cl2 = _make()
    _ready_regen(s2, cap_v=VCAP_REGEN_TAPER_START_V - 1.0, rpm=600.0)
    cl2.update()
    assert abs(s2.regen_command_request - cmd_low) < 0.01


def test_regen_above_taper_end_zero():
    """At or above TAPER_END (41V), regen is zero."""
    s, cl = _make()
    _ready_regen(s, cap_v=VCAP_REGEN_TAPER_END_V, rpm=600.0)
    cl.update()
    assert s.regen_command_request == 0.0


def test_regen_midpoint_taper():
    """At taper midpoint (39.5V), regen is about 50% of full."""
    s_full, cl_full = _make()
    _ready_regen(s_full, cap_v=25.0, rpm=600.0)
    cl_full.update()
    full_cmd = s_full.regen_command_request

    mid_v = (VCAP_REGEN_TAPER_START_V + VCAP_REGEN_TAPER_END_V) / 2.0
    s_mid, cl_mid = _make()
    _ready_regen(s_mid, cap_v=mid_v, rpm=600.0)
    cl_mid.update()
    assert s_mid.regen_command_request > 0.0
    assert abs(s_mid.regen_command_request - full_cmd * 0.5) < 0.5


def test_regen_taper_is_gradual():
    """Regen decreases monotonically across the taper range."""
    commands = []
    for v in [39.0, 39.5, 40.0, 40.5, 41.0, 41.5, 42.0]:
        s, cl = _make()
        _ready_regen(s, cap_v=v, rpm=600.0)
        cl.update()
        commands.append(s.regen_command_request)
    # Monotonically non-increasing
    for i in range(len(commands) - 1):
        assert commands[i] >= commands[i + 1]
    # First should be positive, last should be zero
    assert commands[0] > 0.0
    assert commands[-1] == 0.0


def test_regen_negative_strategy_output_is_clamped():
    s, cl = _make()
    cl._strategy = _CaptureStrategy(return_current=-4.0)
    _ready_regen(s, cap_v=25.0, rpm=600.0)

    cl.update()

    assert s.regen_command_request == 0.0


def test_regen_level_zero_forces_zero_command():
    s, cl = _make()
    cl._strategy = _CaptureStrategy(return_current=20.0)
    _ready_regen(s, cap_v=25.0, rpm=600.0)
    s.requested_level = 0.0

    cl.update()

    assert s.regen_command_request == 0.0
    assert s.motor_command_a == 0.0


def test_aimd_ff_regen_strategy_can_be_selected_and_is_bounded():
    """The aimd_ff strategy can be injected and its command stays within limits."""
    s, cl = _make()
    cl._strategy = AimdFfRegenStrategy()
    _ready_regen(s, cap_v=25.0, rpm=700.0)
    s.vesc_iq_current_a = -6.0
    s.vesc_input_current_a = -8.0
    s.vesc_duty_cycle = 0.40

    for _ in range(10):
        cl.update()

    assert 0.0 <= s.regen_command_request <= REGEN_CURRENT_MAX_A
    assert s.motor_command_a <= 0.0
