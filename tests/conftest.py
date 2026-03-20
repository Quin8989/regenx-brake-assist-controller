# tests/conftest.py — Mock MicroPython hardware modules for CPython test runs
#
# This must run before any project imports.  pytest loads conftest.py first,
# so we inject fake 'machine' and patch 'time' functions here.

import sys
import types

# ---------------------------------------------------------------------------
# Fake 'machine' module
# ---------------------------------------------------------------------------

_machine = types.ModuleType("machine")


class _FakePin:
    IN = "IN"
    OUT = "OUT"
    PULL_UP = "PULL_UP"
    PULL_DOWN = "PULL_DOWN"
    IRQ_RISING = 1
    IRQ_FALLING = 2

    def __init__(self, pin_id, mode=None, pull=None, value=0):
        self._id = pin_id
        self._value = value

    def value(self, v=None):
        if v is not None:
            self._value = v
        return self._value

    def irq(self, handler=None, trigger=None):
        pass

    def init(self, *a, **kw):
        pass


class _FakeADC:
    def __init__(self, pin):
        self._pin = pin
        self._value = 0

    def read_u16(self):
        return self._value


class _FakeUART:
    def __init__(self, *a, **kw):
        self._tx_buf = bytearray()
        self._rx_buf = bytearray()

    def write(self, data):
        self._tx_buf.extend(data)

    def read(self, n=-1):
        if not self._rx_buf:
            return None
        if n < 0:
            out = bytes(self._rx_buf)
            self._rx_buf.clear()
            return out
        out = bytes(self._rx_buf[:n])
        self._rx_buf = self._rx_buf[n:]
        return out

    def any(self):
        return len(self._rx_buf)


class _FakeI2C:
    def __init__(self, *a, **kw):
        self._writes = []

    def writeto(self, addr, data):
        self._writes.append((addr, bytes(data)))

    def scan(self):
        return []


class _FakeWDT:
    def __init__(self, timeout=0):
        self.timeout = timeout
        self._fed = 0

    def feed(self):
        self._fed += 1


_machine.Pin = _FakePin
_machine.ADC = _FakeADC
_machine.UART = _FakeUART
_machine.I2C = _FakeI2C
_machine.WDT = _FakeWDT
sys.modules["machine"] = _machine

# ---------------------------------------------------------------------------
# time — provide ticks_ms / ticks_us / ticks_diff / sleep_ms / sleep_us
# ---------------------------------------------------------------------------
# We keep a controllable clock so tests can advance time deterministically.

_tick_ms = 0


def _ticks_ms():
    return _tick_ms


def _ticks_us():
    return _tick_ms * 1000


def _ticks_diff(a, b):
    return a - b


def _sleep_ms(ms):
    pass


def _sleep_us(us):
    pass


import time as _time_mod  # noqa: E402

_time_mod.ticks_ms = _ticks_ms
_time_mod.ticks_us = _ticks_us
_time_mod.ticks_diff = _ticks_diff
_time_mod.sleep_ms = _sleep_ms
_time_mod.sleep_us = _sleep_us

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def reset_clock():
    """Reset the fake clock to 0 before each test."""
    global _tick_ms
    _tick_ms = 0
    yield


def advance_ms(ms):
    """Advance the fake monotonic clock by 'ms' milliseconds."""
    global _tick_ms
    _tick_ms += ms


def set_clock_ms(ms):
    """Set the fake clock to an absolute value."""
    global _tick_ms
    _tick_ms = ms
