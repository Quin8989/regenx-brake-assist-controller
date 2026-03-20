# tests/test_energy_estimator.py — EnergyEstimator ½CV² and percentage

from core import EnergyEstimator, SharedState


def _make():
    state = SharedState()
    ee = EnergyEstimator(state)
    return state, ee


class TestEnergyCalculation:
    def test_zero_voltage(self):
        s, ee = _make()
        s.cap_voltage_v = 0.0
        ee.update()
        assert s.cap_energy_j == 0.0
        assert s.cap_energy_percent == 0.0

    def test_at_min_operating(self):
        s, ee = _make()
        s.cap_voltage_v = 15.0
        ee.update()
        expected_j = 0.5 * 20.0 * 15.0 * 15.0  # 2250 J
        assert abs(s.cap_energy_j - expected_j) < 0.01
        assert abs(s.cap_energy_percent - 0.0) < 0.1

    def test_at_soft_cutoff(self):
        s, ee = _make()
        s.cap_voltage_v = 40.0
        ee.update()
        assert abs(s.cap_energy_percent - 100.0) < 0.1

    def test_midpoint(self):
        s, ee = _make()
        # Halfway energy is at sqrt((15² + 40²)/2) ≈ 30.23 V
        import math
        v_mid = math.sqrt((15.0**2 + 40.0**2) / 2.0)
        s.cap_voltage_v = v_mid
        ee.update()
        assert abs(s.cap_energy_percent - 50.0) < 1.0

    def test_below_min_clamped_to_zero(self):
        s, ee = _make()
        s.cap_voltage_v = 5.0
        ee.update()
        assert s.cap_energy_percent == 0.0  # clamped

    def test_above_max_clamped_to_100(self):
        s, ee = _make()
        s.cap_voltage_v = 45.0  # above soft cutoff
        ee.update()
        assert s.cap_energy_percent == 100.0

    def test_energy_j_is_half_cv_squared(self):
        s, ee = _make()
        s.cap_voltage_v = 25.0
        ee.update()
        expected = 0.5 * 20.0 * 25.0 * 25.0
        assert abs(s.cap_energy_j - expected) < 0.01
