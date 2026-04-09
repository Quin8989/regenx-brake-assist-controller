# tests/test_utils.py — clamp, linear_map, PeriodicTimer

from utils import clamp, linear_map


# ---- clamp ----

class TestClamp:
    def test_within_range(self):
        assert clamp(5, 0, 10) == 5

    def test_below_lo(self):
        assert clamp(-1, 0, 10) == 0

    def test_above_hi(self):
        assert clamp(15, 0, 10) == 10

    def test_at_lo(self):
        assert clamp(0, 0, 10) == 0

    def test_at_hi(self):
        assert clamp(10, 0, 10) == 10

    def test_float(self):
        assert clamp(0.5, 0.0, 1.0) == 0.5

    def test_negative_range(self):
        assert clamp(-5, -10, -1) == -5
        assert clamp(-15, -10, -1) == -10


# ---- linear_map ----

class TestLinearMap:
    def test_midpoint(self):
        assert linear_map(5, 0, 10, 0, 100) == 50.0

    def test_at_in_lo(self):
        assert linear_map(0, 0, 10, 100, 200) == 100.0

    def test_at_in_hi(self):
        assert linear_map(10, 0, 10, 100, 200) == 200.0

    def test_inverted_output(self):
        assert linear_map(5, 0, 10, 100, 0) == 50.0

    def test_divide_by_zero_safe(self):
        # in_lo == in_hi → returns out_lo
        assert linear_map(5, 5, 5, 100, 200) == 100.0

    def test_extrapolation(self):
        result = linear_map(15, 0, 10, 0, 100)
        assert result == 150.0

