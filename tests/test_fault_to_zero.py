# tests/test_fault_to_zero.py — TC-12: Fault → zero command timing < 100 ms
#
# Verifies that from the moment a critical fault is detected through safety
# supervisor → state machine → command manager, the VESC receives a zero/neutral
# command within a single update cycle (well under 100 ms at 100 Hz).

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
    def test_overvoltage_inhibits_in_one_cycle(self):
        """TC-12 / TC-13: Overvoltage fault → zero command in a single cycle."""
        state, faults, safety, sm, cl, uart, cmd = _make_system()

        # Start in ASSIST with active current
        state.system_state = SystemState.ASSIST
        state.cap_voltage_v = 25.0
        state.throttle_valid = True
        state.inhibit_motor_commands = False
        state.requested_mode = CommandMode.ASSIST
        state.requested_level = 1.0
        state.last_vesc_rx_ms = 1000
        set_clock_ms(1000)

        # Run one cycle to have a live assist command
        _run_chain(safety, sm, cl, cmd)
        assert state.system_state == SystemState.ASSIST

        # Inject overvoltage
        state.cap_voltage_v = 43.0
        uart._tx_buf.clear()

        # Single cycle should detect fault, transition to FAULT, send neutral
        _run_chain(safety, sm, cl, cmd)
        assert state.system_state == SystemState.FAULT
        assert state.inhibit_motor_commands is True
        assert state.assist_command_request == 0.0
        assert state.regen_command_request == 0.0
        # UART should have had a neutral command written
        assert len(uart._tx_buf) > 0

    def test_vesc_timeout_inhibits_in_one_cycle(self):
        """VESC timeout fault → zero command in one cycle."""
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

        # Advance time past telemetry timeout
        set_clock_ms(100 + 600)  # well past 500ms timeout
        uart._tx_buf.clear()

        _run_chain(safety, sm, cl, cmd)
        assert state.system_state == SystemState.FAULT
        assert state.inhibit_motor_commands is True
        assert len(uart._tx_buf) > 0

    def test_vesc_fault_code_inhibits_in_one_cycle(self):
        """VESC internal fault code → inhibit in one cycle."""
        state, faults, safety, sm, cl, uart, cmd = _make_system()

        state.system_state = SystemState.REGEN
        state.cap_voltage_v = 25.0
        state.throttle_valid = True
        state.inhibit_motor_commands = False
        state.requested_mode = CommandMode.REGEN
        state.requested_level = 1.0
        state.last_vesc_rx_ms = 1000
        set_clock_ms(1000)

        # Inject VESC fault code (e.g., 3 = DRV fault)
        state.vesc_fault_code = 3
        uart._tx_buf.clear()

        _run_chain(safety, sm, cl, cmd)
        assert state.system_state == SystemState.FAULT
        assert state.inhibit_motor_commands is True
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
        state.wheel_speed_rpm = 100.0
        state.wheel_speed_valid = True
        state.vesc_mech_rpm = 500.0
        state.requested_mode = CommandMode.REGEN
        state.requested_level = 1.0
        state.last_vesc_rx_ms = 1000
        set_clock_ms(1000)

        # Build up regen command
        for _ in range(50):
            cl.update()
        assert state.regen_command_request > 0.0

        # Inject fault
        state.cap_voltage_v = 43.0
        _run_chain(safety, sm, cl, cmd)
        assert state.regen_command_request == 0.0
