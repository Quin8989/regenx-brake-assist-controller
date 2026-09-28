# kinematics.py — Willis relations, unit conversions, live k cross-check.
#
# Pure functions, importable under CPython. Signs: forward wheel motion is
# positive; DIR_SIGN (config) maps the VESC's ERPM sign onto that convention.
# Spec RGX-2-001 §1; RGX-2-003 D10.

import config


def motor_rpm(erpm):
    """VESC electrical rpm -> signed mechanical rotor rpm, forward +."""
    return erpm * config.DIR_SIGN / config.POLE_PAIRS


def wheel_rpm_from_period_us(period_us):
    """Shell-sensor full pulse period (us) -> wheel rpm. 0 if stopped."""
    if period_us <= 0:
        return 0.0
    return 60_000_000.0 / (period_us * config.SPD_PPR)


def kmh(wheel_rpm):
    return wheel_rpm * config.WHEEL_CIRC_M * 60.0 / 1000.0


def carrier_rpm(m_rpm, w_rpm):
    """Willis: omega_c = (omega_m + k*omega_w) / (1 + k)."""
    k = config.K_RATIO
    return (m_rpm + k * w_rpm) / (1.0 + k)


def carrier_free_rpm(w_rpm):
    """Carrier speed with the rotor parked (coasting overrun)."""
    k = config.K_RATIO
    return k * w_rpm / (1.0 + k)


def slip(m_rpm, w_rpm):
    """s = omega_c / omega_c_free, clamped to [0,1].

    1 = freewheeling, 0 = carrier held. NOTE: s is also 0 during assist
    (the one-way clutch grounds the carrier); interpret only with mode
    context (throttle/brake), never alone. s equals the pad-loss fraction
    during regen (spec §1).
    """
    free = carrier_free_rpm(w_rpm)
    if free <= 1e-6:
        return 1.0
    s = carrier_rpm(m_rpm, w_rpm) / free
    if s < 0.0:
        return 0.0
    if s > 1.0:
        return 1.0
    return s


class KCrossCheck:
    """During assist |omega_m| = k * |omega_w| exactly (carrier grounded).

    Feed it samples whenever assist current is flowing and both speeds are
    live; it maintains an EWMA estimate of k and a health flag. RGX-2-003 D10.
    """

    def __init__(self, alpha=0.02, tol=0.05):
        self._alpha = alpha
        self._tol = tol
        self.k_est = config.K_RATIO
        self.samples = 0
        self.healthy = True

    def feed(self, m_rpm, w_rpm):
        if w_rpm < 30.0:          # too slow: quantisation dominates
            return
        ratio = abs(m_rpm) / w_rpm
        self.k_est += self._alpha * (ratio - self.k_est)
        self.samples += 1
        if self.samples >= 50:
            err = abs(self.k_est - config.K_RATIO) / config.K_RATIO
            self.healthy = err <= self._tol
