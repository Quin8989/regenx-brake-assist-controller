# strategy.py — the regen law behind the D10 socket.
#
# A strategy implements update() with this signature, stays pure, and may
# raise: control.py turns any exception or out-of-range return into DEAD (0 A
# until power cycle). The envelope clamps whatever it returns, so a strategy
# needs no safety checks of its own. Gains and setpoint are tuned in the sim.

import config as C


class Strategy:
    """Contract. + amps = assist, - amps = regen."""

    def reset(self):
        """Called on every entry to RUN (boot, after LIMP): drop stale state."""

    def update(self, s, w_rpm, throttle, i_last, dt):
        """s: carrier slip; i_last: the current actually sent last tick."""
        raise NotImplementedError


class SlipRegulator(Strategy):
    """PI on carrier slip toward SLIP_SET; the throttle ends regen.

    Velocity form: each tick adjusts the current actually sent last tick, so
    nothing winds up while the envelope clamps it. While the rider holds the
    carrier (s < SLIP_SET) regen grows until the brake just slips, so the
    braking torque is whatever the rider's squeeze can hold; when they let go
    the carrier freewheels (s -> 1) and regen falls to 0. Regenerating with
    nobody braking is harmless: with the carrier free the motor has nothing to
    push against and only slows its own rotor.
    """

    def __init__(self):
        self._e = None

    def reset(self):
        self._e = None

    def update(self, s, w_rpm, throttle, i_last, dt):
        e = C.SLIP_SET - s
        de = 0.0 if self._e is None else e - self._e
        self._e = e
        if throttle > 0.0:
            return C.I_ASSIST_MAX * throttle
        r = (-i_last if i_last < 0.0 else 0.0) + C.SLIP_KP * de + C.SLIP_KI * e * dt
        return -r if r > 0.0 else 0.0
