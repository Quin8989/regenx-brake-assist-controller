# tests/test_fault_manager.py — FaultManager latching, set, clear, has_fault

from core import FaultCode, FaultManager, LATCHING_FAULTS, SharedState


def _make():
    state = SharedState()
    fm = FaultManager(state)
    return state, fm


class TestSetClear:
    def test_set_adds_fault(self):
        s, fm = _make()
        fm.set_fault(FaultCode.VESC_TIMEOUT)
        assert FaultCode.VESC_TIMEOUT in s.fault_flags

    def test_clear_removes_non_latching(self):
        s, fm = _make()
        fm.set_fault(FaultCode.VESC_TIMEOUT)
        fm.clear_fault(FaultCode.VESC_TIMEOUT)
        assert FaultCode.VESC_TIMEOUT not in s.fault_flags

    def test_clear_ignores_latching(self):
        s, fm = _make()
        fm.set_fault(FaultCode.OVERVOLTAGE)
        fm.clear_fault(FaultCode.OVERVOLTAGE)
        assert FaultCode.OVERVOLTAGE in s.fault_flags  # still present

    def test_all_latching_faults_are_sticky(self):
        for code in LATCHING_FAULTS:
            s, fm = _make()
            fm.set_fault(code)
            fm.clear_fault(code)
            assert code in s.fault_flags, f"{code} should be latching"

    def test_non_latching_faults_clear(self):
        non_latching = [
            FaultCode.VESC_TIMEOUT,
            FaultCode.THROTTLE_RANGE,
            FaultCode.VESC_FAULT,
        ]
        for code in non_latching:
            s, fm = _make()
            fm.set_fault(code)
            fm.clear_fault(code)
            assert code not in s.fault_flags, f"{code} should clear"


class TestHasFault:
    def test_no_faults(self):
        _, fm = _make()
        assert fm.has_fault() is False

    def test_has_fault_true(self):
        _, fm = _make()
        fm.set_fault(FaultCode.VESC_TIMEOUT)
        assert fm.has_fault() is True

    def test_has_fault_after_clear(self):
        _, fm = _make()
        fm.set_fault(FaultCode.VESC_TIMEOUT)
        fm.clear_fault(FaultCode.VESC_TIMEOUT)
        assert fm.has_fault() is False

    def test_multiple_faults(self):
        _, fm = _make()
        fm.set_fault(FaultCode.VESC_TIMEOUT)
        fm.set_fault(FaultCode.THROTTLE_RANGE)
        fm.clear_fault(FaultCode.VESC_TIMEOUT)
        assert fm.has_fault() is True  # THROTTLE_RANGE still active


class TestResetAll:
    def test_reset_all_clears_non_latching(self):
        s, fm = _make()
        fm.set_fault(FaultCode.VESC_TIMEOUT)
        fm.set_fault(FaultCode.THROTTLE_RANGE)
        fm.reset_all()
        assert fm.has_fault() is False

    def test_reset_all_clears_latching(self):
        s, fm = _make()
        fm.set_fault(FaultCode.OVERVOLTAGE)
        fm.set_fault(FaultCode.INTERNAL)
        fm.reset_all()
        assert fm.has_fault() is False

    def test_reset_all_clears_mixed(self):
        s, fm = _make()
        fm.set_fault(FaultCode.VESC_TIMEOUT)
        fm.set_fault(FaultCode.OVERVOLTAGE)
        fm.set_fault(FaultCode.THROTTLE_RANGE)
        fm.reset_all()
        assert len(s.fault_flags) == 0


class TestFaultText:
    def test_known_code(self):
        _, fm = _make()
        assert fm.fault_text(FaultCode.OVERVOLTAGE) == "Overvoltage"

    def test_unknown_code(self):
        _, fm = _make()
        assert fm.fault_text("MYSTERY") == "MYSTERY"
