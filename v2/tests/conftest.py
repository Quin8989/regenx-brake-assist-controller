import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "firmware"))

import pytest  # noqa: E402

import config as C  # noqa: E402
import control  # noqa: E402
import strategy  # noqa: E402
import vesc  # noqa: E402


class FakeUART:
    """Captures writes, serves injected RX bytes through readinto()."""

    def __init__(self):
        self.tx = bytearray()
        self._rx = bytearray()

    def write(self, b):
        self.tx += bytes(b)

    def inject(self, data):
        self._rx += data

    def any(self):
        return len(self._rx)

    def readinto(self, mv):
        n = min(len(mv), len(self._rx))
        if not n:
            return None
        mv[:n] = self._rx[:n]
        del self._rx[:n]
        return n


class _Value:
    def __init__(self):
        self.value = 0.0

    def read(self):
        return self.value


class FakeSensors:
    def __init__(self):
        self.wheel = _Value()
        self.throttle = _Value()

    def vsys(self):
        return 5.0


# --- what A1 sends ----------------------------------------------------------
def telem(erpm=0.0, v_in=25.0, i_in=0.0, i_motor=0.0, fault=0, temp=None):
    """A COMM_GET_VALUES_SELECTIVE reply frame."""
    im, ii, e, v = int(i_motor * 100), int(i_in * 100), int(erpm), int(v_in * 10)
    if temp is None:
        p = struct.pack(">BIiiihB", 50, vesc.MASK, im, ii, e, v, fault)
    else:
        p = struct.pack(">BIhiiihB", 50, vesc.MASK_T, int(temp * 10), im, ii, e, v, fault)
    return bytes(vesc.frame(p))


def fw_reply(major=6, minor=6):
    return bytes(vesc.frame(bytes((0, major, minor)) + b"FSESC4.20\x00"))


def rotor_erpm(wheel_rpm, s):
    """The plant: A1's ERPM for a wheel speed and carrier slip (0 held, 1 free).

    With A1 provisioned so +current drives the wheel forward, the rotor turns
    at k x wheel x (1 - s): parked while coasting, k x wheel when the carrier
    is held by the clutch (assist) or the brake (regen).
    """
    return C.POLE_PAIRS * C.K_RATIO * wheel_rpm * (1.0 - s)


def last_current(tx):
    """Amps in the last SET_CURRENT frame on the wire."""
    i = tx.rfind(bytes((2, 5, vesc.COMM_SET_CURRENT)))
    assert i >= 0, "no SET_CURRENT on the wire"
    return struct.unpack_from(">i", tx, i + 3)[0] / 1000


class Rig:
    """Control loop + link + fakes, stepped one tick at a time."""

    def __init__(self, strat=None):
        self.uart = FakeUART()
        self.link = vesc.Link(self.uart)
        self.sensors = FakeSensors()
        self.loop = control.Control(self.link, self.sensors, strat or strategy.SlipRegulator())

    def step(self, wheel=0.0, s=1.0, thr=0.0, reply=True, **kw):
        """One tick. The plant sets ERPM from wheel speed and slip unless
        erpm= is given; reply=False simulates a lost telemetry reply."""
        self.sensors.wheel.value = wheel
        self.sensors.throttle.value = thr
        if reply:
            kw.setdefault("erpm", rotor_erpm(wheel, s))
            self.uart.inject(telem(**kw))
        return self.loop.tick()

    def boot(self, **kw):
        for _ in range(C.LINK_RECOVER_FRAMES):
            self.step(**kw)
        assert self.state == control.RUN

    @property
    def state(self):
        return int(self.loop.sn[control.SN_STATE])


@pytest.fixture
def rig():
    return Rig()
