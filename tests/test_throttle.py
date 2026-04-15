# tests/test_throttle.py — Throttle driver: ADC → fraction, deadband, fault detection

import pytest
from drivers.throttle import Throttle
from config.settings import (
    THROTTLE_DEADBAND,
    THROTTLE_FAULT_HIGH,
    THROTTLE_FAULT_LOW,
    THROTTLE_RAW_MAX,
    THROTTLE_RAW_MIN,
)


def _make(raw_12bit=2000):
    """Create a Throttle with its internal ADC preset to a 12-bit value."""
    t = Throttle()
    t._adc._value = raw_12bit << 4
    return t


@pytest.mark.parametrize("raw,expect_frac", [
    pytest.param(2000, lambda f: 0.0 < f <= 1.0, id="midrange"),
    pytest.param(THROTTLE_RAW_MIN, lambda f: f == 0.0, id="min_zero"),
    pytest.param(THROTTLE_RAW_MAX, lambda f: abs(f - 1.0) < 0.01, id="max_one"),
    pytest.param(THROTTLE_RAW_MIN + 5, lambda f: f == 0.0, id="deadband"),
    pytest.param((THROTTLE_RAW_MIN + THROTTLE_RAW_MAX) // 2,
                 lambda f: f > 0.0, id="above_deadband"),
])
def test_normal_range(raw, expect_frac):
    t = _make(raw_12bit=raw)
    t.update()
    assert t.is_valid
    assert expect_frac(t.fraction)


@pytest.mark.parametrize("raw", [
    pytest.param(THROTTLE_FAULT_LOW - 10, id="below_low"),
    pytest.param(THROTTLE_FAULT_HIGH + 10, id="above_high"),
    pytest.param(0, id="zero"),
    pytest.param(4095, id="rail"),
])
def test_fault_detection(raw):
    t = _make(raw_12bit=raw)
    t.update()
    assert t.is_valid is False
    assert t.fraction == 0.0


@pytest.mark.parametrize("raw", [THROTTLE_FAULT_LOW, THROTTLE_FAULT_HIGH],
                         ids=["at_low", "at_high"])
def test_fault_boundary_is_valid(raw):
    t = _make(raw_12bit=raw)
    t.update()
    assert t.is_valid is True


def test_fraction_monotonic():
    """Increasing raw values produce non-decreasing fraction."""
    prev = -1.0
    for raw in range(THROTTLE_RAW_MIN, THROTTLE_RAW_MAX + 1, 100):
        t = _make(raw_12bit=raw)
        t.update()
        assert t.fraction >= prev or abs(t.fraction - prev) < 1e-6
        prev = t.fraction


def test_recovery_after_fault():
    t = _make(raw_12bit=50)
    t.update()
    assert t.is_valid is False
    t._adc._value = 2000 << 4
    t.update()
    assert t.is_valid is True
    assert t.fraction > 0.0
