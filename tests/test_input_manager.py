# tests/test_input_manager.py — InputManager mode arbitration and state population

from tests.conftest import advance_ms

from core import CommandMode, SharedState
from services.input_manager import InputManager


class _FakeThrottle:
    """Controllable throttle stub."""
    def __init__(self):
        self.raw = 0
        self.fraction = 0.0
        self.is_valid = True
        self._updated = False

    def update(self):
        self._updated = True


class _FakeWheel:
    """Controllable wheel speed driver stub."""
    def __init__(self, rpm=0.0, valid=False):
        self._rpm = rpm
        self._valid = valid

    def update(self):
        return self._rpm, self._valid, True


def _make(throttle=None, wheel=None):
    state = SharedState()
    if throttle is None:
        throttle = _FakeThrottle()
    im = InputManager(throttle, state, wheel)
    return state, throttle, im


# ── Mode arbitration ─────────────────────────────────────────────────────

class TestModeArbitration:
    def test_assist_when_throttle_active(self):
        s, t, im = _make()
        t.fraction = 0.6
        t.is_valid = True
        im.update()
        assert s.requested_mode == CommandMode.ASSIST
        assert abs(s.requested_level - 0.6) < 0.01

    def test_regen_when_throttle_off_and_wheel_moving(self):
        """Throttle off + wheel valid above min → REGEN (regardless of motor RPM)."""
        wheel = _FakeWheel(rpm=100.0, valid=True)
        s, t, im = _make(wheel=wheel)
        t.fraction = 0.0
        t.is_valid = True
        s.vesc_mech_rpm = 5.0  # motor nearly stopped — coasting
        im.update()
        assert s.requested_mode == CommandMode.REGEN
        assert s.requested_level == 1.0

    def test_regen_when_carrier_locked(self):
        """Throttle off + motor coupled (carrier locked by brake) → REGEN."""
        wheel = _FakeWheel(rpm=100.0, valid=True)
        s, t, im = _make(wheel=wheel)
        t.fraction = 0.0
        t.is_valid = True
        s.vesc_mech_rpm = 288.0
        im.update()
        assert s.requested_mode == CommandMode.REGEN
        assert s.requested_level == 1.0

    def test_regen_when_carrier_locked_negative_motor_rpm(self):
        """Negative motor RPM sign should still be REGEN."""
        wheel = _FakeWheel(rpm=100.0, valid=True)
        s, t, im = _make(wheel=wheel)
        t.fraction = 0.0
        t.is_valid = True
        s.vesc_mech_rpm = -288.0
        im.update()
        assert s.requested_mode == CommandMode.REGEN

    def test_assist_overrides_regen(self):
        """Throttle active + wheel moving → ASSIST (throttle always wins)."""
        wheel = _FakeWheel(rpm=100.0, valid=True)
        s, t, im = _make(wheel=wheel)
        t.fraction = 0.8
        t.is_valid = True
        s.vesc_mech_rpm = 288.0
        im.update()
        assert s.requested_mode == CommandMode.ASSIST

    def test_neutral_when_wheel_below_min_rpm(self):
        """Wheel too slow → NEUTRAL."""
        wheel = _FakeWheel(rpm=10.0, valid=True)
        s, t, im = _make(wheel=wheel)
        t.fraction = 0.0
        t.is_valid = True
        s.vesc_mech_rpm = 29.0
        im.update()
        assert s.requested_mode == CommandMode.NEUTRAL

    def test_neutral_when_wheel_invalid(self):
        """Wheel speed invalid → NEUTRAL regardless of motor speed."""
        wheel = _FakeWheel(rpm=100.0, valid=False)
        s, t, im = _make(wheel=wheel)
        t.fraction = 0.0
        t.is_valid = True
        s.vesc_mech_rpm = 288.0
        im.update()
        assert s.requested_mode == CommandMode.NEUTRAL

    def test_neutral_when_no_wheel_driver(self):
        s, t, im = _make()
        t.fraction = 0.0
        t.is_valid = True
        im.update()
        assert s.requested_mode == CommandMode.NEUTRAL

    def test_regen_regardless_of_carrier_slip(self):
        """Any motor RPM → REGEN as long as wheel is valid and above min."""
        wheel = _FakeWheel(rpm=100.0, valid=True)
        s, t, im = _make(wheel=wheel)
        t.fraction = 0.0
        t.is_valid = True

        # Fully decoupled (coast)
        s.vesc_mech_rpm = 0.0
        im.update()
        assert s.requested_mode == CommandMode.REGEN

        # Partially coupled
        s.vesc_mech_rpm = 180.0
        im.update()
        assert s.requested_mode == CommandMode.REGEN

        # Fully locked
        s.vesc_mech_rpm = 300.0
        im.update()
        assert s.requested_mode == CommandMode.REGEN

    def test_regen_to_assist_to_regen_no_hysteresis(self):
        """Transitioning through ASSIST and back should immediately re-enter REGEN."""
        wheel = _FakeWheel(rpm=100.0, valid=True)
        s, t, im = _make(wheel=wheel)
        t.fraction = 0.0
        t.is_valid = True

        # REGEN
        im.update()
        assert s.requested_mode == CommandMode.REGEN

        # Apply throttle → ASSIST
        t.fraction = 0.5
        im.update()
        assert s.requested_mode == CommandMode.ASSIST

        # Release throttle → back to REGEN immediately
        t.fraction = 0.0
        im.update()
        assert s.requested_mode == CommandMode.REGEN


# ── Throttle state propagation ───────────────────────────────────────────

class TestThrottleStatePropagation:
    def test_throttle_raw_populated(self):
        s, t, im = _make()
        t.raw = 2048
        t.is_valid = True
        im.update()
        assert s.throttle_raw == 2048

    def test_throttle_valid_propagated(self):
        s, t, im = _make()
        t.is_valid = False
        im.update()
        assert s.throttle_valid is False

    def test_throttle_update_called(self):
        s, t, im = _make()
        im.update()
        assert t._updated is True


# ── Wheel speed integration ──────────────────────────────────────────────

class TestWheelSpeedIntegration:
    def test_wheel_speed_populated(self):
        wheel = _FakeWheel(rpm=120.0, valid=True)
        s, t, im = _make(wheel=wheel)
        im.update()
        assert abs(s.wheel_speed_rpm - 120.0) < 0.01
        assert s.wheel_speed_valid is True

    def test_wheel_invalid_propagated(self):
        wheel = _FakeWheel(rpm=0.0, valid=False)
        s, t, im = _make(wheel=wheel)
        im.update()
        assert s.wheel_speed_valid is False

    def test_no_wheel_driver(self):
        """When no wheel driver is provided, speed stays at default 0."""
        s, t, im = _make(wheel=None)
        im.update()
        assert s.wheel_speed_rpm == 0.0
        assert s.wheel_speed_valid is False

    def test_wheel_speed_rejects_impossible_spike(self):
        wheel = _FakeWheel(rpm=120.0, valid=True)
        s, t, im = _make(wheel=wheel)
        im.update()
        first = s.wheel_speed_rpm

        # Bogus ultra-fast sample should be ignored, preserving last good speed.
        wheel._rpm = 5000.0
        advance_ms(10)
        im.update()

        assert s.wheel_speed_valid is True
        assert abs(s.wheel_speed_rpm - first) < 0.01

    def test_wheel_speed_decel_is_rate_limited(self):
        wheel = _FakeWheel(rpm=180.0, valid=True)
        s, t, im = _make(wheel=wheel)
        im.update()

        # Simulate a missed-magnet read causing a sudden low-speed sample.
        wheel._rpm = 90.0
        advance_ms(10)
        im.update()

        # Filter should move downward only slightly, not jump to 90 RPM.
        assert s.wheel_speed_valid is True
        assert s.wheel_speed_rpm > 170.0
        assert s.wheel_speed_rpm < 180.0

    def test_wheel_speed_holds_last_value_through_short_invalid_gap(self):
        wheel = _FakeWheel(rpm=120.0, valid=True)
        s, t, im = _make(wheel=wheel)
        im.update()

        wheel._valid = False
        advance_ms(500)
        im.update()

        assert s.wheel_speed_valid is True
        assert abs(s.wheel_speed_rpm - 120.0) < 0.01
