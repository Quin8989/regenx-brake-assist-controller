# sensors.py — PIO wheel-speed capture, throttle validation, brake, VSYS.
#
# Hardware-facing; imports are guarded so the module *imports* under CPython
# (classes then require injected fakes — see tests). Spec §10.1-10.3,
# RGX-2-003 §7.

import config
import kinematics

try:
    import rp2
    from machine import Pin, ADC
    _ON_TARGET = True
except ImportError:
    _ON_TARGET = False


if _ON_TARGET:
    # Period capture: two symmetric 2-cycle loops count the high phase and
    # the low phase between edges; each phase count is pushed (inverted x).
    # At 2 MHz PIO clock every loop iteration is exactly 1 us. §7.1 / D-PIO.
    @rp2.asm_pio()
    def _period_pio():
        wrap_target()
        mov(x, invert(null))          # x = 0xFFFFFFFF
        label("high")
        jmp(pin, "high_cont")         # 1 cycle
        jmp("high_done")
        label("high_cont")
        jmp(x_dec, "high")            # 1 cycle -> 2 per iteration
        label("high_done")
        mov(isr, invert(x))
        push(noblock)
        mov(x, invert(null))
        label("low")
        jmp(pin, "low_done")          # 1 cycle
        jmp(x_dec, "low")             # 1 cycle -> 2 per iteration
        label("low_done")
        mov(isr, invert(x))
        push(noblock)
        wrap()


class WheelSpeed:
    """Drain the PIO FIFO each tick; pair high+low phase counts into a full
    period. Zero-speed by timeout (a stopped wheel emits nothing)."""

    _US_PER_COUNT = 1.0  # 2 cycles @ 2 MHz

    def __init__(self):
        if not _ON_TARGET:
            raise RuntimeError("WheelSpeed is target-only; inject a fake")
        pin = Pin(config.PIN_SPD, Pin.IN, Pin.PULL_UP)  # spec §10.2
        self._sm = rp2.StateMachine(0, _period_pio, freq=2_000_000,
                                    in_base=pin, jmp_pin=pin)
        self._sm.active(1)
        self._pending = -1          # first-of-pair phase count
        self._period_us = 0.0
        self._last_edge_ms = -config.SPD_ZERO_MS
        self._rpm = 0.0

    def _push_period(self, us, now_ms):
        if us < config.SPD_MIN_PERIOD_US:      # >60 km/h equiv = noise
            return
        self._period_us = us
        self._last_edge_ms = now_ms
        self._rpm = kinematics.wheel_rpm_from_period_us(us)

    def wheel_rpm(self, now_ms):
        sm = self._sm
        while sm.rx_fifo():
            count = sm.get()
            if self._pending < 0:
                self._pending = count
            else:
                self._push_period((self._pending + count)
                                  * self._US_PER_COUNT, now_ms)
                self._pending = -1
        if now_ms - self._last_edge_ms > config.SPD_ZERO_MS:
            self._rpm = 0.0
        return self._rpm


class Throttle:
    """ADC0, median-of-3, spec §10.3 window with 50 ms fail persistence."""

    def __init__(self):
        if not _ON_TARGET:
            raise RuntimeError("Throttle is target-only; inject a fake")
        self._adc = ADC(config.ADC_THROTTLE)
        self._s = [0, 0, 0]
        self._i = 0
        self._bad_since = -1
        self.failed = False

    def throttle(self, now_ms):
        self._s[self._i] = self._adc.read_u16()
        self._i = (self._i + 1) % 3
        a, b, c = self._s
        med = max(min(a, b), min(max(a, b), c))
        frac = med / 65535.0
        if frac < config.THROTTLE_LO or frac > config.THROTTLE_HI:
            if self._bad_since < 0:
                self._bad_since = now_ms
            elif now_ms - self._bad_since > config.THROTTLE_FAIL_MS:
                self.failed = True
            return 0.0, self.failed
        self._bad_since = -1
        self.failed = False
        span = config.THROTTLE_HI - config.THROTTLE_LO
        return (frac - config.THROTTLE_LO) / span, False


class SensorBank:
    """The duck type control.py consumes; wires the concrete sensors."""

    def __init__(self):
        self._wheel = WheelSpeed()
        self._throttle = Throttle()
        self._vsys = ADC(config.ADC_VSYS)
        if config.BRAKE_FITTED:
            self._brake = Pin(config.PIN_BRAKE, Pin.IN, Pin.PULL_UP)
        else:
            self._brake = None

    def wheel_rpm(self, now_ms):
        return self._wheel.wheel_rpm(now_ms)

    def throttle(self, now_ms):
        return self._throttle.throttle(now_ms)

    def brake(self):
        return (self._brake is not None) and (self._brake.value() == 0)

    def vsys(self):
        return self._vsys.read_u16() * 3.0 * 3.3 / 65535.0
