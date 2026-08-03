# tests/test_display_manager.py — display pages, fault labels, energy estimation

import math
import pytest

from config.settings import VCAP_MIN_OPERATING, VCAP_REGEN_TAPER_START_V
from core import FAULT_LABELS, FaultCode, FaultManager, SharedState, SystemState
from services.display_manager import DisplayManager


class _FakeLCD:
    def __init__(self):
        self.lines = {}
        self.write_calls = []

    def write_line(self, row, text):
        self.lines[row] = text
        self.write_calls.append((row, text))


class _FailLCD:
    def write_line(self, row, text):
        raise OSError("I2C NAK")


def _make(lcd=None):
    state = SharedState()
    faults = FaultManager(state)
    if lcd is None:
        lcd = _FakeLCD()
    dm = DisplayManager(lcd, state)
    return state, faults, lcd, dm


# ---- Page selection and content ----

@pytest.mark.parametrize("sys_state,setup,expect_in_line0", [
    pytest.param(SystemState.PRECHARGE, {"cap_voltage_v": 8.5}, "PRECHARGE", id="precharge"),
    pytest.param(SystemState.REGEN, {"cap_voltage_v": 20.5}, "REGEN", id="regen"),
    pytest.param(SystemState.REGEN, {"cap_voltage_v": 22.3}, "22.3", id="regen_voltage"),
])
def test_page_content(sys_state, setup, expect_in_line0):
    s, f, lcd, dm = _make()
    s.system_state = sys_state
    for k, v in setup.items():
        setattr(s, k, v)
    dm.update()
    assert expect_in_line0 in lcd.lines[0]


def test_assist_page_shows_current():
    s, f, lcd, dm = _make()
    s.system_state = SystemState.ASSIST
    s.cap_voltage_v = 25.0
    s.vesc_iq_current_a = 12.3
    for _ in range(20):
        dm.update()
    assert "ASSIST" in lcd.lines[0]
    assert "A" in lcd.lines[1]


def test_regen_page_shows_negative_current():
    s, f, lcd, dm = _make()
    s.system_state = SystemState.REGEN
    s.cap_voltage_v = 30.0
    s.vesc_input_current_a = -8.7
    for _ in range(20):
        dm.update()
    assert "-" in lcd.lines[1]


def test_energy_percent_shown():
    s, f, lcd, dm = _make()
    s.system_state = SystemState.REGEN
    s.cap_voltage_v = 25.0
    dm.update()
    assert "%" in lcd.lines[0]


# ---- Fault page ----

def test_fault_header():
    s, f, lcd, dm = _make()
    s.system_state = SystemState.FAULT
    f.set_fault(FaultCode.OVERVOLTAGE)
    dm.update()
    assert "FAULT" in lcd.lines[0]


@pytest.mark.parametrize("code", [
    FaultCode.OVERVOLTAGE, FaultCode.VESC_TIMEOUT,
    FaultCode.THROTTLE_RANGE, FaultCode.INTERNAL,
])
def test_fault_shows_label(code):
    s, f, lcd, dm = _make()
    s.system_state = SystemState.FAULT
    f.set_fault(code)
    dm.update()
    assert FAULT_LABELS[code][:16] in lcd.lines[1]


def test_fault_unknown_shows_fallback():
    s, f, lcd, dm = _make()
    s.system_state = SystemState.FAULT
    dm.update()
    assert "Unknown" in lcd.lines[1]


# ---- LCD resilience ----

def test_none_lcd_no_crash():
    dm = DisplayManager(None, SharedState())
    dm.update()


@pytest.mark.parametrize("sys_state", [SystemState.REGEN, SystemState.FAULT, SystemState.PRECHARGE])
def test_oserror_caught_silently(sys_state):
    s, f, _, dm = _make(lcd=_FailLCD())
    s.system_state = sys_state
    s.cap_voltage_v = 20.0
    if sys_state == SystemState.FAULT:
        f.set_fault(FaultCode.INTERNAL)
    dm.update()


# ---- LCD re-init watchdog ----

class _ReinitLCD(_FakeLCD):
    def __init__(self):
        super().__init__()
        self.reinit_calls = 0

    def reinit(self):
        self.reinit_calls += 1


def test_reinit_on_fault_edge():
    s, f, lcd, dm = _make(lcd=_ReinitLCD())
    s.cap_voltage_v = 25.0
    s.system_state = SystemState.REGEN
    dm.update()
    base = lcd.reinit_calls
    # Enter fault
    s.system_state = SystemState.FAULT
    f.set_fault(FaultCode.INTERNAL)
    dm.update()
    assert lcd.reinit_calls == base + 1
    # Exit fault
    f.reset_all()
    s.system_state = SystemState.REGEN
    dm.update()
    assert lcd.reinit_calls == base + 2


def test_reinit_on_state_transition():
    s, f, lcd, dm = _make(lcd=_ReinitLCD())
    s.cap_voltage_v = 25.0
    s.system_state = SystemState.PRECHARGE
    dm.update()
    base = lcd.reinit_calls

    s.system_state = SystemState.REGEN
    dm.update()

    assert lcd.reinit_calls == base + 1


def test_skips_redundant_lcd_writes():
    s, f, lcd, dm = _make()
    s.system_state = SystemState.REGEN
    s.cap_voltage_v = 25.0
    s.vesc_input_current_a = -3.2

    dm.update()
    first_call_count = len(lcd.write_calls)

    dm.update()

    assert len(lcd.write_calls) == first_call_count


def test_reinit_periodic(monkeypatch):
    from services import display_manager as dm_mod

    t = [0]
    monkeypatch.setattr(dm_mod, "ticks_ms", lambda: t[0])
    monkeypatch.setattr(dm_mod, "ticks_diff", lambda a, b: a - b)

    s, f, lcd, dm = _make(lcd=_ReinitLCD())
    s.cap_voltage_v = 25.0
    s.system_state = SystemState.REGEN
    dm.update()
    start = lcd.reinit_calls

    # Advance less than period — no re-init
    t[0] += dm_mod._LCD_REINIT_PERIOD_MS - 1
    dm.update()
    assert lcd.reinit_calls == start

    # Advance past period — re-init fires
    t[0] += 2
    dm.update()
    assert lcd.reinit_calls == start + 1


def test_reinit_absent_method_is_noop():
    # _FakeLCD has no reinit() — must not crash
    s, f, lcd, dm = _make()
    s.system_state = SystemState.REGEN
    s.cap_voltage_v = 25.0
    for _ in range(3):
        dm.update()


# ---- Energy estimation (absorbed from test_energy_estimator) ----

@pytest.mark.parametrize("voltage,expected", [
    pytest.param(0.0, 0.0, id="zero_voltage"),
    pytest.param(VCAP_MIN_OPERATING, 0.0, id="at_min"),
    pytest.param(VCAP_REGEN_TAPER_START_V, 100.0, id="at_taper_start"),
    pytest.param(5.0, 0.0, id="below_min_clamped"),
    pytest.param(45.0, 100.0, id="above_max_clamped"),
])
def test_energy_percent(voltage, expected):
    s, f, lcd, dm = _make()
    s.cap_voltage_v = voltage
    dm.update()
    assert abs(s.cap_energy_percent - expected) < 0.1


def test_energy_midpoint():
    s, f, lcd, dm = _make()
    v_mid = math.sqrt((VCAP_MIN_OPERATING**2 + VCAP_REGEN_TAPER_START_V**2) / 2.0)
    s.cap_voltage_v = v_mid
    dm.update()
    assert abs(s.cap_energy_percent - 50.0) < 1.0
