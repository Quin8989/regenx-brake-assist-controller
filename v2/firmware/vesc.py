# vesc.py — VESC UART link: framing, CRC16, streaming parser, scheduler.
#
# Pure over a duck-typed uart (needs .any(), .readinto(mv), .write(mv)), so
# the whole module runs under CPython for tests. Protocol facts (opcodes,
# selective bit table, scales) verified against the v1 implementation that
# ran on this exact controller. RGX-2-003 D5/D7/D11.
#
# No allocation in the steady state: every buffer is created in __init__.

import struct

# --- Protocol constants ------------------------------------------------------
FRAME_START_SHORT = 0x02
FRAME_START_LONG = 0x03
FRAME_END = 0x03

COMM_FW_VERSION = 0
COMM_GET_VALUES = 4
COMM_SET_CURRENT = 6
COMM_GET_VALUES_SELECTIVE = 50

# Selective-response field table, in bit order (v1-verified):
# (bit, struct fmt char, divisor, attribute)
_SEL_TABLE = (
    (0, "h", 10.0, "temp_fet"),
    (1, "h", 10.0, "temp_motor"),
    (2, "i", 100.0, "i_motor"),
    (3, "i", 100.0, "i_in"),
    (4, "i", 100.0, "avg_id"),
    (5, "i", 100.0, "avg_iq"),
    (6, "h", 1000.0, "duty"),
    (7, "i", 1.0, "erpm"),
    (8, "h", 10.0, "v_in"),
    (9, "i", 10000.0, "ah"),
    (10, "i", 10000.0, "ah_charged"),
    (11, "i", 10000.0, "wh"),
    (12, "i", 10000.0, "wh_charged"),
    (13, "i", 1.0, "tacho"),
    (14, "i", 1.0, "tacho_abs"),
    (15, "B", 1.0, "fault"),
)
_FMT_SIZE = {"h": 2, "i": 4, "B": 1}

_FULL_FMT = ">hhiiiihihiiiiiiB"  # GET_VALUES bits 0-15, 53 bytes
_FULL_ATTRS = ("temp_fet", "temp_motor", "i_motor", "i_in", "avg_id",
               "avg_iq", "duty", "erpm", "v_in", "ah", "ah_charged",
               "wh", "wh_charged", "tacho", "tacho_abs", "fault")
_FULL_DIV = (10.0, 10.0, 100.0, 100.0, 100.0, 100.0, 1000.0, 1.0, 10.0,
             10000.0, 10000.0, 10000.0, 10000.0, 1.0, 1.0, 1.0)

# --- CRC16-XMODEM (poly 0x1021, init 0) -------------------------------------
def _make_table():
    tab = []
    for i in range(256):
        c = i << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) if (c & 0x8000) else (c << 1)
            c &= 0xFFFF
        tab.append(c)
    return tab

_CRC_TAB = _make_table()


def crc16(buf, start, length):
    """Table CRC16-XMODEM over buf[start:start+length]."""
    crc = 0
    tab = _CRC_TAB
    for i in range(start, start + length):
        crc = ((crc << 8) & 0xFFFF) ^ tab[((crc >> 8) ^ buf[i]) & 0xFF]
    return crc

try:  # on-target viper override, byte-identical semantics (gate FW-1)
    from vesc_fast import crc16 as crc16  # noqa: F811
except ImportError:
    pass


# --- Frame building (into caller-owned buffers) ------------------------------
def wrap(out, payload_len):
    """Payload already at out[2:2+payload_len]; add framing. Returns total."""
    out[0] = FRAME_START_SHORT
    out[1] = payload_len
    c = crc16(out, 2, payload_len)
    out[2 + payload_len] = (c >> 8) & 0xFF
    out[3 + payload_len] = c & 0xFF
    out[4 + payload_len] = FRAME_END
    return payload_len + 5


def pack_set_current(out, amps):
    out[2] = COMM_SET_CURRENT
    struct.pack_into(">i", out, 3, int(amps * 1000))
    return wrap(out, 5)


def pack_telemetry_req(out, mask):
    out[2] = COMM_GET_VALUES_SELECTIVE
    struct.pack_into(">I", out, 3, mask)
    return wrap(out, 5)


def pack_fw_req(out):
    out[2] = COMM_FW_VERSION
    return wrap(out, 1)


# --- Streaming parser --------------------------------------------------------
_HUNT, _LEN, _PAYLOAD, _CRC_HI, _CRC_LO, _END = range(6)
_MAX_PAYLOAD = 255  # short frames only — see class docstring


class FrameParser:
    """Byte-stream -> validated payloads. O(1) resync (postmortem #2).

    Short (0x02) frames only, by decision: nothing our command set can
    receive exceeds 78 B, and the 0x03 long-start is byte-identical to the
    0x03 frame-end — honouring it lets a trailing END byte swallow the next
    frame's start after any resync (v1 documented this ambiguity and kept
    the ambiguous path anyway). 0x03 in HUNT is treated as junk; long_seen
    counts them for diagnostics.

    handler(payload_memoryview, length) is called per good frame.
    """

    def __init__(self, handler):
        self._handler = handler
        self._buf = bytearray(_MAX_PAYLOAD)
        self._mv = memoryview(self._buf)
        self._state = _HUNT
        self._need = 0
        self._got = 0
        self._crc = 0
        # health counters (RGX-2-003 §6.5)
        self.frames_ok = 0
        self.crc_fail = 0
        self.resync = 0
        self.long_seen = 0

    def feed(self, data, n):
        i = 0
        st = self._state
        while i < n:
            b = data[i]
            if st == _HUNT:
                if b == FRAME_START_SHORT:
                    st = _LEN
                elif b == FRAME_START_LONG:
                    self.long_seen += 1
                i += 1
            elif st == _LEN:
                self._need = b
                self._got = 0
                st = _PAYLOAD if 0 < b else _HUNT
                i += 1
            elif st == _PAYLOAD:
                take = n - i
                room = self._need - self._got
                if take > room:
                    take = room
                self._mv[self._got:self._got + take] = data[i:i + take]
                self._got += take
                i += take
                if self._got == self._need:
                    st = _CRC_HI
            elif st == _CRC_HI:
                self._crc = b << 8
                st = _CRC_LO
                i += 1
            elif st == _CRC_LO:
                self._crc |= b
                st = _END
                i += 1
            else:  # _END
                if b == FRAME_END and crc16(self._buf, 0, self._need) == self._crc:
                    self.frames_ok += 1
                    self._handler(self._mv, self._need)
                    st = _HUNT
                    i += 1
                else:
                    self.crc_fail += 1
                    self.resync += 1
                    st = _HUNT
                    # do not consume: this byte may start the next frame
        self._state = st


# --- Telemetry values --------------------------------------------------------
class Values:
    def __init__(self):
        self.temp_fet = 0.0
        self.temp_motor = 0.0
        self.i_motor = 0.0
        self.i_in = 0.0
        self.avg_id = 0.0
        self.avg_iq = 0.0
        self.duty = 0.0
        self.erpm = 0.0
        self.v_in = 0.0
        self.ah = 0.0
        self.ah_charged = 0.0
        self.wh = 0.0
        self.wh_charged = 0.0
        self.tacho = 0.0
        self.tacho_abs = 0.0
        self.fault = 0
        self.fw_major = 0
        self.fw_minor = 0


def parse_selective(payload, n, values):
    """COMM_GET_VALUES_SELECTIVE response: opcode, mask:>I, fields per bit."""
    if n < 5:
        return False
    mask = struct.unpack_from(">I", payload, 1)[0]
    off = 5
    for bit, fmt, div, attr in _SEL_TABLE:
        if mask & (1 << bit):
            size = _FMT_SIZE[fmt]
            if off + size > n:
                return False
            (raw,) = struct.unpack_from(">" + fmt, payload, off)
            setattr(values, attr, raw / div if div != 1.0 else float(raw))
            off += size
    if mask & (1 << 15):
        values.fault = int(values.fault)
    return True


def parse_full(payload, n, values):
    if n < 1 + struct.calcsize(_FULL_FMT):
        return False
    fields = struct.unpack_from(_FULL_FMT, payload, 1)
    for val, attr, div in zip(fields, _FULL_ATTRS, _FULL_DIV):
        setattr(values, attr, val / div if div != 1.0 else float(val))
    values.fault = int(values.fault)
    return True


# --- Link scheduler ----------------------------------------------------------
class VescLink:
    """SET_CURRENT every tick (the keepalive), telemetry every TELEM_DIV
    ticks, temp mask every TEMP_DIV requests. RGX-2-003 D7."""

    def __init__(self, uart, cfg):
        self._uart = uart
        self._cfg = cfg
        self._txc = bytearray(16)   # command frame
        self._txr = bytearray(16)   # request frame
        self._rx = bytearray(256)
        self._rxmv = memoryview(self._rx)
        self.values = Values()
        self.parser = FrameParser(self._on_frame)
        self.last_frame_ms = -10_000_000
        self.rtt_ms = 0.0
        self._req_ms = -1
        self._tick_n = 0
        self._req_n = 0
        self._now = 0
        self.rx_overrun = 0

    # -- RX handler
    def _on_frame(self, payload, n):
        op = payload[0]
        good = False
        if op == COMM_GET_VALUES_SELECTIVE:
            good = parse_selective(payload, n, self.values)
        elif op == COMM_GET_VALUES:
            good = parse_full(payload, n, self.values)
        elif op == COMM_FW_VERSION and n >= 3:
            self.values.fw_major = payload[1]
            self.values.fw_minor = payload[2]
            good = True
        if good:
            self.last_frame_ms = self._now
            if self._req_ms >= 0:
                rtt = self._now - self._req_ms
                self.rtt_ms += 0.2 * (rtt - self.rtt_ms)
                self._req_ms = -1

    def request_fw(self):
        n = pack_fw_req(self._txr)
        self._uart.write(memoryview(self._txr)[:n])

    def tick(self, now_ms, current_a):
        """One control tick: transmit, then drain RX. Never blocks."""
        self._now = now_ms
        n = pack_set_current(self._txc, current_a)
        self._uart.write(memoryview(self._txc)[:n])
        self._tick_n += 1
        if self._tick_n % self._cfg.TELEM_DIV == 0:
            self._req_n += 1
            mask = (self._cfg.TELEM_MASK_TEMP
                    if self._req_n % self._cfg.TEMP_DIV == 0
                    else self._cfg.TELEM_MASK)
            n = pack_telemetry_req(self._txr, mask)
            self._uart.write(memoryview(self._txr)[:n])
            if self._req_ms < 0:
                self._req_ms = now_ms
        self.poll(now_ms)

    def poll(self, now_ms):
        self._now = now_ms
        avail = self._uart.any()
        while avail:
            take = avail if avail < len(self._rx) else len(self._rx)
            got = self._uart.readinto(self._rxmv[:take])
            if not got:
                break
            if got == len(self._rx):
                self.rx_overrun += 1
            self.parser.feed(self._rxmv, got)
            avail = self._uart.any()

    def age_ms(self, now_ms):
        return now_ms - self.last_frame_ms
