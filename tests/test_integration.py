# tests/test_integration.py — cross-module integration: fault-to-zero, soft reset

import pytest
from tests.conftest import set_clock_ms
from core import CommandMode, FaultCode, FaultManager, SharedState, SystemState
from services.system_supervisor import SystemSupervisor
from services.control_loop import ControlLoop
from services.vesc_comm import VESCComm
from app.controller import AppController
from machine import UART


# ---- Fault-to-zero pipeline (TC-12/TC-13) ----

def _make_chain():
    state = SharedState()
    state.throttle_valid = True
    faults = FaultManager(state)
    safety = SystemSupervisor(state, faults)
    cl = ControlLoop(state)
    uart = UART()
    vesc = VESCComm(state, uart)
    return state, faults, safety, cl, uart, vesc


def _run_chain(safety, cl, vesc, state):
    safety.update()
    cl.update()
    vesc.send_current(state.motor_command_a)


@pytest.mark.parametrize("fault_type,inject", [
    pytest.param("overvoltage", {"cap_voltage_v": 43.0}, id="overvoltage"),
    pytest.param("vesc_timeout", {"_advance_time": True}, id="vesc_timeout"),
    pytest.param("vesc_fault_code", {"vesc_fault_code": 3}, id="vesc_fault"),
    pytest.param("throttle", {"throttle_valid": False}, id="throttle"),
])
def test_fault_inhibits_in_one_cycle(fault_type, inject):
    """Any critical fault → zero command in a single update cycle."""
    state, faults, safety, cl, uart, vesc = _make_chain()
    state.system_state = SystemState.ASSIST
    state.cap_voltage_v = 25.0
    state.inhibit_motor_commands = False
    state.requested_mode = CommandMode.ASSIST
    state.requested_level = 1.0
    state.last_vesc_rx_ms = 100
    set_clock_ms(100)

    _run_chain(safety, cl, vesc, state)
    assert state.system_state == SystemState.ASSIST

    if inject.get("_advance_time"):
        set_clock_ms(100 + 600)
    else:
        for attr, val in inject.items():
            setattr(state, attr, val)

    uart._tx_buf.clear()
    _run_chain(safety, cl, vesc, state)
    assert state.system_state == SystemState.FAULT
    assert state.inhibit_motor_commands is True
    assert state.motor_command_a == 0.0
    assert len(uart._tx_buf) > 0


def test_fault_clears_regen_command():
    """Active regen → fault → regen request zeroed in same cycle."""
    state, faults, safety, cl, uart, vesc = _make_chain()
    state.system_state = SystemState.REGEN
    state.cap_voltage_v = 25.0
    state.throttle_valid = True
    state.inhibit_motor_commands = False
    state.vesc_mech_rpm = 500.0
    state.requested_mode = CommandMode.REGEN
    state.requested_level = 1.0
    state.last_vesc_rx_ms = 1000
    set_clock_ms(1000)

    for _ in range(50):
        state.vesc_motor_current_a = state.regen_command_request
        cl.update()
    assert state.regen_command_request > 0.0

    state.cap_voltage_v = 43.0
    _run_chain(safety, cl, vesc, state)
    assert state.regen_command_request == 0.0


# ---- Soft reset (absorbed from test_soft_reset) ----

class _FakeButton:
    def __init__(self):
        self._pressed = False

    def poll(self):
        result = self._pressed
        self._pressed = False
        return result

    def press(self):
        self._pressed = True


class _Noop:
    def update(self): pass
    def service_rx(self): pass
    def request_telemetry(self): pass
    def debug(self, *args): pass


def _make_app():
    state = SharedState()
    fm = FaultManager(state)
    cl = ControlLoop(state)
    btn = _FakeButton()
    noop = _Noop()
    app = AppController(
        state=state,
        input_mgr=noop,
        vesc_comm=noop,
        safety=noop,
        control_loop=cl,
        display_mgr=noop,
        reset_button=btn,
        fault_manager=fm,
    )
    return state, fm, cl, btn, app


def test_soft_reset_clears_everything():
    s, fm, cl, btn, app = _make_app()
    s.system_state = SystemState.FAULT
    fm.set_fault(FaultCode.OVERVOLTAGE)
    s.inhibit_motor_commands = True
    s.assist_command_request = 10.0
    s.regen_command_request = 5.0
    s.requested_level = 0.8

    btn.press()
    app.update()

    assert fm.has_fault() is False
    assert s.system_state == SystemState.PRECHARGE
    assert s.inhibit_motor_commands is True
    assert s.assist_command_request == 0.0
    assert s.regen_command_request == 0.0
    assert s.requested_level == 0.0


def test_no_reset_without_press():
    s, fm, cl, btn, app = _make_app()
    s.system_state = SystemState.FAULT
    fm.set_fault(FaultCode.OVERVOLTAGE)
    app.update()
    assert fm.has_fault() is True


def test_reset_from_any_state():
    s, fm, cl, btn, app = _make_app()
    s.system_state = SystemState.ASSIST
    s.inhibit_motor_commands = False
    btn.press()
    app.update()
    assert s.system_state == SystemState.PRECHARGE
    assert s.inhibit_motor_commands is True
