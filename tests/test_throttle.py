# tests/test_throttle.py — Throttle driver: ADC → fraction, deadband, fault thresholds

from machine import ADC, Pin
from drivers.throttle import Throttle
from config.settings import (
    THROTTLE_ADC_PIN,
    THROTTLE_DEADBAND,
    THROTTLE_FAULT_HIGH,
    THROTTLE_FAULT_LOW,
    THROTTLE_RAW_MAX,
    THROTTLE_RAW_MIN,
)


def _make(raw_12bit=2000):
    """Create a Throttle with its internal ADC preset to a 12-bit value."""
    t = Throttle()
    # _FakeADC.read_u16 returns _value directly; Throttle does >> 4 to get 12-bit.
    # So we set the 16-bit value to raw_12bit << 4.
    t._adc._value = raw_12bit << 4
    return t


class TestNormalOperation:
    def test_midrange_produces_nonzero_fraction(self):
        t = _make(raw_12bit=2000)
        t.update()
        assert t.is_valid
        assert 0.0 < t.fraction <= 1.0

    def test_min_produces_zero(self):
        t = _make(raw_12bit=THROTTLE_RAW_MIN)
        t.update()
        assert t.is_valid
        # At min, linear_map gives 0.0, which is below deadband
        assert t.fraction == 0.0

    def test_max_produces_one(self):
        t = _make(raw_12bit=THROTTLE_RAW_MAX)
        t.update()
        assert t.is_valid
        assert abs(t.fraction - 1.0) < 0.01

    def test_fraction_monotonic(self):
        """Increasing raw values → non-decreasing fraction."""
        prev = -1.0
        for raw in range(THROTTLE_RAW_MIN, THROTTLE_RAW_MAX + 1, 100):
            t = _make(raw_12bit=raw)
            t.update()
            assert t.fraction >= prev or abs(t.fraction - prev) < 1e-6
            prev = t.fraction

    def test_fraction_clamped_to_01(self):
        # Slightly above max range → clamped to 1.0
        t = _make(raw_12bit=THROTTLE_RAW_MAX + 50)
        t.update()
        if t.is_valid:
            assert t.fraction <= 1.0


class TestDeadband:
    def test_just_above_min_is_deadband(self):
        """Very small throttle → suppressed to 0.0."""
        # A raw value that maps to fraction < THROTTLE_DEADBAND
        small_raw = THROTTLE_RAW_MIN + 5
        t = _make(raw_12bit=small_raw)
        t.update()
        assert t.fraction == 0.0

    def test_above_deadband_is_nonzero(self):
        # A raw value comfortably above deadband
        mid_raw = (THROTTLE_RAW_MIN + THROTTLE_RAW_MAX) // 2
        t = _make(raw_12bit=mid_raw)
        t.update()
        assert t.fraction > 0.0


class TestFaultDetection:
    def test_below_fault_low(self):
        t = _make(raw_12bit=THROTTLE_FAULT_LOW - 10)
        t.update()
        assert t.is_valid is False
        assert t.fraction == 0.0

    def test_above_fault_high(self):
        t = _make(raw_12bit=THROTTLE_FAULT_HIGH + 10)
        t.update()
        assert t.is_valid is False
        assert t.fraction == 0.0

    def test_at_fault_low_boundary(self):
        """Exactly at fault_low should still be valid."""
        t = _make(raw_12bit=THROTTLE_FAULT_LOW)
        t.update()
        assert t.is_valid is True

    def test_at_fault_high_boundary(self):
        """Exactly at fault_high should still be valid."""
        t = _make(raw_12bit=THROTTLE_FAULT_HIGH)
        t.update()
        assert t.is_valid is True

    def test_recovery_after_fault(self):
        """Throttle returns to valid after a fault reading clears."""
        t = _make(raw_12bit=50)
        t.update()
        assert t.is_valid is False

        # Return to mid-range
        t._adc._value = 2000 << 4
        t.update()
        assert t.is_valid is True
        assert t.fraction > 0.0

    def test_zero_raw_is_fault(self):
        t = _make(raw_12bit=0)
        t.update()
        assert t.is_valid is False

    def test_max_raw_is_fault(self):
        t = _make(raw_12bit=4095)
        t.update()
        assert t.is_valid is False
