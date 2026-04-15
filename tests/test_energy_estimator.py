# tests/test_energy_estimator.py — DisplayManager energy estimation (½CV²)

from config.settings import VCAP_MIN_OPERATING, VCAP_SOFT_REGEN_CUTOFF
from core import SharedState
from services.display_manager import DisplayManager


def _make():
    state = SharedState()
    dm = DisplayManager(None, state)  # no LCD needed for energy calc
    return state, dm


class TestEnergyCalculation:
    def test_zero_voltage(self):
        s, dm = _make()
        s.cap_voltage_v = 0.0
        dm.update()
        assert s.cap_energy_percent == 0.0

    def test_at_min_operating(self):
        s, dm = _make()
        s.cap_voltage_v = VCAP_MIN_OPERATING
        dm.update()
        assert abs(s.cap_energy_percent - 0.0) < 0.1

    def test_at_soft_cutoff(self):
        s, dm = _make()
        s.cap_voltage_v = VCAP_SOFT_REGEN_CUTOFF
        dm.update()
        assert abs(s.cap_energy_percent - 100.0) < 0.1

    def test_midpoint(self):
        s, dm = _make()
        import math
        v_mid = math.sqrt((VCAP_MIN_OPERATING**2 + VCAP_SOFT_REGEN_CUTOFF**2) / 2.0)
        s.cap_voltage_v = v_mid
        dm.update()
        assert abs(s.cap_energy_percent - 50.0) < 1.0

    def test_below_min_clamped_to_zero(self):
        s, dm = _make()
        s.cap_voltage_v = 5.0
        dm.update()
        assert s.cap_energy_percent == 0.0  # clamped

    def test_above_max_clamped_to_100(self):
        s, dm = _make()
        s.cap_voltage_v = 45.0  # above soft cutoff
        dm.update()
        assert s.cap_energy_percent == 100.0
