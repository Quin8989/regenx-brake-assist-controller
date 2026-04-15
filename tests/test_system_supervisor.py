# tests/test_system_supervisor.py — SystemSupervisor safety, transitions, inhibits

import pytest
from tests.conftest import set_clock_ms

from core import CommandMode, FaultCode, FaultManager, SharedState, SystemState
from services.system_supervisor import SystemSupervisor
from config.settings import VCAP_ABSOLUTE_MAX, VCAP_MIN_OPERATING, VESC_TELEMETRY_TIMEOUT_MS


def _make(**overrides):
    state = SharedState()
    state.throttle_valid = True
    for k, v in overrides.items():
        setattr(state, k, v)
    faults = FaultManager(state)
    sv = SystemSupervisor(state, faults)
    return state, faults, sv


# ---- Safety checks (fault detection) ----

@pytest.mark.parametrize("setup,fault_code,expected", [
    pytest.param({"cap_voltage_v": VCAP_ABSOLUTE_MAX}, FaultCode.OVERVOLTAGE, True, id="overvoltage_at_max"),
    pytest.param({"cap_voltage_v": VCAP_ABSOLUTE_MAX - 0.1}, FaultCode.OVERVOLTAGE, False, id="overvoltage_below"),
    pytest.param({"vesc_fault_code": 3}, FaultCode.VESC_FAULT, True, id="vesc_fault_nonzero"),
    pytest.param({"vesc_fault_code": 0}, FaultCode.VESC_FAULT, False, id="vesc_fault_zero"),
    pytest.param({"throttle_valid": False}, FaultCode.THROTTLE_RANGE, True, id="throttle_invalid"),
    pytest.param({"throttle_valid": True}, FaultCode.THROTTLE_RANGE, False, id="throttle_valid"),
])
def test_fault_detection(setup, fault_code, expected):
    s, f, sv = _make(**setup)
    sv.update()
    assert (fault_code in s.fault_flags) == expected


def test_overvoltage_latches():
    s, f, sv = _make(cap_voltage_v=VCAP_ABSOLUTE_MAX)
    sv.update()
    s.cap_voltage_v = 30.0
    sv.update()
    assert FaultCode.OVERVOLTAGE in s.fault_flags


def test_vesc_fault_auto_clears():
    s, f, sv = _make(vesc_fault_code=5)
    sv.update()
    assert FaultCode.VESC_FAULT in s.fault_flags
    s.vesc_fault_code = 0
    sv.update()
    assert FaultCode.VESC_FAULT not in s.fault_flags


def test_telemetry_timeout_and_clear():
    """Stale telemetry faults; fresh packet clears it."""
    s, f, sv = _make(system_state=SystemState.REGEN, cap_voltage_v=20.0)
    set_clock_ms(100)
    s.last_vesc_rx_ms = 100
    set_clock_ms(100 + VESC_TELEMETRY_TIMEOUT_MS + 100)
    sv.update()
    assert FaultCode.VESC_TIMEOUT in s.fault_flags
    # Fresh packet clears
    s.last_vesc_rx_ms = 100 + VESC_TELEMETRY_TIMEOUT_MS + 110
    set_clock_ms(100 + VESC_TELEMETRY_TIMEOUT_MS + 110)
    sv.update()
    assert FaultCode.VESC_TIMEOUT not in s.fault_flags


def test_telemetry_exempt_in_precharge():
    s, f, sv = _make(system_state=SystemState.PRECHARGE)
    s.last_vesc_rx_ms = 1
    set_clock_ms(10000)
    sv.update()
    assert FaultCode.VESC_TIMEOUT not in s.fault_flags


def test_telemetry_no_fault_before_first_rx():
    s, f, sv = _make(system_state=SystemState.REGEN, cap_voltage_v=20.0)
    set_clock_ms(10000)
    sv.update()
    assert FaultCode.VESC_TIMEOUT not in s.fault_flags


# ---- State transitions ----

@pytest.mark.parametrize("start,cap_v,mode,faults,expected", [
    pytest.param(SystemState.PRECHARGE, 8.0, CommandMode.REGEN, [], SystemState.PRECHARGE, id="precharge_below_threshold"),
    pytest.param(SystemState.PRECHARGE, VCAP_MIN_OPERATING, CommandMode.REGEN, [], SystemState.REGEN, id="precharge_at_threshold"),
    pytest.param(SystemState.PRECHARGE, 16.0, CommandMode.REGEN, [], SystemState.REGEN, id="precharge_above_threshold"),
    pytest.param(SystemState.REGEN, 20.0, CommandMode.ASSIST, [], SystemState.ASSIST, id="regen_to_assist"),
    pytest.param(SystemState.REGEN, 20.0, CommandMode.REGEN, [], SystemState.REGEN, id="regen_stays"),
    pytest.param(SystemState.ASSIST, 20.0, CommandMode.REGEN, [], SystemState.REGEN, id="assist_to_regen"),
    pytest.param(SystemState.ASSIST, 20.0, CommandMode.ASSIST, [], SystemState.ASSIST, id="assist_stays"),
    pytest.param(SystemState.REGEN, 20.0, CommandMode.REGEN, [FaultCode.INTERNAL], SystemState.FAULT, id="regen_to_fault"),
    pytest.param(SystemState.ASSIST, 20.0, CommandMode.ASSIST, [FaultCode.INTERNAL], SystemState.FAULT, id="assist_to_fault"),
    pytest.param(SystemState.PRECHARGE, 20.0, CommandMode.REGEN, [FaultCode.OVERVOLTAGE], SystemState.FAULT, id="precharge_to_fault"),
    pytest.param(SystemState.FAULT, 20.0, CommandMode.REGEN, [], SystemState.REGEN, id="fault_clears_to_regen"),
    pytest.param(SystemState.FAULT, 20.0, CommandMode.REGEN, [FaultCode.OVERVOLTAGE], SystemState.FAULT, id="fault_stays_active"),
])
def test_transition(start, cap_v, mode, faults, expected):
    s, f, sv = _make(system_state=start, cap_voltage_v=cap_v, requested_mode=mode)
    for fc in faults:
        f.set_fault(fc)
    sv.update()
    assert s.system_state == expected


# ---- Inhibit policy ----

@pytest.mark.parametrize("state,cap_v,mode,fault,expected_inhibit", [
    pytest.param(SystemState.PRECHARGE, 5.0, CommandMode.REGEN, None, True, id="precharge_inhibits"),
    pytest.param(SystemState.REGEN, 20.0, CommandMode.REGEN, None, False, id="regen_normal"),
    pytest.param(SystemState.ASSIST, 20.0, CommandMode.ASSIST, None, False, id="assist_normal"),
    pytest.param(SystemState.REGEN, 20.0, CommandMode.REGEN, FaultCode.INTERNAL, True, id="fault_inhibits"),
    pytest.param(SystemState.ASSIST, 5.0, CommandMode.ASSIST, None, True, id="low_v_blocks_assist"),
    pytest.param(SystemState.REGEN, 5.0, CommandMode.REGEN, None, False, id="low_v_allows_regen"),
])
def test_inhibit(state, cap_v, mode, fault, expected_inhibit):
    s, f, sv = _make(system_state=state, cap_voltage_v=cap_v, requested_mode=mode)
    if fault:
        f.set_fault(fault)
    sv.update()
    assert s.inhibit_motor_commands == expected_inhibit
