# tests/test_fault_manager.py — FaultManager set/clear, latching, reset

import pytest
from core import FaultCode, FaultManager, LATCHING_FAULTS, SharedState


def _make():
    state = SharedState()
    return state, FaultManager(state)


@pytest.mark.parametrize("code,latching", [
    pytest.param(FaultCode.VESC_TIMEOUT, False, id="vesc_timeout"),
    pytest.param(FaultCode.THROTTLE_RANGE, False, id="throttle_range"),
    pytest.param(FaultCode.VESC_FAULT, False, id="vesc_fault"),
    pytest.param(FaultCode.OVERVOLTAGE, True, id="overvoltage"),
    pytest.param(FaultCode.INTERNAL, True, id="internal"),
])
def test_set_and_clear(code, latching):
    """Set adds fault; clear removes non-latching but keeps latching."""
    s, fm = _make()
    fm.set_fault(code)
    assert code in s.fault_flags
    fm.clear_fault(code)
    assert (code in s.fault_flags) == latching


def test_has_fault_lifecycle():
    """has_fault tracks active faults; partial clear leaves remainder."""
    s, fm = _make()
    assert fm.has_fault() is False
    fm.set_fault(FaultCode.VESC_TIMEOUT)
    assert fm.has_fault() is True
    fm.set_fault(FaultCode.THROTTLE_RANGE)
    fm.clear_fault(FaultCode.VESC_TIMEOUT)
    assert fm.has_fault() is True  # THROTTLE_RANGE remains
    fm.clear_fault(FaultCode.THROTTLE_RANGE)
    assert fm.has_fault() is False


def test_reset_all():
    """reset_all clears everything including latching faults."""
    s, fm = _make()
    fm.set_fault(FaultCode.OVERVOLTAGE)
    fm.set_fault(FaultCode.VESC_TIMEOUT)
    fm.set_fault(FaultCode.INTERNAL)
    fm.reset_all()
    assert len(s.fault_flags) == 0


def test_fault_text():
    _, fm = _make()
    assert fm.fault_text(FaultCode.OVERVOLTAGE) == "Overvoltage"
    assert fm.fault_text("MYSTERY") == "MYSTERY"
