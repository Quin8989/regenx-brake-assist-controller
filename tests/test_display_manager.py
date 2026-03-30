# tests/test_display_manager.py — DisplayManager page selection and LCD safety

from core import FAULT_LABELS, FaultCode, FaultManager, SharedState, SystemState
from services.display_manager import DisplayManager


class _FakeLCD:
    """Records write_line calls for assertion."""
    def __init__(self):
        self.lines = {}

    def write_line(self, row, text):
        self.lines[row] = text


class _FailLCD:
    """Always raises OSError to simulate a disconnected display."""
    def write_line(self, row, text):
        raise OSError("I2C NAK")


def _make(lcd=None):
    state = SharedState()
    faults = FaultManager(state)
    if lcd is None:
        lcd = _FakeLCD()
    dm = DisplayManager(lcd, state)
    return state, faults, lcd, dm


class TestPageSelection:
    def test_fault_page(self):
        s, f, lcd, dm = _make()
        s.system_state = SystemState.FAULT
        f.set_fault(FaultCode.OVERVOLTAGE)
        dm.update()
        assert "FAULT" in lcd.lines[0]

    def test_precharge_page(self):
        s, f, lcd, dm = _make()
        s.system_state = SystemState.PRECHARGE
        s.cap_voltage_v = 8.5
        dm.update()
        assert "PRECHARGE" in lcd.lines[0]
        assert "8.5" in lcd.lines[1]

    def test_run_page_in_ready(self):
        s, f, lcd, dm = _make()
        s.system_state = SystemState.REGEN
        s.cap_voltage_v = 20.5
        dm.update()
        assert "REGEN" in lcd.lines[0]
        assert "20.5" in lcd.lines[0]

    def test_run_page_in_assist(self):
        s, f, lcd, dm = _make()
        s.system_state = SystemState.ASSIST
        s.cap_voltage_v = 25.0
        s.cap_energy_percent = 42.0
        s.vesc_iq_current_a = 12.3
        s.wheel_speed_valid = True
        s.wheel_speed_rpm = 100.0
        dm.update()
        assert "ASSIST" in lcd.lines[0]
        assert "km/h" in lcd.lines[1]

    def test_run_page_shows_signed_vesc_iq_in_regen(self):
        s, f, lcd, dm = _make()
        s.system_state = SystemState.REGEN
        s.cap_voltage_v = 30.0
        s.vesc_iq_current_a = -8.7
        s.wheel_speed_valid = True
        s.wheel_speed_rpm = 80.0
        dm.update()
        assert "-8.7A" in lcd.lines[1]
        assert "km/h" in lcd.lines[1]


class TestNoneLCD:
    def test_none_lcd_no_crash(self):
        s = SharedState()
        dm = DisplayManager(None, s)
        dm.update()  # should not raise


class TestLcdFaultTolerance:
    def test_oserror_caught_silently(self):
        lcd = _FailLCD()
        s, f, _, dm = _make(lcd=lcd)
        s.system_state = SystemState.REGEN
        s.cap_voltage_v = 20.0
        # Should not raise
        dm.update()

    def test_oserror_in_fault_page(self):
        lcd = _FailLCD()
        s, f, _, dm = _make(lcd=lcd)
        s.system_state = SystemState.FAULT
        f.set_fault(FaultCode.INTERNAL)
        dm.update()  # should not raise

    def test_oserror_in_precharge_page(self):
        lcd = _FailLCD()
        s, f, _, dm = _make(lcd=lcd)
        s.system_state = SystemState.PRECHARGE
        dm.update()  # should not raise


# ---- TC-15: Display page correctness — COAST ----

class TestRunPageContent:
    """TC-15: COAST page shows state, voltage, and energy percentage."""

    def test_ready_shows_voltage_value(self):
        s, f, lcd, dm = _make()
        s.system_state = SystemState.REGEN
        s.cap_voltage_v = 22.3
        dm.update()
        assert "22.3" in lcd.lines[0]

    def test_ready_shows_energy_percent(self):
        s, f, lcd, dm = _make()
        s.system_state = SystemState.REGEN
        s.cap_voltage_v = 25.0
        s.cap_energy_percent = 67.0
        dm.update()
        assert "67" in lcd.lines[0]
        assert "%" in lcd.lines[0]

    def test_assist_shows_current(self):
        s, f, lcd, dm = _make()
        s.system_state = SystemState.ASSIST
        s.cap_voltage_v = 25.0
        s.vesc_iq_current_a = 15.2
        dm.update()
        assert "+15.2" in lcd.lines[1]
        assert "A" in lcd.lines[1]

    def test_regen_shows_state_name(self):
        s, f, lcd, dm = _make()
        s.system_state = SystemState.REGEN
        s.cap_voltage_v = 30.0
        dm.update()
        assert "REGEN" in lcd.lines[0]

    def test_run_page_shows_unknown_speed_when_wheel_invalid(self):
        s, f, lcd, dm = _make()
        s.system_state = SystemState.REGEN
        s.cap_voltage_v = 22.0
        s.wheel_speed_valid = False
        dm.update()
        assert "--.-km/h" in lcd.lines[1]


# ---- TC-16: Display page correctness — FAULT ----

class TestFaultPageContent:
    """TC-16: FAULT page shows 'FAULT' text and the fault description."""

    def test_fault_shows_fault_header(self):
        s, f, lcd, dm = _make()
        s.system_state = SystemState.FAULT
        f.set_fault(FaultCode.OVERVOLTAGE)
        dm.update()
        assert "FAULT" in lcd.lines[0]

    def test_fault_shows_overvoltage_label(self):
        s, f, lcd, dm = _make()
        s.system_state = SystemState.FAULT
        f.set_fault(FaultCode.OVERVOLTAGE)
        dm.update()
        expected = FAULT_LABELS[FaultCode.OVERVOLTAGE]
        assert expected[:16] in lcd.lines[1]

    def test_fault_shows_vesc_timeout_label(self):
        s, f, lcd, dm = _make()
        s.system_state = SystemState.FAULT
        f.set_fault(FaultCode.VESC_TIMEOUT)
        dm.update()
        expected = FAULT_LABELS[FaultCode.VESC_TIMEOUT]
        assert expected[:16] in lcd.lines[1]

    def test_fault_shows_throttle_label(self):
        s, f, lcd, dm = _make()
        s.system_state = SystemState.FAULT
        f.set_fault(FaultCode.THROTTLE_RANGE)
        dm.update()
        expected = FAULT_LABELS[FaultCode.THROTTLE_RANGE]
        assert expected[:16] in lcd.lines[1]

    def test_fault_shows_internal_label(self):
        s, f, lcd, dm = _make()
        s.system_state = SystemState.FAULT
        f.set_fault(FaultCode.INTERNAL)
        dm.update()
        expected = FAULT_LABELS[FaultCode.INTERNAL]
        assert expected[:16] in lcd.lines[1]

    def test_fault_unknown_shows_fallback(self):
        s, f, lcd, dm = _make()
        s.system_state = SystemState.FAULT
        # No faults set → should show "Unknown"
        dm.update()
        assert "Unknown" in lcd.lines[1]

    def test_fault_overrides_normal_page(self):
        """Fault page should appear regardless of other state data."""
        s, f, lcd, dm = _make()
        s.system_state = SystemState.FAULT
        s.cap_voltage_v = 25.0  # Would normally trigger run page
        f.set_fault(FaultCode.INTERNAL)
        dm.update()
        assert "FAULT" in lcd.lines[0]
        # Run page content should NOT appear
        assert "COAST" not in lcd.lines[0]
        assert "ASSIST" not in lcd.lines[0]
