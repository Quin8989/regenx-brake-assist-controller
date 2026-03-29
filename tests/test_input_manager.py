# tests/test_input_manager.py — InputManager mode arbitration and state population

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
        return self._rpm, self._valid


def _make(throttle=None, wheel=None):
    state = SharedState()
    if throttle is None:
        throttle = _FakeThrottle()
    im = InputManager(throttle, state, wheel)
    return state, throttle, im


# ── Mode arbitration (carrier-lock inference) ────────────────────────────

class TestModeArbitration:
    def test_assist_when_throttle_active(self):
        s, t, im = _make()
        t.fraction = 0.6
        t.is_valid = True
        im.update()
        assert s.requested_mode == CommandMode.ASSIST
        assert abs(s.requested_level - 0.6) < 0.01

    def test_neutral_when_coasting(self):
        """Throttle off + motor decoupled (≈ 0 RPM) → NEUTRAL (true coast)."""
        wheel = _FakeWheel(rpm=100.0, valid=True)
        s, t, im = _make(wheel=wheel)
        t.fraction = 0.0
        t.is_valid = True
        s.vesc_mech_rpm = 5.0  # motor nearly stopped → freewheel disengaged
        im.update()
        assert s.requested_mode == CommandMode.NEUTRAL

    def test_regen_when_carrier_locked(self):
        """Throttle off + motor coupled (carrier locked by brake) → REGEN."""
        wheel = _FakeWheel(rpm=100.0, valid=True)
        s, t, im = _make(wheel=wheel)
        t.fraction = 0.0
        t.is_valid = True
        # Locked motor RPM = 100 × 3 = 300; motor at 288 → 4% slip
        s.vesc_mech_rpm = 288.0
        im.update()
        assert s.requested_mode == CommandMode.REGEN
        assert s.requested_level == 1.0

    def test_regen_when_carrier_locked_negative_motor_rpm(self):
        """Negative motor RPM sign should still count as coupled lock."""
        wheel = _FakeWheel(rpm=100.0, valid=True)
        s, t, im = _make(wheel=wheel)
        t.fraction = 0.0
        t.is_valid = True
        s.vesc_mech_rpm = -288.0
        im.update()
        assert s.requested_mode == CommandMode.REGEN

    def test_assist_overrides_brake(self):
        """Throttle active + carrier locked → ASSIST (throttle always wins)."""
        wheel = _FakeWheel(rpm=100.0, valid=True)
        s, t, im = _make(wheel=wheel)
        t.fraction = 0.8
        t.is_valid = True
        s.vesc_mech_rpm = 288.0
        im.update()
        assert s.requested_mode == CommandMode.ASSIST

    def test_neutral_when_wheel_below_min_rpm(self):
        """Even if carrier appears locked, wheel too slow → NEUTRAL."""
        wheel = _FakeWheel(rpm=10.0, valid=True)
        s, t, im = _make(wheel=wheel)
        t.fraction = 0.0
        t.is_valid = True
        s.vesc_mech_rpm = 29.0  # low slip, but wheel below min RPM gate
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

    def test_partial_coupling_below_engage_threshold(self):
        """Motor at 60% of locked speed → 40% slip → above 30% engage → NEUTRAL."""
        wheel = _FakeWheel(rpm=100.0, valid=True)
        s, t, im = _make(wheel=wheel)
        t.fraction = 0.0
        t.is_valid = True
        s.vesc_mech_rpm = 180.0  # 180/300 = 60% → 40% slip
        im.update()
        assert s.requested_mode == CommandMode.NEUTRAL

    def test_coupling_at_engage_boundary(self):
        """Motor at 70% of locked speed → 30% slip → at boundary → NEUTRAL (not <)."""
        wheel = _FakeWheel(rpm=100.0, valid=True)
        s, t, im = _make(wheel=wheel)
        t.fraction = 0.0
        t.is_valid = True
        s.vesc_mech_rpm = 210.0  # 210/300 = 70% → exactly 30% slip
        im.update()
        assert s.requested_mode == CommandMode.NEUTRAL

    def test_coupling_just_below_engage(self):
        """Motor at 71% of locked speed → 29% slip → below 30% → REGEN."""
        wheel = _FakeWheel(rpm=100.0, valid=True)
        s, t, im = _make(wheel=wheel)
        t.fraction = 0.0
        t.is_valid = True
        s.vesc_mech_rpm = 213.0  # 213/300 = 71% → 29% slip
        im.update()
        assert s.requested_mode == CommandMode.REGEN


# ── Hysteresis tests ─────────────────────────────────────────────────────

class TestHysteresis:
    def test_stays_in_regen_between_thresholds(self):
        """Once in REGEN (slip < 30%), stays until slip > 50%."""
        wheel = _FakeWheel(rpm=100.0, valid=True)
        s, t, im = _make(wheel=wheel)
        t.fraction = 0.0
        t.is_valid = True

        # Enter REGEN: 20% slip
        s.vesc_mech_rpm = 240.0  # 240/300 = 80% lock → 20% slip
        im.update()
        assert s.requested_mode == CommandMode.REGEN

        # Slip increases to 40% — between engage (30%) and disengage (50%)
        s.vesc_mech_rpm = 180.0  # 180/300 = 60% lock → 40% slip
        im.update()
        assert s.requested_mode == CommandMode.REGEN  # hysteresis holds

    def test_exits_regen_above_disengage(self):
        """Exits REGEN when carrier slip exceeds disengage threshold."""
        wheel = _FakeWheel(rpm=100.0, valid=True)
        s, t, im = _make(wheel=wheel)
        t.fraction = 0.0
        t.is_valid = True

        # Enter REGEN
        s.vesc_mech_rpm = 240.0  # 20% slip
        im.update()
        assert s.requested_mode == CommandMode.REGEN

        # Slip exceeds 50% → exit
        s.vesc_mech_rpm = 120.0  # 120/300 = 40% lock → 60% slip
        im.update()
        assert s.requested_mode == CommandMode.NEUTRAL

    def test_throttle_clears_regen_flag(self):
        """Applying throttle during REGEN clears the hysteresis flag."""
        wheel = _FakeWheel(rpm=100.0, valid=True)
        s, t, im = _make(wheel=wheel)
        t.fraction = 0.0
        t.is_valid = True

        # Enter REGEN
        s.vesc_mech_rpm = 240.0
        im.update()
        assert s.requested_mode == CommandMode.REGEN

        # Apply throttle → ASSIST, clears _in_regen
        t.fraction = 0.5
        im.update()
        assert s.requested_mode == CommandMode.ASSIST

        # Release throttle — motor still at 40% slip (between thresholds)
        # Without hysteresis flag, should NOT re-enter REGEN
        t.fraction = 0.0
        s.vesc_mech_rpm = 180.0  # 40% slip — above 30% engage threshold
        im.update()
        assert s.requested_mode == CommandMode.NEUTRAL

    def test_exits_at_exactly_disengage_threshold(self):
        """Slip at exactly 50% → exits REGEN (not strictly less than threshold)."""
        wheel = _FakeWheel(rpm=100.0, valid=True)
        s, t, im = _make(wheel=wheel)
        t.fraction = 0.0
        t.is_valid = True

        # Enter REGEN
        s.vesc_mech_rpm = 240.0  # 20% slip
        im.update()
        assert s.requested_mode == CommandMode.REGEN

        # Slip at exactly 50%
        s.vesc_mech_rpm = 150.0  # 150/300 = 50% lock → 50% slip → not < 50%
        im.update()
        assert s.requested_mode == CommandMode.NEUTRAL

    def test_stays_just_below_disengage(self):
        """Slip at 49% → stays in REGEN."""
        wheel = _FakeWheel(rpm=100.0, valid=True)
        s, t, im = _make(wheel=wheel)
        t.fraction = 0.0
        t.is_valid = True

        # Enter REGEN
        s.vesc_mech_rpm = 240.0  # 20% slip
        im.update()
        assert s.requested_mode == CommandMode.REGEN

        # Slip just below disengage: 49%
        s.vesc_mech_rpm = 153.0  # 153/300 = 51% lock → 49% slip → stays
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
