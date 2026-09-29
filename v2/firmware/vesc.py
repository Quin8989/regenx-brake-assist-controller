# vesc.py — VESC UART link: framing, CRC16, parser, per-tick schedule.
#
# Pure over a duck-typed uart (.any(), .readinto(mv), .write(buf)), so it runs under
# CPython for tests. RGX-2-003 D5/D7/D11.
#
# Health is one number: `ok`, the count of consecutive clean telemetry frames.
# Silence, a VESC fault or a dead link all reset it, and control only commands
# current while ok >= LINK_RECOVER_FRAMES. No separate state machine needed.

import struct

import config as C

COMM_SET_CURRENT = 6
COMM_GET_VALUES_SELECTIVE = 50
MASK = 0x818D                 # temp_fet, i_motor, i_in, erpm, v_in, fault
_LEN = 22                     # reply payload: command, mask echo, the 6 values
_FRAME = _LEN + 5             # start, length, payload, CRC (2), end


def _make_table():
    tab = []
    for i in range(256):
        c = i << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) if (c & 0x8000) else (c << 1)
        tab.append(c & 0xFFFF)
    return tab


_TAB = _make_table()


def crc16(buf, start, n):
    """CRC16-XMODEM (poly 0x1021, init 0) over buf[start:start+n]."""
    crc = 0
    for i in range(start, start + n):
        crc = ((crc << 8) & 0xFFFF) ^ _TAB[((crc >> 8) ^ buf[i]) & 0xFF]
    return crc


def frame(payload):
    """Complete short frame around payload. Init-time only (allocates)."""
    n = len(payload)
    b = bytearray(n + 5)
    b[0] = 2
    b[1] = n
    b[2:2 + n] = payload
    _seal(b, n)
    return b


def _seal(b, n):
    c = crc16(b, 2, n)
    b[n + 2] = c >> 8
    b[n + 3] = c & 0xFF
    b[n + 4] = 3


class Link:
    def __init__(self, uart):
        self.uart = uart
        self._cmd = frame(bytes((COMM_SET_CURRENT, 0, 0, 0, 0)))
        self._req = frame(struct.pack(">BI", COMM_GET_VALUES_SELECTIVE, MASK))
        self._buf = bytearray(256)
        self._mv = memoryview(self._buf)
        self._n = 0
        self.erpm = self.v_in = self.i_in = self.i_motor = self.temp_fet = 0.0
        self.fault = 0
        self.ok = 0               # consecutive clean telemetry frames
        self.silent = 0           # ticks since the last telemetry frame
        self.bad = 0              # frames that failed their checksum

    def send(self, amps):
        """End of tick: the current command (also the keepalive A1's timeout
        watches), then the telemetry request the next tick's poll() reads."""
        self.silent += 1
        if self.silent > C.LINK_TIMEOUT_TICKS:
            self.ok = 0
        struct.pack_into(">i", self._cmd, 3, int(amps * 1000))
        _seal(self._cmd, 5)
        self.uart.write(self._cmd)
        self.uart.write(self._req)

    def poll(self):
        """Start of tick: drain RX and decode every complete reply.

        Every reply has the same length, so a candidate start that is still
        waiting for bytes is never followed by a complete frame: scanning can
        stop there and keep the tail. Anything that is not a whole, valid
        reply is skipped one byte at a time.
        """
        b = self._buf
        n0 = self._n
        k = self.uart.any()             # read only what is there: rp2 readinto
        if not k:                       # waits out the char timeout otherwise
            return
        got = self.uart.readinto(self._mv[n0:n0 + min(k, len(b) - n0)])
        if not got:
            return
        end = n0 + got
        i = 0
        while end - i >= _FRAME:
            if b[i] == 2 and b[i + 1] == _LEN:
                if (b[i + 2] == COMM_GET_VALUES_SELECTIVE and b[i + _FRAME - 1] == 3
                        and crc16(b, i + 2, _LEN) == (b[i + _LEN + 2] << 8 | b[i + _LEN + 3])):
                    self._decode(i + 7)
                    i += _FRAME
                    continue
                self.bad += 1
            i += 1
        for j in range(end - i):            # keep the unconsumed tail
            b[j] = b[i + j]
        self._n = end - i

    def _decode(self, p):
        t, im, ii, e, v, f = struct.unpack_from(">hiiihB", self._buf, p)
        self.temp_fet = t / 10
        self.i_motor = im / 100
        self.i_in = ii / 100
        self.erpm = float(e)
        self.v_in = v / 10
        self.fault = f
        self.silent = 0
        self.ok = self.ok + 1 if f == 0 else 0
