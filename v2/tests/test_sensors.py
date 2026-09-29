import pytest

import config as C
from sensors import Throttle, Wheel


class FakeADC:
    def __init__(self, frac):
        self.frac = frac

    def read_u16(self):
        return int(self.frac * 65535)


class FakeSM:
    """PIO state machine stand-in: phase lengths (us) waiting in the RX FIFO."""

    def __init__(self):
        self.fifo = []

    def rx_fifo(self):
        return len(self.fifo)

    def get(self):
        return self.fifo.pop(0)


SPAN = C.THR_FULL - C.THR_IDLE


# --- throttle (review F05, F18, F03) -------------------------------------------------
def test_idle_reads_zero_and_arms():
    t = Throttle(FakeADC(C.THR_IDLE))
    assert t.read() == 0.0 and t.armed


def test_held_throttle_at_power_on_gives_nothing_until_idle():
    adc = FakeADC(C.THR_IDLE + 0.6 * SPAN)
    t = Throttle(adc)
    assert t.read() == 0.0
    adc.frac = C.THR_IDLE
    t.read()
    adc.frac = C.THR_IDLE + 0.6 * SPAN
    assert t.read() > 0.5


def test_full_scale_and_deadband():
    adc = FakeADC(C.THR_IDLE)
    t = Throttle(adc)
    t.read()
    adc.frac = C.THR_IDLE + 0.04 * SPAN          # inside the deadband
    assert t.read() == 0.0
    adc.frac = C.THR_FULL
    assert t.read() == pytest.approx(1.0, abs=1e-3)
    adc.frac = C.THR_HI - 0.01                  # past full, still valid
    assert t.read() == 1.0


@pytest.mark.parametrize("frac", [0.0, C.THR_LO - 0.01, C.THR_HI + 0.01, 1.0])
def test_open_or_shorted_reads_zero_and_disarms(frac):
    adc = FakeADC(C.THR_IDLE)
    t = Throttle(adc)
    t.read()
    adc.frac = frac
    assert t.read() == 0.0
    adc.frac = C.THR_IDLE + 0.6 * SPAN          # wire back, throttle still held
    assert t.read() == 0.0                      # nothing until released
    adc.frac = C.THR_IDLE
    t.read()
    adc.frac = C.THR_IDLE + 0.6 * SPAN
    assert t.read() > 0.5


# --- wheel speed (review F10, F30, F31, F54, F59) ------------------------------------
def rpm_for(kmh):
    return kmh / 3.6 / C.WHEEL_CIRC_M * 60


def ride(w, sm, kmh, seconds):
    """Deliver phases at the right ticks for a steady speed; return rpm trace."""
    half = C.SPD_K / rpm_for(kmh) / 2          # us per phase at 50 % duty
    t_us, next_edge, out = 0, half, []
    for _ in range(int(seconds * 100)):
        t_us += C.TICK_MS * 1000
        while next_edge <= t_us:
            sm.fifo.append(int(half))
            next_edge += half
        out.append(w.read())
    return out


@pytest.mark.parametrize("kmh", [3.5, 10.0, 20.0, 45.0])
def test_steady_speed_is_exact_and_never_capped(kmh):
    sm = FakeSM()
    w = Wheel(sm)
    trace = ride(w, sm, kmh, 3.0)
    settled = trace[len(trace) // 2:]
    assert min(settled) == pytest.approx(rpm_for(kmh), rel=1e-3)
    assert max(settled) == pytest.approx(rpm_for(kmh), rel=1e-3)


@pytest.mark.parametrize("word", [40, 0])   # a us-wide spike; PIO counter exhaustion
def test_short_words_are_rejected_not_paired(word):
    # Pairing a 40 us word would read ~800x the true speed. The partial phases
    # either side of a real glitch can still read up to ~2x for one or two
    # samples; the fix for that is C6 = 100 nF (100 us RC), not more code.
    sm = FakeSM()
    w = Wheel(sm)
    half = int(C.SPD_K / rpm_for(20) / 2)
    sm.fifo += [half, half]
    ok = w.read()
    sm.fifo += [word]
    assert w.read() <= ok
    sm.fifo += [half, half]
    assert w.read() == pytest.approx(ok)


def test_speed_decays_when_pulses_stop_and_reads_zero():
    sm = FakeSM()
    w = Wheel(sm)
    ride(w, sm, 20.0, 1.0)
    v0 = w.read()
    trace = [w.read() for _ in range(150)]
    assert trace[20] < v0                                   # decaying, not held
    assert all(a >= b for a, b in zip(trace, trace[1:]))
    assert trace[-1] == 0.0
