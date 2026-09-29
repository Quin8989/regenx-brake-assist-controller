# ui.py — core 1: the display.
#
# Speed, bank voltage and current, and below them one line per problem, only
# while there is one. Redrawing the screen takes about 25 ms over I2C, longer
# than a control tick, which is why this runs on the second core.
#
# problems() is pure (host-tested); Oled and Core1 are target-only.
# RGX-2-003 D14 as amended in Rev D.

import config as C
import control as K


def problems(sn, errors=0):
    """Everything wrong right now, most serious first; empty when all is well.

    Bad frames, late ticks and display errors are counts since power-on, so
    they stay up once seen: any at all means the wiring or the timing needs a
    look.
    """
    out = []
    st = sn[K.SN_STATE]
    if st == K.LIMP_FAULT:
        out.append("VESC FAULT %d" % sn[K.SN_FAULT])
    elif st == K.LIMP_LINK:
        out.append("NO LINK")
    t = sn[K.SN_TFET]
    if t > C.TEMP_HOT:
        out.append("HOT %d C" % t)
    elif t < C.TEMP_COLD:
        out.append("COLD %d C" % t)
    if sn[K.SN_BAD]:
        out.append("BAD FRAMES %d" % sn[K.SN_BAD])
    if sn[K.SN_LATE]:
        out.append("LATE TICKS %d" % sn[K.SN_LATE])
    if errors:
        out.append("SCREEN ERR %d" % errors)
    return out


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
        from machine import I2C, Pin
        self.sn = sn
        self.i2c = I2C(0, sda=Pin(C.PIN_SDA), scl=Pin(C.PIN_SCL), freq=C.I2C_FREQ)
        self.oled = None
        self.errors = 0

    def run(self):
        import time
        while True:
            try:
                self.render()
            except Exception:           # display unplugged or an I2C glitch:
                self.oled = None        # count it, set it up again next pass
                self.errors += 1
            time.sleep_ms(C.DISPLAY_MS)

    def render(self):
        o = self.oled
        if o is None:
            o = self.oled = Oled(self.i2c)
        sn = self.sn
        t = o.fb.text
        o.fb.fill(0)
        t("%5.1f km/h" % (sn[K.SN_WHEEL] * C.WHEEL_CIRC_M * 0.06), 0, 0)
        t("%5.1f V" % sn[K.SN_VIN], 0, 10)
        t("%+5.1f A" % sn[K.SN_ICMD], 0, 20)
        for n, line in enumerate(problems(sn, self.errors)[:3]):
            t(line, 0, 34 + 10 * n)
        o.show()
