# sensors.py — wheel speed and throttle.
# Specification sections 10.1 to 10.3.
#
# Wheel and Throttle take their hardware object as an argument, so the logic
# runs on a desktop Python with stand-ins for the hardware. Only Sensors()
# touches the Pico's machine and rp2 modules.

import config as C


class Wheel:
    """Wheel speed in revolutions per minute, updated once per control tick.

    The wheel-speed sensor gives 6 pulses per revolution. A programmable
    input/output state machine on the Pico (hardware that runs on its own,
    independent of the processor cores) times how long the signal stays high
    and how long it stays low, in microseconds, and queues each of those
    phase lengths for this code to read.

    Any two consecutive phases, one high and one low, add up to one full
    pulse period, so the speed estimate refreshes at every edge. A phase
    shorter than SPD_MIN_PHASE_US is electrical noise (or the state machine's
    counter running out after 71 minutes at a standstill) and restarts the
    pairing. Between edges the estimate is capped by the time since the last
    edge, so it falls while braking instead of holding an out-of-date period,
    and reads zero once the wheel has stopped.
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
            else:                       # stopped: pair only new phases on restart
                self.rpm = 0.0
                self._prev = 0
        return self.rpm


class Throttle:
    """Throttle position from 0 to 1 with a dead band at idle, or 0 when the
    reading cannot be trusted (specification section 10.3).

    Outside the THR_LO to THR_HI window (a broken wire reads 0 volts through
    R2, a short circuit reads the supply rail) the throttle simply reads 0:
    assist stops, regenerative braking is untouched, and there is no fault
    state to manage. Assist is only enabled once the throttle has been seen
    at idle, both at power-on and after any out-of-window reading, so a held,
    stuck or flickering throttle does nothing until it is released.
    """

    def __init__(self, adc):
        self.adc = adc
        self.armed = False

    def read(self):
        f = self.adc.read_u16() / 65535
        if not C.THR_LO < f < C.THR_HI:
            self.armed = False
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
    """The sensors control.py reads. Runs on the Pico only."""

    def __init__(self):
        import rp2
        from machine import ADC, Pin

        @rp2.asm_pio()
        def phases():
            # State machine program: measure every high and every low phase
            # of the wheel-speed signal and queue its length in microseconds
            # (each counting loop takes 2 cycles at a 2 megahertz clock).
            # It first waits for a rising edge so the first phase is whole.
            wait(0, pin, 0)                 # noqa: F821 (assembler names)
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
        # Build the analog-to-digital converter from a Pin object so
        # MicroPython switches off the pin's internal pull-down resistor.
        # Created from a bare channel number it stays on and drags the
        # throttle reading down.
        self.throttle = Throttle(ADC(Pin(C.PIN_THR)))
