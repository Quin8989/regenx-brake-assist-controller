# strategy.py — the deferred control law's socket (RGX-2-003 D10).
#
# The real law is the output of the scoring work; it must implement update()
# with this exact signature and be pure (no allocation, no I/O). Everything
# here is replaceable scaffolding — ride-able, deliberately not the product.

import config


class Strategy:
    """Contract. + amps = assist, - amps = regen."""

    def update(self, s, omega_wheel, v_bank, throttle, brake, i_motor, dt):
        raise NotImplementedError


class Placeholder(Strategy):
    """Minimal proportional scaffold.

    Assist: throttle maps linearly to assist current.
    Regen:  when the lever is present it announces intent instantly (D9);
            otherwise slip departure from freewheel (s dropping below ~0.97)
            reveals the rider dragging the carrier. Current rises as the
            carrier is held harder: I = I_max * (1 - s).
    The safety envelope in control.py clamps everything after this.
    """

    REGEN_ONSET_S = 0.97

    def update(self, s, omega_wheel, v_bank, throttle, brake, i_motor, dt):
        braking = brake or (omega_wheel > 30.0 and s < self.REGEN_ONSET_S)
        if braking:
            return -config.I_REGEN_MAX_A * (1.0 - s)
        if throttle > 0.0:
            return config.I_ASSIST_MAX_A * throttle
        return 0.0
