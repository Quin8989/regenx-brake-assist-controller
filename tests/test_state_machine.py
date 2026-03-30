# tests/test_state_machine.py — StateMachine transition coverage

from app.state_machine import StateMachine
from core import CommandMode, FaultCode, FaultManager, SharedState, SystemState


def _make():
    state = SharedState()
    faults = FaultManager(state)
    sm = StateMachine(state, faults)
    return state, faults, sm


# ---- Initial / OFF transitions ----

class TestOffTransitions:
    def test_off_to_precharge_when_low_voltage(self):
        s, f, sm = _make()
        s.system_state = SystemState.OFF
        s.cap_voltage_v = 5.0
        sm.update()
        assert s.system_state == SystemState.PRECHARGE

    def test_off_to_regen_when_voltage_ok(self):
        s, f, sm = _make()
        s.system_state = SystemState.OFF
        s.cap_voltage_v = 20.0
        sm.update()
        assert s.system_state == SystemState.REGEN

    def test_off_to_precharge_at_exactly_threshold(self):
        s, f, sm = _make()
        s.system_state = SystemState.OFF
        s.cap_voltage_v = 14.99
        sm.update()
        assert s.system_state == SystemState.PRECHARGE

    def test_off_to_regen_at_exactly_threshold(self):
        s, f, sm = _make()
        s.system_state = SystemState.OFF
        s.cap_voltage_v = 15.0
        sm.update()
        assert s.system_state == SystemState.REGEN


# ---- PRECHARGE transitions ----

class TestPrechargeTransitions:
    def test_precharge_stays_until_threshold(self):
        s, f, sm = _make()
        s.system_state = SystemState.PRECHARGE
        s.cap_voltage_v = 10.0
        sm.update()
        assert s.system_state == SystemState.PRECHARGE

    def test_precharge_to_regen(self):
        s, f, sm = _make()
        s.system_state = SystemState.PRECHARGE
        s.cap_voltage_v = 16.0
        sm.update()
        assert s.system_state == SystemState.REGEN


# ---- ASSIST transitions ----

class TestAssistTransitions:
    def test_assist_to_regen(self):
        s, f, sm = _make()
        s.system_state = SystemState.ASSIST
        s.cap_voltage_v = 20.0
        s.requested_mode = CommandMode.REGEN
        sm.update()
        assert s.system_state == SystemState.REGEN

    def test_assist_to_regen_on_neutral(self):
        s, f, sm = _make()
        s.system_state = SystemState.ASSIST
        s.cap_voltage_v = 20.0
        s.requested_mode = CommandMode.NEUTRAL
        sm.update()
        assert s.system_state == SystemState.REGEN

    def test_assist_stays_on_assist(self):
        s, f, sm = _make()
        s.system_state = SystemState.ASSIST
        s.cap_voltage_v = 20.0
        s.requested_mode = CommandMode.ASSIST
        sm.update()
        assert s.system_state == SystemState.ASSIST


# ---- REGEN transitions ----

class TestRegenTransitions:
    def test_regen_to_assist(self):
        s, f, sm = _make()
        s.system_state = SystemState.REGEN
        s.cap_voltage_v = 20.0
        s.requested_mode = CommandMode.ASSIST
        sm.update()
        assert s.system_state == SystemState.ASSIST

    def test_regen_stays_on_neutral(self):
        s, f, sm = _make()
        s.system_state = SystemState.REGEN
        s.cap_voltage_v = 20.0
        s.requested_mode = CommandMode.NEUTRAL
        sm.update()
        assert s.system_state == SystemState.REGEN

    def test_regen_stays_on_regen(self):
        s, f, sm = _make()
        s.system_state = SystemState.REGEN
        s.cap_voltage_v = 20.0
        s.requested_mode = CommandMode.REGEN
        sm.update()
        assert s.system_state == SystemState.REGEN


# ---- FAULT transitions ----

class TestFaultTransitions:
    def test_any_state_to_fault(self):
        for start in (SystemState.OFF, SystemState.PRECHARGE,
                      SystemState.ASSIST, SystemState.REGEN):
            s, f, sm = _make()
            s.system_state = start
            s.cap_voltage_v = 20.0
            f.set_fault(FaultCode.OVERVOLTAGE)
            sm.update()
            assert s.system_state == SystemState.FAULT
            assert s.inhibit_motor_commands is True

    def test_fault_to_regen_when_cleared(self):
        s, f, sm = _make()
        s.system_state = SystemState.FAULT
        # No faults active
        sm.update()
        assert s.system_state == SystemState.REGEN
        assert s.inhibit_motor_commands is True

    def test_fault_stays_while_fault_active(self):
        s, f, sm = _make()
        s.system_state = SystemState.FAULT
        f.set_fault(FaultCode.OVERVOLTAGE)
        sm.update()
        assert s.system_state == SystemState.FAULT

    def test_fault_inhibits_motor(self):
        s, f, sm = _make()
        s.system_state = SystemState.REGEN
        s.cap_voltage_v = 20.0
        s.inhibit_motor_commands = False
        f.set_fault(FaultCode.INTERNAL)
        sm.update()
        assert s.inhibit_motor_commands is True
