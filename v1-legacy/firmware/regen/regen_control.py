"""Shared regen control helpers used by sim and production paths.

This module keeps the common control policy in one place:
    - voltage taper near full caps
    - feedforward current from RPM
    - generic regen command shaping under power/duty/current limits
"""

import math


def voltage_taper(vcap, taper_start, taper_end):
    """Linear taper: 1.0 below start, 0.0 at/above end."""
    if vcap <= taper_start:
        return 1.0
    if vcap >= taper_end:
        return 0.0
    return (taper_end - vcap) / (taper_end - taper_start)


def ff_current_from_rpm(rpm, gain, *, flux_linkage, phase_resistance,
                        pole_pairs, current_limit):
    """Feedforward current from electrical back-EMF, clamped to limits."""
    omega_e = rpm * pole_pairs * 2.0 * math.pi / 60.0
    current = gain * flux_linkage * omega_e / phase_resistance
    if current < 0.0:
        return 0.0
    if current > current_limit:
        return current_limit
    return current


def apply_regen_limits(current_cmd, *, current_limit,
                       power_w=None, power_limit_w=None,
                       duty_cycle=None, duty_limit=None):
    """Shape a regen current command under shared safety limits."""
    if power_limit_w is not None and power_w is not None and power_w > power_limit_w:
        current_cmd *= power_limit_w / power_w

    if duty_limit is not None and duty_cycle is not None and duty_cycle > duty_limit:
        headroom = (1.0 - duty_cycle) / (1.0 - duty_limit)
        if headroom < 0.0:
            headroom = 0.0
        elif headroom > 1.0:
            headroom = 1.0
        current_cmd *= headroom

    if current_cmd < 0.0:
        return 0.0
    if current_cmd > current_limit:
        return current_limit
    return current_cmd