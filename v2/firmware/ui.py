# ui.py — core 1: display, RAM ring log, flush to flash at standstill.
#
# The record codec, ring and standstill test are pure (host-tested); Core1
# and Oled are target-only. RGX-2-003 D13/D14.
#
# Flash writes stall BOTH cores (XIP), so they only happen at a standstill
# that has lasted STANDSTILL_MS, one ~4 KB chunk per pass, re-checked every
# pass. Every standstill flushes whatever is new, so a power-off loses at most
# the records since the last stop.

import struct
from array import array

import config as C
import control as K

# ms, wheel rpm x10, erpm/10, v_in x100, i_in x100, i_motor x100, i_cmd x100,
# throttle x1000, vsys x1000, temp_fet x10, state | brake<<2, fault.
# Only measurements are logged; slip, speed and k are derived offline
# (tools/decode_log.py).
REC_FMT = "<IHhHhhhHHhBB"
REC_SIZE = struct.calcsize(REC_FMT)  # 24
STATE_NAMES = ("RUN", "NO LINK", "VESC FAULT", "DEAD")


def _i(x, lo, hi):
    return lo if x < lo else (hi if x > hi else round(x))


def pack(buf, off, ms, sn):
    struct.pack_into(
        REC_FMT, buf, off, ms & 0xFFFFFFFF,
        _i(sn[K.SN_WHEEL] * 10, 0, 65535),
        _i(sn[K.SN_ERPM] / 10, -32768, 32767),
        _i(sn[K.SN_VIN] * 100, 0, 65535),
        _i(sn[K.SN_IIN] * 100, -32768, 32767),
        _i(sn[K.SN_IMOTOR] * 100, -32768, 32767),
        _i(sn[K.SN_ICMD] * 100, -32768, 32767),
        _i(sn[K.SN_THR] * 1000, 0, 65535),
        _i(sn[K.SN_VSYS] * 1000, 0, 65535),
        _i(sn[K.SN_TFET] * 10, -32768, 32767),
        int(sn[K.SN_STATE]) | (4 if sn[K.SN_BRAKE] else 0),
        _i(sn[K.SN_FAULT], 0, 255))


def header(fw):
    return ("RGX2 rec=%s fw=%.2f k=%g pp=%d circ=%g\n" % (
        REC_FMT, fw, C.K_RATIO, C.POLE_PAIRS, C.WHEEL_CIRC_M)).encode()


def still(sn):
    """Nothing moving and nothing commanded: safe to stall both cores."""
    return (sn[K.SN_WHEEL] == 0.0 and sn[K.SN_THR] == 0.0 and not sn[K.SN_BRAKE]
            and -0.1 < sn[K.SN_ICMD] < 0.1 and -100.0 < sn[K.SN_ERPM] < 100.0)


class Log:
    """RAM ring of records plus a flush watermark. Pure."""

    def __init__(self, n):
        self.n = n
        self.buf = bytearray(n * REC_SIZE)
        self._mv = memoryview(self.buf)
        self.w = 0                  # records ever written
        self.f = 0                  # records ever flushed (or overwritten)

    def add(self, ms, sn):
        pack(self.buf, (self.w % self.n) * REC_SIZE, ms, sn)
        self.w += 1
        if self.w - self.f > self.n:
            self.f = self.w - self.n

    def chunk(self):
        """Next contiguous run of unflushed records, or None."""
        if self.f == self.w:
            return None
        s = self.f % self.n
        k = min(self.w - self.f, self.n - s, C.FLUSH_RECORDS)
        self.f += k
        return self._mv[s * REC_SIZE:(s + k) * REC_SIZE]


class Oled:
    """Minimal SSD1306 128x64 on I2C."""

    def __init__(self, i2c):
        import framebuf
        self.i2c = i2c
        self.buf = bytearray(1024)
        self.fb = framebuf.FrameBuffer(self.buf, 128, 64, framebuf.MONO_VLSB)
        for c in (0xAE, 0x20, 0x00, 0x40, 0xA1, 0xA8, 63, 0xC8, 0xD3, 0x00,
                  0xDA, 0x12, 0xD5, 0x80, 0xD9, 0xF1, 0xDB, 0x30, 0x81, 0xFF,
                  0xA4, 0xA6, 0x8D, 0x14, 0xAF):
            self._cmd(c)

    def _cmd(self, c):
        self.i2c.writeto(C.OLED_ADDR, bytes((0x80, c)))

    def show(self):
        for c in (0x21, 0, 127, 0x22, 0, 7):
            self._cmd(c)
        self.i2c.writevto(C.OLED_ADDR, (b"\x40", self.buf))


class Core1:
    """Runs on core 1 via _thread. Never lets an exception end the thread."""

    def __init__(self, sn):
        import os
        from machine import I2C, Pin
        self.sn = sn
        self.cp = array("f", sn)
        self.log = Log(C.LOG_RECORDS)
        self.i2c = I2C(0, sda=Pin(C.PIN_SDA), scl=Pin(C.PIN_SCL), freq=C.I2C_FREQ)
        self.oled = None
        self.file = None
        self.errors = 0
        self._t_log = self._t_disp = self._t_move = 0
        try:
            os.mkdir(C.LOG_DIR)
        except OSError:
            pass
        ids = [int(x[:4]) for x in os.listdir(C.LOG_DIR) if x[:4].isdigit()]
        self.name = "%04d.bin" % (max(ids) + 1 if ids else 0)

    def run(self):
        import time
        while True:
            try:
                self.step(time.ticks_ms(), time.ticks_diff)
            except Exception:
                self.errors += 1
            time.sleep_ms(20)

    def step(self, now, diff):
        cp = self.cp
        cp[:] = self.sn
        moving = not still(cp)
        if moving:
            self._t_move = now
        if diff(now, self._t_log) >= (C.LOG_RIDE_MS if moving else C.LOG_IDLE_MS):
            self._t_log = now
            self.log.add(now, cp)
        if diff(now, self._t_disp) >= C.DISPLAY_MS:
            self._t_disp = now
            try:
                self._render(cp)
            except OSError:             # display gone: re-init next time
                self.oled = None
                self.errors += 1
        try:
            if diff(now, self._t_move) >= C.STANDSTILL_MS and self._flush(cp):
                return
            self._close()
        except OSError:
            self.errors += 1
            self.file = None

    def _flush(self, cp):
        mv = self.log.chunk()
        if mv is None:
            return False
        if self.file is None:
            self._open(cp)
        self.file.write(mv)
        return True

    def _open(self, cp):
        import os
        names = sorted(os.listdir(C.LOG_DIR))
        while names and names[0] != self.name:     # make room: oldest first
            st = os.statvfs("/")
            if st[0] * st[3] >= C.LOG_MIN_FREE:
                break
            os.remove(C.LOG_DIR + "/" + names.pop(0))
        new = self.name not in names
        self.file = open(C.LOG_DIR + "/" + self.name, "ab")
        if new:
            self.file.write(header(cp[K.SN_FW]))

    def _close(self):
        f = self.file
        if f is not None:
            self.file = None
            f.close()

    def _render(self, sn):
        o = self.oled
        if o is None:
            o = self.oled = Oled(self.i2c)
        t = o.fb.text
        o.fb.fill(0)
        st = int(sn[K.SN_STATE])
        if st == K.RUN:
            t("%5.1f km/h %4.1fV" % (sn[K.SN_WHEEL] * C.WHEEL_CIRC_M * 0.06,
                                     sn[K.SN_VIN]), 0, 0)
            t("%+6.1f A %5.0f W" % (sn[K.SN_ICMD], sn[K.SN_VIN] * sn[K.SN_IIN]),
              0, 20)
        else:
            t(STATE_NAMES[st], 0, 0)
            t("fault %d fw %.2f" % (sn[K.SN_FAULT], sn[K.SN_FW]), 0, 12)
            t("%4.1fV %3.0fC %.2fV" % (sn[K.SN_VIN], sn[K.SN_TFET],
                                       sn[K.SN_VSYS]), 0, 24)
        # link and loop health on every page (FW-7 needs them green)
        t("fr%d bad%d" % (sn[K.SN_FRAMES], sn[K.SN_BAD]), 0, 44)
        t("miss%d %.1fms e%d" % (sn[K.SN_MISS], sn[K.SN_TMAX] / 1000,
                                 self.errors), 0, 54)
        o.show()
