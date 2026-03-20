# tests/test_precharge_manager.py — PrechargeManager watchdog and state logic

from tests.conftest import advance_ms, set_clock_ms

from core import FaultCode, FaultManager, SharedState
from services.precharge_manager import PrechargeManager


class _FakeIO:
    """Fake precharge IO driver that records calls."""
    def __init__(self):
        self.precharge_on = False
        self.boost_on = False

    def enable_precharge(self):
        self.precharge_on = True

    def enable_boost(self):
        self.boost_on = True

    def disable_all(self):
        self.precharge_on = False
        self.boost_on = False


def _make():
    state = SharedState()
    faults = FaultManager(state)
    io = _FakeIO()
    pm = PrechargeManager(io, state, faults)
    return state, faults, io, pm


# ---- Basic ON/OFF policy ----

class TestOnOffPolicy:
    def test_precharge_on_when_voltage_low(self):
        s, f, io, pm = _make()
        s.cap_voltage_v = 5.0
        pm.update()
        assert io.precharge_on is True
        assert io.boost_on is True

    def test_precharge_off_when_voltage_ok(self):
        s, f, io, pm = _make()
        s.cap_voltage_v = 16.0
        pm.update()
        assert io.precharge_on is False
        assert io.boost_on is False

    def test_precharge_off_on_fault(self):
        s, f, io, pm = _make()
        s.cap_voltage_v = 5.0
        f.set_fault(FaultCode.INTERNAL)
        pm.update()
        assert io.precharge_on is False

    def test_precharge_off_at_exactly_threshold(self):
        s, f, io, pm = _make()
        s.cap_voltage_v = 15.0  # >= VCAP_MIN_OPERATING
        pm.update()
        assert io.precharge_on is False


# ---- Watchdog — telemetry grace ----

class TestTelemetryGrace:
    def test_no_fault_within_grace(self):
        s, f, io, pm = _make()
        s.cap_voltage_v = 5.0
        set_clock_ms(0)
        pm.update()  # start watchdog
        s.last_vesc_rx_ms = 0  # never received
        set_clock_ms(11 * 60 * 1000)  # 11 min — within 12 min grace
        pm.update()
        assert FaultCode.PRECHARGE_STALL not in s.fault_flags

    def test_fault_after_grace_exceeded(self):
        s, f, io, pm = _make()
        s.cap_voltage_v = 5.0
        set_clock_ms(0)
        pm.update()  # start watchdog
        s.last_vesc_rx_ms = 0
        set_clock_ms(13 * 60 * 1000)  # 13 min > 12 min grace
        pm.update()
        assert FaultCode.PRECHARGE_STALL in s.fault_flags

    def test_no_fault_if_telemetry_arrives(self):
        s, f, io, pm = _make()
        s.cap_voltage_v = 5.0
        set_clock_ms(0)
        pm.update()  # arm
        set_clock_ms(8 * 60 * 1000)
        s.last_vesc_rx_ms = 8 * 60 * 1000  # telemetry arrived
        s.cap_voltage_v = 8.0  # VESC booted, reporting voltage
        pm.update()
        assert FaultCode.PRECHARGE_STALL not in s.fault_flags


# ---- Watchdog — hard timeout ----

class TestHardTimeout:
    def test_no_fault_before_hard_timeout(self):
        s, f, io, pm = _make()
        s.cap_voltage_v = 5.0
        set_clock_ms(0)
        pm.update()  # arm
        # Simulate telemetry arriving so grace doesn't trip first
        set_clock_ms(8 * 60 * 1000)
        s.last_vesc_rx_ms = 8 * 60 * 1000
        s.cap_voltage_v = 10.0
        pm.update()
        set_clock_ms(34 * 60 * 1000)  # 34 min < 35 min
        s.cap_voltage_v = 14.0
        pm.update()
        assert FaultCode.PRECHARGE_STALL not in s.fault_flags

    def test_fault_at_hard_timeout(self):
        s, f, io, pm = _make()
        s.cap_voltage_v = 5.0
        set_clock_ms(0)
        pm.update()  # arm
        set_clock_ms(8 * 60 * 1000)
        s.last_vesc_rx_ms = 8 * 60 * 1000
        s.cap_voltage_v = 10.0
        pm.update()
        set_clock_ms(36 * 60 * 1000)  # 36 min > 35 min
        s.cap_voltage_v = 14.0
        pm.update()
        assert FaultCode.PRECHARGE_STALL in s.fault_flags


# ---- Watchdog — progress windows ----

class TestProgressWindows:
    def test_good_progress_resets_bad_window_count(self):
        s, f, io, pm = _make()
        s.cap_voltage_v = 5.0
        set_clock_ms(0)
        pm.update()  # arm

        # Simulate VESC boot and good voltage rise
        set_clock_ms(8 * 60 * 1000)
        s.last_vesc_rx_ms = 8 * 60 * 1000
        s.cap_voltage_v = 8.0
        pm.update()  # first tick with telemetry

        # Window 1: good progress (8→10 V over 60s)
        set_clock_ms(8 * 60 * 1000 + 60_000)
        s.cap_voltage_v = 10.0
        pm.update()
        assert FaultCode.PRECHARGE_STALL not in s.fault_flags

    def test_stalled_progress_trips_fault(self):
        s, f, io, pm = _make()
        s.cap_voltage_v = 5.0
        set_clock_ms(0)
        pm.update()  # arm

        set_clock_ms(8 * 60 * 1000)
        s.last_vesc_rx_ms = 8 * 60 * 1000
        s.cap_voltage_v = 10.0
        pm.update()

        # 3 consecutive bad windows (no progress)
        for i in range(1, 4):
            set_clock_ms(8 * 60 * 1000 + i * 60_000)
            s.cap_voltage_v = 10.0  # no change
            pm.update()

        assert FaultCode.PRECHARGE_STALL in s.fault_flags

    def test_recovery_resets_bad_count(self):
        s, f, io, pm = _make()
        s.cap_voltage_v = 5.0
        set_clock_ms(0)
        pm.update()

        set_clock_ms(8 * 60 * 1000)
        s.last_vesc_rx_ms = 8 * 60 * 1000
        s.cap_voltage_v = 10.0
        pm.update()

        # 2 bad windows (not yet 3)
        for i in range(1, 3):
            set_clock_ms(8 * 60 * 1000 + i * 60_000)
            s.cap_voltage_v = 10.0
            pm.update()
        assert FaultCode.PRECHARGE_STALL not in s.fault_flags

        # Good window — should reset counter
        set_clock_ms(8 * 60 * 1000 + 3 * 60_000)
        s.cap_voltage_v = 12.0  # good progress
        pm.update()
        assert FaultCode.PRECHARGE_STALL not in s.fault_flags

        # 2 more bad — total would be 4 without reset, but counter was reset
        for i in range(4, 6):
            set_clock_ms(8 * 60 * 1000 + i * 60_000)
            s.cap_voltage_v = 12.0
            pm.update()
        assert FaultCode.PRECHARGE_STALL not in s.fault_flags


# ---- Watchdog reset ----

class TestWatchdogReset:
    def test_watchdog_resets_when_voltage_reached(self):
        s, f, io, pm = _make()
        s.cap_voltage_v = 5.0
        set_clock_ms(0)
        pm.update()  # arm

        # Voltage reaches threshold
        s.cap_voltage_v = 16.0
        pm.update()

        # Drop back below — should re-arm, not carry stale state
        s.cap_voltage_v = 5.0
        set_clock_ms(1000)
        pm.update()  # re-arms from scratch

        # Should not fault immediately
        set_clock_ms(2000)
        pm.update()
        assert FaultCode.PRECHARGE_STALL not in s.fault_flags


# ---- Non-zero starting voltage ----

class TestNonZeroStart:
    def test_cap_starts_above_vesc_boot(self):
        """If cap already has residual charge above 6V, VESC boots immediately."""
        s, f, io, pm = _make()
        s.cap_voltage_v = 8.0
        s.last_vesc_rx_ms = 100  # telemetry already arriving
        set_clock_ms(100)
        pm.update()  # arm
        # Progress window after 60s — good progress 8→10V
        set_clock_ms(60_100)
        s.cap_voltage_v = 10.0
        pm.update()
        assert FaultCode.PRECHARGE_STALL not in s.fault_flags

    def test_idle_when_fault_disables_outputs(self):
        s, f, io, pm = _make()
        s.cap_voltage_v = 5.0
        pm.update()
        assert io.precharge_on is True

        # Watchdog trips → outputs should disable
        set_clock_ms(0)
        pm.update()  # arm
        s.last_vesc_rx_ms = 0
        set_clock_ms(13 * 60 * 1000)
        pm.update()
        assert FaultCode.PRECHARGE_STALL in s.fault_flags
        assert io.precharge_on is False
