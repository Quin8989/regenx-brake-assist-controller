# tests/test_utils.py — clamp, linear_map, SlewLimiter, PeriodicTimer

from utils import SlewLimiter, clamp, linear_map


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


# ---- SlewLimiter ----

class TestSlewLimiter:
    def test_ramps_up(self):
        sl = SlewLimiter(max_delta=1.0, initial=0.0)
        assert sl.update(10.0) == 1.0
        assert sl.update(10.0) == 2.0
        assert sl.update(10.0) == 3.0

    def test_ramps_down(self):
        sl = SlewLimiter(max_delta=1.0, initial=5.0)
        assert sl.update(0.0) == 4.0
        assert sl.update(0.0) == 3.0

    def test_no_overshoot(self):
        sl = SlewLimiter(max_delta=5.0, initial=0.0)
        assert sl.update(3.0) == 3.0  # step < max_delta

    def test_reset(self):
        sl = SlewLimiter(max_delta=1.0, initial=10.0)
        sl.reset(0.0)
        assert sl.value == 0.0
        assert sl.update(0.5) == 0.5

    def test_holds_at_target(self):
        sl = SlewLimiter(max_delta=10.0, initial=5.0)
        assert sl.update(5.0) == 5.0

    def test_negative_to_positive(self):
        sl = SlewLimiter(max_delta=2.0, initial=-3.0)
        assert sl.update(5.0) == -1.0
        assert sl.update(5.0) == 1.0
