# tests/test_soft_reset.py — Soft reset via FaultManager.reset_all and AppController

from core import CommandMode, FaultCode, FaultManager, SharedState, SystemState
from services.control_loop import ControlLoop
from app.controller import AppController


def _make():
    state = SharedState()
    fm = FaultManager(state)
    cl = ControlLoop(state)
    return state, fm, cl


class _FakeButton:
    """Controllable reset button stub."""
    def __init__(self):
        self._pressed = False

    def poll(self):
        result = self._pressed
        self._pressed = False
        return result

    def press(self):
        self._pressed = True


class _Noop:
    """Stub for services that aren't under test."""
    def update(self):
        pass

    def service_rx(self):
        pass

    def request_telemetry(self):
        pass

    def debug(self, *args):
        pass


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
        precharge_mgr=noop,
        state_machine=noop,
        control_loop=cl,
        command_mgr=noop,
        energy=noop,
        display_mgr=noop,
        logger=noop,
        reset_button=btn,
        fault_manager=fm,
    )
    return state, fm, cl, btn, app


class TestSoftReset:
    def test_reset_clears_latching_fault_and_returns_to_off(self):
        s, fm, cl, btn, app = _make_app()
        s.system_state = SystemState.FAULT
        fm.set_fault(FaultCode.OVERVOLTAGE)
        s.inhibit_motor_commands = True

        btn.press()
        app.update()

        assert fm.has_fault() is False
        assert s.system_state == SystemState.OFF
        assert s.inhibit_motor_commands is True  # inhibited until COAST

    def test_reset_zeros_command_requests(self):
        s, fm, cl, btn, app = _make_app()
        s.system_state = SystemState.FAULT
        fm.set_fault(FaultCode.INTERNAL)
        s.assist_command_request = 10.0
        s.regen_command_request = 5.0

        btn.press()
        app.update()

        assert s.assist_command_request == 0.0
        assert s.regen_command_request == 0.0

    def test_reset_clears_requested_mode(self):
        s, fm, cl, btn, app = _make_app()
        s.system_state = SystemState.FAULT
        fm.set_fault(FaultCode.PRECHARGE_STALL)
        s.requested_mode = CommandMode.ASSIST
        s.requested_level = 0.8

        btn.press()
        app.update()

        assert s.requested_mode == CommandMode.NEUTRAL
        assert s.requested_level == 0.0

    def test_no_reset_without_press(self):
        s, fm, cl, btn, app = _make_app()
        s.system_state = SystemState.FAULT
        fm.set_fault(FaultCode.OVERVOLTAGE)

        app.update()  # no press

        assert fm.has_fault() is True
        assert s.system_state == SystemState.FAULT

    def test_reset_from_any_state(self):
        """Reset can be triggered from any state, not just FAULT."""
        s, fm, cl, btn, app = _make_app()
        s.system_state = SystemState.ASSIST
        s.inhibit_motor_commands = False

        btn.press()
        app.update()

        assert s.system_state == SystemState.OFF
        assert s.inhibit_motor_commands is True
