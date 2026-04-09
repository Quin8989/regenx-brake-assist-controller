# tests/test_fault_to_zero.py — TC-12: Fault → zero command timing < 100 ms
#
# Verifies that from the moment a critical fault is detected through safety
# supervisor → state machine → command manager, the VESC receives a zero/neutral
# command within a single update cycle (well under 100 ms at 100 Hz).

import pytest

from tests.conftest import set_clock_ms
from core import CommandMode, FaultCode, FaultManager, SharedState, SystemState
from services.safety_supervisor import SafetySupervisor
from services.control_loop import ControlLoop
from services.vesc_comm import CommandManager, VESCComm
from app.state_machine import StateMachine
from machine import UART


def _make_system():
    """Wire up the full chain: safety → state machine → control loop → command manager."""
    state = SharedState()
    faults = FaultManager(state)
    safety = SafetySupervisor(state, faults)
    sm = StateMachine(state, faults)
    cl = ControlLoop(state)
    uart = UART()
    vesc = VESCComm(uart, state)
    cmd = CommandManager(vesc, state)
    return state, faults, safety, sm, cl, uart, cmd


def _run_chain(safety, sm, cl, cmd):
    """Execute one full cycle in the correct update order."""
    safety.update()
    sm.update()
    cl.update()
    cmd.update()


class TestFaultToZero:
    @pytest.mark.parametrize("fault_type,inject", [
        ("overvoltage", {"cap_voltage_v": 43.0}),
        ("vesc_timeout", {"_advance_time": True}),
        ("vesc_fault_code", {"vesc_fault_code": 3}),
    ])
    def test_fault_inhibits_in_one_cycle(self, fault_type, inject):
        """TC-12 / TC-13: Any critical fault → zero command in a single cycle."""
        state, faults, safety, sm, cl, uart, cmd = _make_system()

        state.system_state = SystemState.ASSIST
        state.cap_voltage_v = 25.0
        state.throttle_valid = True
        state.inhibit_motor_commands = False
        state.requested_mode = CommandMode.ASSIST
        state.requested_level = 1.0
        state.last_vesc_rx_ms = 100
        set_clock_ms(100)

        _run_chain(safety, sm, cl, cmd)
        assert state.system_state == SystemState.ASSIST

        # Inject the fault condition
        if inject.get("_advance_time"):
            set_clock_ms(100 + 600)  # past 500ms telemetry timeout
        else:
            for attr, val in inject.items():
                setattr(state, attr, val)

        uart._tx_buf.clear()
        _run_chain(safety, sm, cl, cmd)
        assert state.system_state == SystemState.FAULT
        assert state.inhibit_motor_commands is True
        assert state.assist_command_request == 0.0
        assert state.regen_command_request == 0.0
        assert len(uart._tx_buf) > 0

    def test_throttle_fault_during_assist(self):
        """Throttle out-of-range during ASSIST → inhibit in one cycle (TC-11 related)."""
        state, faults, safety, sm, cl, uart, cmd = _make_system()

        state.system_state = SystemState.ASSIST
        state.cap_voltage_v = 25.0
        state.throttle_valid = True
        state.inhibit_motor_commands = False
        state.requested_mode = CommandMode.ASSIST
        state.requested_level = 1.0
        state.last_vesc_rx_ms = 1000
        set_clock_ms(1000)

        _run_chain(safety, sm, cl, cmd)

        # Throttle goes bad
        state.throttle_valid = False
        uart._tx_buf.clear()

        _run_chain(safety, sm, cl, cmd)
        assert state.system_state == SystemState.FAULT
        assert state.inhibit_motor_commands is True

    def test_fault_clears_regen_command(self):
        """Active regen command → fault → regen request zeroed in same cycle."""
        state, faults, safety, sm, cl, uart, cmd = _make_system()

        state.system_state = SystemState.REGEN
        state.cap_voltage_v = 25.0
        state.throttle_valid = True
        state.inhibit_motor_commands = False
        state.vesc_mech_rpm = 500.0
        state.vesc_motor_current_a = 20.0
        state.requested_mode = CommandMode.REGEN
        state.requested_level = 1.0
        state.last_vesc_rx_ms = 1000
        set_clock_ms(1000)

        # Build up regen command
        for _ in range(50):
            state.vesc_motor_current_a = state.regen_command_request
            cl.update()
        assert state.regen_command_request > 0.0

        # Inject fault
        state.cap_voltage_v = 43.0
        _run_chain(safety, sm, cl, cmd)
        assert state.regen_command_request == 0.0
