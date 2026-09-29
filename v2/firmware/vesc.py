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

COMM_FW_VERSION = 0
COMM_SET_CURRENT = 6
COMM_GET_VALUES_SELECTIVE = 50
MASK = 0x818C                 # i_motor, i_in, erpm, v_in, fault
MASK_T = 0x818D               # + temp_fet
_MAX_LEN = 80                 # longest reply we request is < 80 B; a start byte
                              # followed by a larger LEN is junk, not a frame


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


try:  # on target: viper version, same semantics (gate FW-1)
    from vesc_fast import crc16  # noqa: F811
except ImportError:
    pass


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
        self._cmd = frame(bytes(5))
        self._cmd[2] = COMM_SET_CURRENT
        self._req = frame(struct.pack(">BI", COMM_GET_VALUES_SELECTIVE, MASK))
        self._req_t = frame(struct.pack(">BI", COMM_GET_VALUES_SELECTIVE, MASK_T))
        self._req_fw = frame(bytes((COMM_FW_VERSION,)))
        self._buf = bytearray(256)
        self._mv = memoryview(self._buf)
        self._n = 0
        self._t = 0
        self.erpm = self.v_in = self.i_in = self.i_motor = self.temp_fet = 0.0
        self.fault = 0
        self.fw = 0.0             # major + minor/100 once known, e.g. 6.06
        self.ok = 0               # consecutive clean telemetry frames
        self.silent = 0           # ticks since the last telemetry frame
        self.frames = 0           # health counters for the display / log
        self.bad = 0

    def send(self, amps):
        """End of tick: current command (the keepalive), then any requests."""
        self._t += 1
        self.silent += 1
        if self.silent > C.LINK_TIMEOUT_TICKS:
            self.ok = 0
        w = self.uart.write
        if C.SEND_CURRENT:
            struct.pack_into(">i", self._cmd, 3, int(amps * 1000))
            _seal(self._cmd, 5)
            w(self._cmd)
        t = self._t
        if t % C.TELEM_DIV == 0:
            w(self._req_t if t % (C.TELEM_DIV * C.TEMP_DIV) == 0 else self._req)
        if not self.fw and t % C.FW_REQ_DIV == 1:
            w(self._req_fw)

    def poll(self):
        """Start of tick: drain RX and decode every complete frame."""
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
        keep = end                          # first start still waiting for bytes
        while end - i >= 6:
            n = b[i + 1]
            if b[i] == 2 and 0 < n <= _MAX_LEN:
                if i + n + 5 > end:         # incomplete: keep it, but look on,
                    if keep == end:         # a complete frame further in would
                        keep = i            # prove this start false
                elif b[i + n + 4] == 3 and crc16(b, i + 2, n) == (b[i + n + 2] << 8 | b[i + n + 3]):
                    self._decode(i + 2, n)
                    i += n + 5
                    keep = end
                    continue
                elif keep == end:           # (counted once, not on every rescan)
                    self.bad += 1
            i += 1
        if keep < i:
            i = keep
        for j in range(end - i):            # keep the unconsumed tail
            b[j] = b[i + j]
        self._n = end - i

    def _decode(self, p, n):
        b = self._buf
        op = b[p]
        if op == COMM_GET_VALUES_SELECTIVE and n == 20:
            im, ii, e, v, f = struct.unpack_from(">iiihB", b, p + 5)
        elif op == COMM_GET_VALUES_SELECTIVE and n == 22:
            t, im, ii, e, v, f = struct.unpack_from(">hiiihB", b, p + 5)
            self.temp_fet = t / 10
        elif op == COMM_FW_VERSION and n >= 3:
            self.fw = b[p + 1] + b[p + 2] / 100
            return
        else:
            return
        self.i_motor = im / 100
        self.i_in = ii / 100
        self.erpm = float(e)
        self.v_in = v / 10
        self.fault = f
        self.frames += 1
        self.silent = 0
        self.ok = self.ok + 1 if f == 0 else 0
