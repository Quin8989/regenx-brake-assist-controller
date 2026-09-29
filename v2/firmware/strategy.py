# strategy.py — the deferred control law's socket (RGX-2-003 D10).
#
# The real law comes out of the scoring work. It implements update() with this
# signature, stays pure, and may raise: control.py turns any exception or
# out-of-range return into DEAD (0 A until power cycle). The envelope clamps
# whatever it returns, so a strategy never needs its own safety checks.

import config as C


class Strategy:
    """Contract. + amps = assist, - amps = regen."""

    def reset(self):
        """Called on every entry to RUN (boot, after LIMP): drop stale state."""

    def update(self, s, omega_wheel, v_bank, throttle, brake, i_motor, dt):
        raise NotImplementedError


class Placeholder(Strategy):
    """Ride-able scaffold, deliberately not the product.

    The lever (when fitted) and a held carrier with the throttle released both
    mean braking; regen grows as the carrier is held harder, I = Imax (1 - s).
    Slip alone cannot mean braking while the throttle is open, because the
    clutch holds the carrier during assist as well (s = 0 in both).
    Regenerating when the rider is not braking is harmless: with the carrier
    free the motor has nothing to push against and only slows its own rotor.
    """

    REGEN_ONSET_S = 0.97

    def update(self, s, omega_wheel, v_bank, throttle, brake, i_motor, dt):
        if brake or (throttle == 0.0 and s < self.REGEN_ONSET_S):
            return -C.I_REGEN_MAX * (1.0 - s)
        return C.I_ASSIST_MAX * throttle
