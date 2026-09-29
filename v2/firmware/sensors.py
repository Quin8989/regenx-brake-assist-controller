# sensors.py — wheel speed (PIO), throttle, brake, VSYS. Spec §10.1-10.3.
#
# Wheel and Throttle take their hardware object as an argument, so the logic
# runs under CPython with fakes. Only Sensors() touches machine/rp2.

import config as C


class Wheel:
    """Wheel rpm from the PIO phase lengths (us), one read per tick.

    Every high and every low phase arrives as its own word, so any two
    consecutive phases make a full period and the estimate refreshes at each
    edge. A phase shorter than SPD_MIN_PHASE_US is a glitch (or the counter
    running out after 71 min) and restarts the pairing. Between edges the
    estimate is capped by the time since the last edge, so it decays while
    braking instead of holding a stale period, and reads 0 once stopped.
    """

    def __init__(self, sm):
        self.sm = sm
        self.rpm = 0.0
        self._prev = 0
        self._age = 0

    def read(self):
        sm = self.sm
        while sm.rx_fifo():
            ph = sm.get()
            if ph < C.SPD_MIN_PHASE_US:
                self._prev = 0
                continue
            if self._prev:
                self.rpm = C.SPD_K / (self._prev + ph)
            self._prev = ph
            self._age = 0
        self._age += 1
        cap = C.SPD_K / (self._age * C.TICK_MS * 1000)
        if self.rpm > cap:
            if cap > C.SPD_MIN_RPM:
                self.rpm = cap
            else:                       # stopped: next start pairs fresh phases
                self.rpm = 0.0
                self._prev = 0
        return self.rpm


class Throttle:
    """0..1 with deadband, or 0 when not trustworthy (spec §10.3).

    Out of the [THR_LO, THR_HI] window (open wire reads 0 V through R2, a
    short reads rail) the throttle simply reads 0: assist stops, regen is
    untouched, and there is no fault state to manage. Assist arms only once
    the throttle has been seen at idle, so a held or stuck throttle at
    power-on gives nothing.
    """

    def __init__(self, adc):
        self.adc = adc
        self.armed = False

    def read(self):
        f = self.adc.read_u16() / 65535
        if not C.THR_LO < f < C.THR_HI:
            return 0.0
        t = (f - C.THR_IDLE) / (C.THR_FULL - C.THR_IDLE)
        if t < C.THR_DEADBAND:
            self.armed = True
            return 0.0
        if not self.armed:
            return 0.0
        t = (t - C.THR_DEADBAND) / (1.0 - C.THR_DEADBAND)
        return t if t < 1.0 else 1.0


class Sensors:
    """The bundle control.py consumes. Target only."""

    def __init__(self):
        import rp2
        from machine import ADC, Pin

        @rp2.asm_pio()
        def phases():
            # Push the length of every high and low phase in us (2 cycles per
            # loop at 2 MHz). Sync to a rising edge first so the first phase
            # is whole.
            wait(0, pin, 0)                 # noqa: F821 (PIO assembler names)
            wait(1, pin, 0)                 # noqa: F821
            wrap_target()                   # noqa: F821
            mov(x, invert(null))            # noqa: F821
            label("hi")                     # noqa: F821
            jmp(pin, "hi_c")                # noqa: F821
            jmp("hi_d")                     # noqa: F821
            label("hi_c")                   # noqa: F821
            jmp(x_dec, "hi")                # noqa: F821
            label("hi_d")                   # noqa: F821
            mov(isr, invert(x))             # noqa: F821
            push(noblock)                   # noqa: F821
            mov(x, invert(null))            # noqa: F821
            label("lo")                     # noqa: F821
            jmp(pin, "lo_d")                # noqa: F821
            jmp(x_dec, "lo")                # noqa: F821
            label("lo_d")                   # noqa: F821
            mov(isr, invert(x))             # noqa: F821
            push(noblock)                   # noqa: F821
            wrap()                          # noqa: F821

        spd = Pin(C.PIN_SPD, Pin.IN, Pin.PULL_UP)
        sm = rp2.StateMachine(0, phases, freq=2_000_000, in_base=spd, jmp_pin=spd)
        sm.active(1)
        self.wheel = Wheel(sm)
        # ADCs built from Pins so MicroPython disables the pad pull-down
        # (by channel number it stays on and VSYS reads about half).
        self.throttle = Throttle(ADC(Pin(C.PIN_THR)))
        self._vsys = ADC(Pin(C.PIN_VSYS))
        self._brake = Pin(C.PIN_BRAKE, Pin.IN, Pin.PULL_UP) if C.BRAKE_FITTED else None

    def brake(self):
        return self._brake is not None and self._brake.value() == 0

    def vsys(self):
        return self._vsys.read_u16() * (3 * 3.3 / 65535)
