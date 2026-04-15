# tests/test_input_manager.py — InputManager mode arbitration and state population

from tests.conftest import advance_ms, set_clock_ms

from config.settings import REGEN_ENTRY_RPM, REGEN_EXIT_RPM, REGEN_HOLDOFF_MS, REGEN_SLIP_GRACE_MS
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


def _make(throttle=None):
    state = SharedState()
    if throttle is None:
        throttle = _FakeThrottle()
    im = InputManager(throttle, state)
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

    def test_regen_when_motor_spinning_after_holdoff(self):
        """Throttle off + holdoff expired + motor RPM above entry → REGEN."""
        s, t, im = _make()
        t.fraction = 0.0
        t.is_valid = True
        s.vesc_mech_rpm = REGEN_ENTRY_RPM + 10.0
        set_clock_ms(0)
        im.update()  # starts holdoff
        advance_ms(REGEN_HOLDOFF_MS + 1)
        im.update()  # holdoff expired → REGEN
        assert s.requested_mode == CommandMode.REGEN
        assert s.requested_level == 1.0

    def test_neutral_during_holdoff(self):
        """Motor spinning but still within holdoff window → NEUTRAL."""
        s, t, im = _make()
        t.fraction = 0.0
        t.is_valid = True
        s.vesc_mech_rpm = REGEN_ENTRY_RPM + 50.0
        set_clock_ms(0)
        im.update()
        advance_ms(REGEN_HOLDOFF_MS - 1)
        im.update()
        assert s.requested_mode == CommandMode.NEUTRAL

    def test_no_regen_with_negative_motor_rpm(self):
        """Negative motor RPM (backward roll) must NOT trigger REGEN."""
        s, t, im = _make()
        t.fraction = 0.0
        t.is_valid = True
        s.vesc_mech_rpm = -(REGEN_ENTRY_RPM + 10.0)
        set_clock_ms(0)
        im.update()
        advance_ms(REGEN_HOLDOFF_MS + 1)
        im.update()
        assert s.requested_mode == CommandMode.NEUTRAL

    def test_assist_overrides_regen(self):
        """Throttle active + motor spinning → ASSIST (throttle always wins)."""
        s, t, im = _make()
        t.fraction = 0.8
        t.is_valid = True
        s.vesc_mech_rpm = REGEN_ENTRY_RPM + 50.0
        im.update()
        assert s.requested_mode == CommandMode.ASSIST

    def test_neutral_when_motor_below_entry_rpm(self):
        """Motor RPM below entry threshold → NEUTRAL."""
        s, t, im = _make()
        t.fraction = 0.0
        t.is_valid = True
        s.vesc_mech_rpm = REGEN_ENTRY_RPM - 5.0
        set_clock_ms(0)
        im.update()
        advance_ms(REGEN_HOLDOFF_MS + 1)
        im.update()
        assert s.requested_mode == CommandMode.NEUTRAL

    def test_neutral_when_motor_stopped(self):
        """Motor RPM at zero (coasting on freewheel) → NEUTRAL."""
        s, t, im = _make()
        t.fraction = 0.0
        t.is_valid = True
        s.vesc_mech_rpm = 0.0
        set_clock_ms(0)
        im.update()
        advance_ms(REGEN_HOLDOFF_MS + 1)
        im.update()
        assert s.requested_mode == CommandMode.NEUTRAL

    def test_hysteresis_stays_in_regen_above_exit(self):
        """Once in REGEN, stays in REGEN until motor RPM drops below exit threshold."""
        s, t, im = _make()
        t.fraction = 0.0
        t.is_valid = True
        s.vesc_mech_rpm = REGEN_ENTRY_RPM + 10.0
        set_clock_ms(0)
        im.update()
        advance_ms(REGEN_HOLDOFF_MS + 1)
        im.update()
        assert s.requested_mode == CommandMode.REGEN

        # Drop below entry but above exit — should stay REGEN
        s.vesc_mech_rpm = (REGEN_ENTRY_RPM + REGEN_EXIT_RPM) / 2.0
        advance_ms(10)
        im.update()
        assert s.requested_mode == CommandMode.REGEN

    def test_hysteresis_exits_regen_below_exit(self):
        """Motor RPM dropping below exit threshold → NEUTRAL after grace."""
        s, t, im = _make()
        t.fraction = 0.0
        t.is_valid = True
        s.vesc_mech_rpm = REGEN_ENTRY_RPM + 10.0
        set_clock_ms(0)
        im.update()
        advance_ms(REGEN_HOLDOFF_MS + 1)
        im.update()
        assert s.requested_mode == CommandMode.REGEN

        # Drop below exit — grace period keeps REGEN active
        s.vesc_mech_rpm = REGEN_EXIT_RPM - 1.0
        advance_ms(10)
        im.update()
        assert s.requested_mode == CommandMode.REGEN

        # After grace period expires → NEUTRAL
        advance_ms(REGEN_SLIP_GRACE_MS + 1)
        im.update()
        assert s.requested_mode == CommandMode.NEUTRAL

    def test_regen_to_assist_to_regen_with_holdoff(self):
        """Transitioning through ASSIST resets holdoff timer."""
        s, t, im = _make()
        t.fraction = 0.0
        t.is_valid = True
        s.vesc_mech_rpm = REGEN_ENTRY_RPM + 10.0
        set_clock_ms(0)
        im.update()
        advance_ms(REGEN_HOLDOFF_MS + 1)
        im.update()
        assert s.requested_mode == CommandMode.REGEN

        # Apply throttle → ASSIST (resets holdoff)
        t.fraction = 0.5
        advance_ms(10)
        im.update()
        assert s.requested_mode == CommandMode.ASSIST

        # Release throttle — holdoff restarts, so first cycle is NEUTRAL
        t.fraction = 0.0
        advance_ms(10)
        im.update()
        assert s.requested_mode == CommandMode.NEUTRAL

        # After holdoff expires → back to REGEN
        advance_ms(REGEN_HOLDOFF_MS + 1)
        im.update()
        assert s.requested_mode == CommandMode.REGEN

    def test_holdoff_resets_on_throttle_reapply(self):
        """Holdoff timer resets each time throttle is applied then released."""
        s, t, im = _make()
        s.vesc_mech_rpm = REGEN_ENTRY_RPM + 10.0
        t.fraction = 0.0
        t.is_valid = True
        set_clock_ms(0)
        im.update()

        advance_ms(REGEN_HOLDOFF_MS - 50)  # almost expired
        # Brief throttle blip
        t.fraction = 0.1
        im.update()
        t.fraction = 0.0
        advance_ms(10)
        im.update()

        # Holdoff should have restarted — not yet expired
        advance_ms(REGEN_HOLDOFF_MS - 50)
        im.update()
        assert s.requested_mode == CommandMode.NEUTRAL

        # Now it expires
        advance_ms(60)
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
