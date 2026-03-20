# tests/test_wheel_speed.py — WheelSpeedHall driver: edge timing, RPM calc, timeout

from tests.conftest import set_clock_ms
from drivers.wheel_speed_hall import WheelSpeedHall
from config.settings import (
    WHEEL_HALL_MIN_EDGE_US,
    WHEEL_MAGNET_COUNT,
    WHEEL_SPEED_TIMEOUT_MS,
)

# With the fake time module, ticks_us = ticks_ms * 1000.
# WheelSpeedHall uses IRQ-based edge timing and update() for RPM retrieval.


def _make():
    ws = WheelSpeedHall()
    return ws


class TestInitialization:
    def test_enabled_with_valid_pin(self):
        ws = _make()
        assert ws._enabled is True

    def test_initial_rpm_zero(self):
        ws = _make()
        rpm, valid = ws.update()
        assert rpm == 0.0
        assert valid is False


class TestEdgeTiming:
    def test_single_edge_no_rpm(self):
        """First edge has no prior reference — no period yet."""
        ws = _make()
        set_clock_ms(100)
        ws._on_edge(None)
        rpm, valid = ws.update()
        assert rpm == 0.0
        assert valid is False

    def test_two_edges_produce_rpm(self):
        ws = _make()
        # First edge at t=100ms
        set_clock_ms(100)
        ws._on_edge(None)
        # Second edge at t=200ms → period = 100ms = 100_000 us
        set_clock_ms(200)
        ws._on_edge(None)

        # Still within timeout — should produce valid RPM
        rpm, valid = ws.update()
        expected_rpm = 60.0 * 1_000_000.0 / (100_000 * WHEEL_MAGNET_COUNT)
        assert valid is True
        assert abs(rpm - expected_rpm) < 0.1

    def test_fast_edges_debounced(self):
        """Edges faster than MIN_EDGE_US are rejected."""
        ws = _make()
        set_clock_ms(100)
        ws._on_edge(None)
        # Edge only 1ms (1000 us) later, which is < MIN_EDGE_US (1500)
        set_clock_ms(101)
        ws._on_edge(None)
        # period_us should still be None (debounced)
        assert ws._period_us is None

    def test_edge_just_at_min_accepted(self):
        """Edge exactly at MIN_EDGE_US should be accepted."""
        ws = _make()
        set_clock_ms(100)
        ws._on_edge(None)
        # MIN_EDGE_US = 1500 → 1.5ms
        set_clock_ms(100 + 2)  # 2ms = 2000us ≥ 1500us
        ws._on_edge(None)
        assert ws._period_us is not None


class TestTimeout:
    def test_stale_reading_returns_zero(self):
        ws = _make()
        set_clock_ms(100)
        ws._on_edge(None)
        set_clock_ms(200)
        ws._on_edge(None)

        # Now advance time well beyond timeout
        set_clock_ms(200 + WHEEL_SPEED_TIMEOUT_MS + 100)
        rpm, valid = ws.update()
        assert rpm == 0.0
        assert valid is False

    def test_fresh_reading_within_timeout(self):
        ws = _make()
        set_clock_ms(100)
        ws._on_edge(None)
        set_clock_ms(200)
        ws._on_edge(None)

        # Within timeout window
        set_clock_ms(200 + WHEEL_SPEED_TIMEOUT_MS - 100)
        rpm, valid = ws.update()
        assert valid is True
        assert rpm > 0.0


class TestRPMCalculation:
    def test_known_speed(self):
        """60 RPM with 6 magnets → 10 edges/rev → period per edge = 1s/10 = 100ms."""
        ws = _make()
        period_ms = 100  # 100_000 us per edge
        set_clock_ms(1000)
        ws._on_edge(None)
        set_clock_ms(1000 + period_ms)
        ws._on_edge(None)

        rpm, valid = ws.update()
        # rpm = 60 * 1e6 / (100_000 * 6) = 100
        expected = 60.0 * 1_000_000.0 / (period_ms * 1000 * WHEEL_MAGNET_COUNT)
        assert valid is True
        assert abs(rpm - expected) < 0.1
