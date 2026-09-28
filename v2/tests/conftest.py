import os
import sys
import struct

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "firmware"))

import pytest  # noqa: E402
import vesc    # noqa: E402
import config  # noqa: E402


class FakeUART:
    """Duck-typed UART: captures writes, serves scripted RX bytes."""

    def __init__(self):
        self.tx = bytearray()
        self._rx = bytearray()

    def write(self, mv):
        self.tx += bytes(mv)

    def inject(self, data):
        self._rx += data

    def any(self):
        return len(self._rx)

    def readinto(self, mv):
        n = min(len(mv), len(self._rx))
        mv[:n] = self._rx[:n]
        del self._rx[:n]
        return n or None


class FakeSensors:
    def __init__(self):
        self.rpm = 0.0
        self.thr = 0.0
        self.thr_failed = False
        self.brk = False
        self.v = 5.0

    def wheel_rpm(self, now_ms):
        return self.rpm

    def throttle(self, now_ms):
        return self.thr, self.thr_failed

    def brake(self):
        return self.brk

    def vsys(self):
        return self.v


def make_frame(payload):
    out = bytearray(len(payload) + 5)
    out[2:2 + len(payload)] = payload
    n = vesc.wrap(out, len(payload))
    return bytes(out[:n])


def selective_payload(mask, erpm=0.0, v_in=14.0, i_in=0.0, i_motor=0.0,
                      fault=0, temp_fet=25.0):
    """Build a COMM_GET_VALUES_SELECTIVE response payload per the bit table."""
    out = bytearray()
    out.append(vesc.COMM_GET_VALUES_SELECTIVE)
    out += struct.pack(">I", mask)
    values = {
        0: ("h", int(temp_fet * 10)),
        2: ("i", int(i_motor * 100)),
        3: ("i", int(i_in * 100)),
        7: ("i", int(erpm)),
        8: ("h", int(v_in * 10)),
        15: ("B", fault),
    }
    for bit, fmt, div, attr in vesc._SEL_TABLE:
        if mask & (1 << bit):
            fmt_c, raw = values.get(bit, (fmt, 0))
            out += struct.pack(">" + fmt, raw)
    return bytes(out)


def fw_payload(major=5, minor=2):
    return bytes([vesc.COMM_FW_VERSION, major, minor]) + b"FSESC4.20\x00"


@pytest.fixture
def uart():
    return FakeUART()


@pytest.fixture
def link(uart):
    return vesc.VescLink(uart, config)


@pytest.fixture
def sensors_fake():
    return FakeSensors()
