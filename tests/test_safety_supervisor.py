# tests/test_safety_supervisor.py — SafetySupervisor check coverage

from tests.conftest import advance_ms, set_clock_ms

from core import FaultCode, FaultManager, SharedState, SystemState
from services.safety_supervisor import SafetySupervisor


def _make():
    state = SharedState()
    faults = FaultManager(state)
    sv = SafetySupervisor(state, faults)
    return state, faults, sv


# ---- Overvoltage ----

class TestOvervoltage:
    def test_overvoltage_sets_fault(self):
        s, f, sv = _make()
        s.cap_voltage_v = 42.0
        sv.update()
        assert FaultCode.OVERVOLTAGE in s.fault_flags

    def test_below_threshold_no_fault(self):
        s, f, sv = _make()
        s.cap_voltage_v = 41.9
        sv.update()
        assert FaultCode.OVERVOLTAGE not in s.fault_flags

    def test_overvoltage_is_latching(self):
        s, f, sv = _make()
        s.cap_voltage_v = 42.0
        sv.update()
        s.cap_voltage_v = 30.0
        sv.update()
        assert FaultCode.OVERVOLTAGE in s.fault_flags  # still latched


# ---- Undervoltage ----

class TestUndervoltage:
    def test_undervoltage_in_assist(self):
        s, f, sv = _make()
        s.system_state = SystemState.ASSIST
        s.cap_voltage_v = 10.0
        sv.update()
        assert FaultCode.UNDERVOLTAGE in s.fault_flags

    def test_undervoltage_in_regen(self):
        s, f, sv = _make()
        s.system_state = SystemState.REGEN
        s.cap_voltage_v = 10.0
        sv.update()
        assert FaultCode.UNDERVOLTAGE in s.fault_flags

    def test_no_undervoltage_in_precharge(self):
        s, f, sv = _make()
        s.system_state = SystemState.PRECHARGE
        s.cap_voltage_v = 5.0
        sv.update()
        assert FaultCode.UNDERVOLTAGE not in s.fault_flags

    def test_undervoltage_clears_when_voltage_returns(self):
        s, f, sv = _make()
        s.system_state = SystemState.ASSIST
        s.cap_voltage_v = 10.0
        sv.update()
        assert FaultCode.UNDERVOLTAGE in s.fault_flags
        s.cap_voltage_v = 20.0
        sv.update()
        assert FaultCode.UNDERVOLTAGE not in s.fault_flags


# ---- Telemetry health ----

class TestTelemetryHealth:
    def test_timeout_in_ready(self):
        s, f, sv = _make()
        s.system_state = SystemState.READY
        s.cap_voltage_v = 20.0
        s.last_vesc_rx_ms = 0
        set_clock_ms(100)
        s.last_vesc_rx_ms = 100
        set_clock_ms(700)  # 600 ms since last packet
        sv.update()
        assert FaultCode.VESC_TIMEOUT in s.fault_flags

    def test_no_timeout_when_fresh(self):
        s, f, sv = _make()
        s.system_state = SystemState.READY
        s.cap_voltage_v = 20.0
        set_clock_ms(100)
        s.last_vesc_rx_ms = 100
        set_clock_ms(200)  # 100 ms — within 500 ms window
        sv.update()
        assert FaultCode.VESC_TIMEOUT not in s.fault_flags

    def test_exempt_in_precharge(self):
        s, f, sv = _make()
        s.system_state = SystemState.PRECHARGE
        s.last_vesc_rx_ms = 1
        set_clock_ms(10000)
        sv.update()
        assert FaultCode.VESC_TIMEOUT not in s.fault_flags

    def test_exempt_in_off(self):
        s, f, sv = _make()
        s.system_state = SystemState.OFF
        s.last_vesc_rx_ms = 1
        set_clock_ms(10000)
        sv.update()
        assert FaultCode.VESC_TIMEOUT not in s.fault_flags

    def test_no_fault_before_first_packet(self):
        s, f, sv = _make()
        s.system_state = SystemState.READY
        s.cap_voltage_v = 20.0
        s.last_vesc_rx_ms = 0  # never received
        set_clock_ms(10000)
        sv.update()
        assert FaultCode.VESC_TIMEOUT not in s.fault_flags

    def test_timeout_clears_when_packet_arrives(self):
        s, f, sv = _make()
        s.system_state = SystemState.READY
        s.cap_voltage_v = 20.0
        set_clock_ms(100)
        s.last_vesc_rx_ms = 100
        set_clock_ms(700)
        sv.update()
        assert FaultCode.VESC_TIMEOUT in s.fault_flags
        set_clock_ms(710)
        s.last_vesc_rx_ms = 710
        sv.update()
        assert FaultCode.VESC_TIMEOUT not in s.fault_flags


# ---- VESC fault code ----

class TestVescFault:
    def test_nonzero_sets_fault(self):
        s, f, sv = _make()
        s.vesc_fault_code = 3  # DRV error
        sv.update()
        assert FaultCode.VESC_FAULT in s.fault_flags

    def test_zero_clears_fault(self):
        s, f, sv = _make()
        s.vesc_fault_code = 5
        sv.update()
        assert FaultCode.VESC_FAULT in s.fault_flags
        s.vesc_fault_code = 0
        sv.update()
        assert FaultCode.VESC_FAULT not in s.fault_flags

    def test_zero_no_fault(self):
        s, f, sv = _make()
        s.vesc_fault_code = 0
        sv.update()
        assert FaultCode.VESC_FAULT not in s.fault_flags


# ---- Throttle validity ----

class TestThrottleValidity:
    def test_invalid_throttle_sets_fault(self):
        s, f, sv = _make()
        s.throttle_valid = False
        sv.update()
        assert FaultCode.THROTTLE_RANGE in s.fault_flags

    def test_valid_throttle_clears_fault(self):
        s, f, sv = _make()
        s.throttle_valid = False
        sv.update()
        s.throttle_valid = True
        sv.update()
        assert FaultCode.THROTTLE_RANGE not in s.fault_flags


# ---- Inhibit policy ----

class TestInhibits:
    def test_inhibit_on_fault(self):
        s, f, sv = _make()
        s.system_state = SystemState.READY
        s.cap_voltage_v = 20.0
        s.throttle_valid = True
        f.set_fault(FaultCode.INTERNAL)
        sv.update()
        assert s.inhibit_motor_commands is True

    def test_inhibit_on_low_voltage(self):
        s, f, sv = _make()
        s.system_state = SystemState.PRECHARGE
        s.cap_voltage_v = 5.0
        s.throttle_valid = True
        sv.update()
        assert s.inhibit_motor_commands is True

    def test_inhibit_in_off_state(self):
        s, f, sv = _make()
        s.system_state = SystemState.OFF
        s.cap_voltage_v = 20.0
        s.throttle_valid = True
        sv.update()
        assert s.inhibit_motor_commands is True

    def test_no_inhibit_in_ready(self):
        s, f, sv = _make()
        s.system_state = SystemState.READY
        s.cap_voltage_v = 20.0
        s.throttle_valid = True
        sv.update()
        assert s.inhibit_motor_commands is False

    def test_no_inhibit_in_assist(self):
        s, f, sv = _make()
        s.system_state = SystemState.ASSIST
        s.cap_voltage_v = 20.0
        s.throttle_valid = True
        set_clock_ms(10)
        s.last_vesc_rx_ms = 10
        sv.update()
        assert s.inhibit_motor_commands is False
