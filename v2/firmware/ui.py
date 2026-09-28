# ui.py — core 1: display pages, RAM ring logger, standstill flash flush,
# USB bench stream. The record codec and Ring are pure (host-tested); the
# display/flush sections are target-only. RGX-2-003 D13/D14.
#
# XIP reality (D13): a flash write stalls BOTH cores; flush only ever runs
# at verified standstill, which is exactly what the gate below enforces.

import struct
from array import array

import config
import control

try:
    from machine import Pin, I2C
    import framebuf
    import time
    import os
    _ON_TARGET = True
except ImportError:
    _ON_TARGET = False

# --- record codec (pure) -----------------------------------------------------
REC_FMT = "<IHhHhhhHHHBB"
REC_SIZE = struct.calcsize(REC_FMT)  # 24 B (verified by test)


def pack_record(buf, off, ms, sn):
    struct.pack_into(
        REC_FMT, buf, off,
        ms & 0xFFFFFFFF,
        min(65535, round(sn[control.SN_WHEEL_RPM] * 10)),
        max(-32768, min(32767, round(sn[control.SN_ERPM] / 10))),
        min(65535, round(sn[control.SN_VBANK] * 100)),
        max(-32768, min(32767, round(sn[control.SN_IIN] * 100))),
        max(-32768, min(32767, round(sn[control.SN_IMOTOR] * 100))),
        max(-32768, min(32767, round(sn[control.SN_ICMD] * 100))),
        min(65535, round(sn[control.SN_SLIP] * 1000)),
        min(65535, round(sn[control.SN_THROTTLE] * 1000)),
        min(65535, round(sn[control.SN_VSYS] * 1000)),
        int(sn[control.SN_STATE]) & 0xFF,
        int(sn[control.SN_FAULT]) & 0xFF,
    )


def unpack_record(buf, off):
    (ms, wr, erpm10, vb, iin, im, icmd, slip, thr, vsys, state,
     fault) = struct.unpack_from(REC_FMT, buf, off)
    return {
        "ms": ms, "wheel_rpm": wr / 10.0, "erpm": erpm10 * 10.0,
        "v_bank": vb / 100.0, "i_in": iin / 100.0, "i_motor": im / 100.0,
        "i_cmd": icmd / 100.0, "slip": slip / 1000.0,
        "throttle": thr / 1000.0, "vsys": vsys / 1000.0,
        "state": state, "fault": fault,
    }


class Ring:
    """Fixed byte ring of REC_SIZE records; overwrites oldest. Pure."""

    def __init__(self, n_records=None):
        self.n = n_records or config.LOG_RING_RECORDS
        self.buf = bytearray(self.n * REC_SIZE)
        self.w = 0          # next record index
        self.count = 0      # total stored (saturates at n)
        self.dropped = 0

    def append(self, ms, sn):
        pack_record(self.buf, self.w * REC_SIZE, ms, sn)
        self.w = (self.w + 1) % self.n
        if self.count < self.n:
            self.count += 1
        else:
            self.dropped += 1

    def records(self):
        """Yield (offset) of each stored record, oldest first."""
        start = (self.w - self.count) % self.n
        for i in range(self.count):
            yield ((start + i) % self.n) * REC_SIZE

    def clear(self):
        self.w = 0
        self.count = 0


# --- everything below is target-only ----------------------------------------
if _ON_TARGET:

    class SSD1306(framebuf.FrameBuffer):
        """Minimal SSD1306 I2C driver (framebuf-backed)."""

        def __init__(self, i2c):
            self.i2c = i2c
            self.addr = config.OLED_ADDR
            self.w = config.OLED_W
            self.h = config.OLED_H
            self.pages = self.h // 8
            self.buffer = bytearray(self.pages * self.w)
            super().__init__(self.buffer, self.w, self.h, framebuf.MONO_VLSB)
            self._cmdbuf = bytearray(2)
            self._databuf = bytearray(1)
            self.ok = True
            self.reinits = 0
            self.init_display()

        def _cmd(self, c):
            self._cmdbuf[0] = 0x80
            self._cmdbuf[1] = c
            self.i2c.writeto(self.addr, self._cmdbuf)

        def init_display(self):
            for c in (0xAE, 0x20, 0x00, 0x40, 0xA1, 0xA8, self.h - 1, 0xC8,
                      0xD3, 0x00, 0xDA, 0x12, 0xD5, 0x80, 0xD9, 0xF1,
                      0xDB, 0x30, 0x81, 0xFF, 0xA4, 0xA6, 0x8D, 0x14, 0xAF):
                self._cmd(c)
            self.fill(0)
            self.show()

        def show(self):
            self._cmd(0x21); self._cmd(0); self._cmd(self.w - 1)
            self._cmd(0x22); self._cmd(0); self._cmd(self.pages - 1)
            self.i2c.writevto(self.addr, (b"\x40", self.buffer))

    class Panel:
        """Pages + lazy re-init. Never scheduled, never on core 0 (D14)."""

        def __init__(self):
            self.i2c = I2C(0, sda=Pin(config.PIN_I2C_SDA),
                           scl=Pin(config.PIN_I2C_SCL),
                           freq=config.I2C_FREQ)
            self.oled = None
            self.errors = 0
            self._try_init()

        def _try_init(self):
            try:
                self.oled = SSD1306(self.i2c)
            except OSError:
                self.oled = None
                self.errors += 1

        def render(self, sn):
            if self.oled is None:
                self._try_init()
                if self.oled is None:
                    return
            o = self.oled
            try:
                o.fill(0)
                st = int(sn[control.SN_STATE])
                if st == control.LIMP or sn[control.SN_FAULT]:
                    self._page_system(o, sn)
                else:
                    self._page_ride(o, sn)
                o.show()
            except OSError:
                self.errors += 1
                self.oled = None      # lazy re-init next render

        def _page_ride(self, o, sn):
            o.text("%5.1f km/h" % sn[control.SN_KMH], 0, 0)
            o.text("%5.1f V" % sn[control.SN_VBANK], 0, 12)
            o.text("%5.1f A %s" % (abs(sn[control.SN_ICMD]),
                   "RGN" if sn[control.SN_ICMD] < 0 else "AST"), 0, 24)
            # slip bar: full = freewheel, empty = held
            w = int(sn[control.SN_SLIP] * (config.OLED_W - 2))
            o.rect(0, 40, config.OLED_W, 10, 1)
            o.fill_rect(1, 41, w, 8, 1)
            o.text("rtt%3.0f e%d" % (sn[control.SN_RTT],
                   int(sn[control.SN_CRCFAIL])), 0, 54)

        def _page_system(self, o, sn):
            o.text("STATE %d RSN %d" % (int(sn[control.SN_STATE]),
                                        int(sn[control.SN_REASON])), 0, 0)
            o.text("FAULT %d" % int(sn[control.SN_FAULT]), 0, 12)
            o.text("VSYS %4.2f" % sn[control.SN_VSYS], 0, 24)
            o.text("crc%d rs%d" % (int(sn[control.SN_CRCFAIL]),
                                   int(sn[control.SN_RESYNC])), 0, 36)
            o.text("miss%d gc%3.1f" % (int(sn[control.SN_DLMISS]),
                                       sn[control.SN_GCMAX]), 0, 48)

    class Core1:
        """Entry for _thread on core 1. Reads snapshots, renders, logs,
        flushes at standstill, streams on USB when enabled."""

        def __init__(self, snapshot):
            self.snapshot = snapshot
            self.panel = Panel()
            self.ring = Ring()
            self._sn = array("f", [0.0] * control.SN_LEN)
            self._still_since = -1
            self._boot_id = self._next_boot_id()
            self.bench_stream = False

        def _next_boot_id(self):
            try:
                os.mkdir(config.LOG_DIR)
            except OSError:
                pass
            try:
                names = os.listdir(config.LOG_DIR)
            except OSError:
                return 0
            return len(names)

        def _standstill(self, now, sn):
            moving = (sn[control.SN_WHEEL_RPM] > 0.5 or
                      sn[control.SN_THROTTLE] > 0.02)
            if moving:
                self._still_since = -1
                return False
            if self._still_since < 0:
                self._still_since = now
            return now - self._still_since > config.STANDSTILL_MS

        def _flush(self):
            if self.ring.count == 0:
                return
            path = "%s/ride%04d.bin" % (config.LOG_DIR, self._boot_id)
            with open(path, "ab") as f:
                mv = memoryview(self.ring.buf)
                for off in self.ring.records():
                    f.write(mv[off:off + REC_SIZE])
            self.ring.clear()

        def run(self):
            period_disp = 1000 // config.DISPLAY_HZ
            last_disp = 0
            last_log = 0
            while True:
                now = time.ticks_ms()
                self.snapshot.read(self._sn)
                sn = self._sn
                still = self._standstill(now, sn)
                hz = config.LOG_IDLE_HZ if still else config.LOG_RIDE_HZ
                if time.ticks_diff(now, last_log) >= 1000 // hz:
                    last_log = now
                    self.ring.append(now, sn)
                    if self.bench_stream:
                        print("LOG,%d,%.1f,%.0f,%.2f,%.2f,%.2f,%.3f" % (
                            now, sn[control.SN_KMH], sn[control.SN_ERPM],
                            sn[control.SN_VBANK], sn[control.SN_ICMD],
                            sn[control.SN_VSYS], sn[control.SN_SLIP]))
                if time.ticks_diff(now, last_disp) >= period_disp:
                    last_disp = now
                    self.panel.render(sn)
                if still and self.ring.count >= self.ring.n // 2:
                    self._flush()          # both cores stall briefly: D13
                time.sleep_ms(20)
