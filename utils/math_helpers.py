# utils/math_helpers.py — Generic scalar math helpers
#
# Prevents repeated small formulas across modules.
# Keep this file small — move application-specific helpers to their service.


def clamp(value, lo, hi):
    """Clamp value to [lo, hi]."""
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def linear_map(x, in_lo, in_hi, out_lo, out_hi):
    """Linearly map x from [in_lo, in_hi] to [out_lo, out_hi]."""
    if in_hi == in_lo:
        return out_lo
    return out_lo + (x - in_lo) * (out_hi - out_lo) / (in_hi - in_lo)


def safe_div(numerator, denominator, default=0.0):
    """Divide with a fallback if the denominator is zero."""
    if denominator == 0:
        return default
    return numerator / denominator


def percent_from_range(value, lo, hi):
    """Return value as a percentage within [lo, hi], clamped to 0–100."""
    if hi <= lo:
        return 0.0
    pct = (value - lo) / (hi - lo) * 100.0
    return clamp(pct, 0.0, 100.0)


# TODO: Keep this file small
# TODO: Move anything application-specific out to the relevant service
