# tests/test_input_manager.py — InputManager mode arbitration and state propagation

import pytest
from tests.conftest import advance_ms, set_clock_ms

from config.settings import REGEN_ENTRY_RPM, REGEN_EXIT_RPM, REGEN_HOLDOFF_MS
from core import CommandMode, SharedState
from services.input_manager import InputManager


class _FakeThrottle:
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


def test_assist_when_throttle_active():
    s, t, im = _make()
    t.fraction = 0.6
    im.update()
    assert s.requested_mode == CommandMode.ASSIST
    assert abs(s.requested_level - 0.6) < 0.01


def test_assist_overrides_regen():
    """Throttle active + motor spinning → ASSIST (throttle always wins)."""
    s, t, im = _make()
    t.fraction = 0.8
    s.vesc_mech_rpm = REGEN_ENTRY_RPM + 50.0
    im.update()
    assert s.requested_mode == CommandMode.ASSIST


def test_regen_lifecycle():
    """Holdoff → idle → regen active → hysteresis exit lifecycle."""
    s, t, im = _make()
    t.fraction = 0.0
    s.vesc_mech_rpm = REGEN_ENTRY_RPM + 10.0
    set_clock_ms(0)

    # During holdoff: idle (level 0)
    im.update()
    advance_ms(REGEN_HOLDOFF_MS - 1)
    im.update()
    assert s.requested_mode == CommandMode.REGEN
    assert s.requested_level == 0.0

    # After holdoff: active regen
    advance_ms(2)
    im.update()
    assert s.requested_mode == CommandMode.REGEN
    assert s.requested_level == 1.0

    # Drop below entry but above exit → stays in REGEN (hysteresis)
    s.vesc_mech_rpm = (REGEN_ENTRY_RPM + REGEN_EXIT_RPM) / 2.0
    advance_ms(10)
    im.update()
    assert s.requested_mode == CommandMode.REGEN
    assert s.requested_level == 1.0

    # Drop below exit → exits active regen
    s.vesc_mech_rpm = REGEN_EXIT_RPM - 1.0
    advance_ms(10)
    im.update()
    assert s.requested_mode == CommandMode.REGEN
    assert s.requested_level == 0.0


@pytest.mark.parametrize("rpm,expect_active", [
    pytest.param(REGEN_ENTRY_RPM - 5.0, False, id="below_entry"),
    pytest.param(0.0, False, id="stopped"),
    pytest.param(-(REGEN_ENTRY_RPM + 10.0), False, id="negative_rpm"),
    pytest.param(REGEN_ENTRY_RPM + 10.0, True, id="above_entry"),
])
def test_regen_rpm_thresholds(rpm, expect_active):
    """After holdoff, regen is active only for positive RPM above entry."""
    s, t, im = _make()
    t.fraction = 0.0
    s.vesc_mech_rpm = rpm
    set_clock_ms(0)
    im.update()
    advance_ms(REGEN_HOLDOFF_MS + 1)
    im.update()
    assert s.requested_mode == CommandMode.REGEN
    assert (s.requested_level == 1.0) == expect_active


def test_holdoff_resets_on_throttle_blip():
    """Throttle blip during holdoff restarts the timer."""
    s, t, im = _make()
    s.vesc_mech_rpm = REGEN_ENTRY_RPM + 10.0
    t.fraction = 0.0
    set_clock_ms(0)
    im.update()
    advance_ms(REGEN_HOLDOFF_MS - 50)

    # Brief throttle → ASSIST → release
    t.fraction = 0.1
    im.update()
    t.fraction = 0.0
    advance_ms(10)
    im.update()

    # Timer restarted — not yet expired
    advance_ms(REGEN_HOLDOFF_MS - 50)
    im.update()
    assert s.requested_level == 0.0

    # Now it expires
    advance_ms(60)
    im.update()
    assert s.requested_level == 1.0


def test_throttle_state_propagation():
    s, t, im = _make()
    t.raw = 2048
    t.is_valid = False
    im.update()
    assert s.throttle_raw == 2048
    assert s.throttle_valid is False
    assert t._updated is True
